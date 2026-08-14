"""Bronze layer writer: append-only Parquet, partitioned by ingestion time.

Bronze is the layer you can never regenerate. If a feature turns out to be
wrong, silver and gold can be recomputed from bronze; if bronze dropped the
data, it is gone permanently. That asymmetry drives every decision here:

- **Append-only.** Nothing is ever updated or deleted. A new poll writes a new
  file; it never touches an existing one.
- **Atomic writes.** Files land via a temp-file-and-rename, so a process killed
  mid-write leaves no half-written Parquet for a reader to choke on.
- **Explicit schema.** Types are pinned rather than inferred, so files written
  weeks apart are guaranteed to union cleanly.
- **Content hash per record.** Lets silver deduplicate without guessing what
  makes two records the same.

Layout, Hive-style so DuckDB can read it with `hive_partitioning=true`:

    data/bronze/{source}/date=YYYY-MM-DD/hour=HH/{timestamp}-{suffix}.parquet
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from flight_delay.common.logging_config import get_logger

logger = get_logger(__name__)

# 128 bits. Collision probability stays negligible far beyond any volume this
# project will reach, at half the width of a SHA-256 hex digest. Hash columns
# are high-cardinality, so Parquet's dictionary encoding cannot compress them
# and the width is paid per row, every row, forever.
_HASH_DIGEST_BYTES = 16


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one write. Returned rather than logged so callers can report
    it in their own structured log line and, later, as a metric."""

    path: Path | None
    rows: int
    bytes_written: int


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Stable content hash of one record, for deduplication.

    Canonical JSON (sorted keys, no incidental whitespace) so the digest depends
    only on content, never on dict ordering or formatting.

    The caller decides what goes in, and that choice defines what "duplicate"
    means. Two rules matter:

    - **Ingestion metadata must be excluded.** `ingested_at` differs on every
      write by construction, so including it would make every record unique and
      quietly defeat the entire mechanism.
    - **Observation context should usually be excluded too.** The KJFK and KEWR
      bounding boxes overlap heavily (the airports are ~18 nm apart, and the
      boxes reach 60 nm), so the same aircraft is genuinely returned by both
      polls. Those two rows describe one physical observation and should hash
      identically. The `airport` column is retained alongside for attribution,
      but it is not part of the identity.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=_HASH_DIGEST_BYTES).hexdigest()


class BronzeWriter:
    """Writes partitioned Parquet into the bronze layer."""

    def __init__(self, root: Path, *, compression: str = "zstd") -> None:
        """
        Args:
            root: Bronze root, normally `settings.bronze_dir`.
            compression: zstd gives noticeably better ratios than snappy on this
                kind of repetitive telemetry, at decompression speed that is
                still far faster than the disk. DuckDB and pyarrow both read it
                without configuration.
        """
        self._root = root
        self._compression = compression

    def partition_dir(self, source: str, partition_time: datetime) -> Path:
        """Directory for a given source and ingestion timestamp.

        Partitioned by **ingestion** time, not event time. Ingestion time is
        known at write time and only moves forward, so writes are pure appends
        that never revisit an existing partition. Event-time partitioning would
        scatter a single poll across many directories (aircraft report stale
        `last_contact` values) and force rewrites of closed partitions when data
        arrives late. Silver is free to re-partition by event time; bronze
        records when we learned something, not when it happened.
        """
        return (
            self._root
            / source
            / f"date={partition_time.strftime('%Y-%m-%d')}"
            / f"hour={partition_time.strftime('%H')}"
        )

    def write(
        self,
        source: str,
        rows: Sequence[Mapping[str, Any]],
        schema: pa.Schema,
        *,
        partition_time: datetime,
    ) -> WriteResult:
        """Append one Parquet file. Never modifies existing files.

        Args:
            rows: Records to write, already carrying their metadata columns.
            schema: Explicit pyarrow schema. Passing it is mandatory rather than
                letting pyarrow infer: a batch where every `sensors` value is
                null infers a different type than one with values, and two such
                files will not union. Inferred schemas drift silently and the
                damage only appears at query time, weeks later.
        """
        if not rows:
            # An empty poll is normal (a tight bbox at night). Writing a
            # zero-row file per cycle would litter the lake with thousands of
            # useless files and slow every subsequent scan.
            logger.debug("bronze.write.skipped_empty", source=source)
            return WriteResult(path=None, rows=0, bytes_written=0)

        directory = self.partition_dir(source, partition_time)
        directory.mkdir(parents=True, exist_ok=True)

        table = pa.Table.from_pylist(list(rows), schema=schema)

        # Sortable by name and collision-free: the timestamp orders files
        # chronologically within the hour, the random suffix keeps concurrent
        # writers (or a restart inside the same second) from clashing.
        stem = f"{partition_time.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
        final_path = directory / f"{stem}.parquet"
        temp_path = directory / f".{stem}.parquet.tmp"

        try:
            pq.write_table(table, temp_path, compression=self._compression)
            # Atomic on POSIX and on Windows (os.replace) within one filesystem.
            # Without this, SIGKILL mid-write leaves a truncated Parquet file
            # that breaks every later read of the partition. This is what makes
            # "kill and restart loses at most one poll" true.
            os.replace(temp_path, final_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        size = final_path.stat().st_size
        logger.debug(
            "bronze.write.completed",
            source=source,
            path=str(final_path),
            rows=table.num_rows,
            bytes=size,
        )
        return WriteResult(path=final_path, rows=table.num_rows, bytes_written=size)
