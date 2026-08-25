#!/usr/bin/env bash
#
# Mirror the bronze lake from the EBS volume to S3.
#
# EBS is the staging buffer the writer needs (it depends on os.replace, which
# S3 has no equivalent for). S3 is the durable copy and the one you query from
# a laptop. This script is the only thing joining them, so it errs towards
# doing nothing rather than doing something destructive.
#
#   BRONZE_DIR   local root, matching the -v host path on the container
#   S3_URI       destination, e.g. s3://flight-delay-bronze/bronze
#
set -euo pipefail

BRONZE_DIR="${BRONZE_DIR:-/mnt/bronze}"
S3_URI="${S3_URI:?S3_URI is required, e.g. s3://flight-delay-bronze/bronze}"
LOCK_FILE="${LOCK_FILE:-/run/flight-delay-sync.lock}"

log() { printf '%s sync: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [[ ! -d "$BRONZE_DIR" ]]; then
    log "ERROR: $BRONZE_DIR does not exist. Is the EBS volume mounted?"
    exit 1
fi

# An empty source with a healthy-looking exit code is how you quietly sync
# nothing for a month and find out when you go looking for the data.
if ! find "$BRONZE_DIR" -name '*.parquet' -print -quit | grep -q .; then
    log "ERROR: no parquet files under $BRONZE_DIR. Refusing to report success."
    exit 1
fi

# Skip rather than stack up if the previous run is still going. Only matters
# once the lake is big enough that a sync outlasts the interval, but a pile of
# concurrent syncs is a bad way to discover that.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "previous run still in progress, skipping this tick"
    exit 0
fi

log "syncing $BRONZE_DIR -> $S3_URI"

# Two flags carry the safety of this whole script:
#
#   --exclude "*.tmp"   BronzeWriter writes <target>.parquet.tmp and renames it
#                       over the real file. The rename is atomic so any .parquet
#                       on disk is complete, but the .tmp is not, and uploading
#                       one would put a truncated file in the durable store.
#
#   NO --delete         Deliberately absent. Bronze is append-only history, and
#                       --delete would let a wiped or unmounted EBS volume
#                       propagate that deletion to the only good copy. If you
#                       ever need to remove something from S3, do it by hand.
aws s3 sync "$BRONZE_DIR" "$S3_URI" \
    --exclude "*.tmp" \
    --no-progress \
    | tee /tmp/flight-delay-sync.out || {
        log "ERROR: aws s3 sync failed"
        exit 1
    }

uploaded=$(grep -c '^upload:' /tmp/flight-delay-sync.out || true)
log "done, ${uploaded} file(s) uploaded"
