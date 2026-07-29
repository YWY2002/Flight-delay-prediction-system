# Flight Delay Prediction Pipeline

Live flight + weather ingestion, approach-anomaly detection (go-arounds, holds),
and airport-level delay-cascade prediction — built as a production-shaped ML
pipeline, not a notebook.

See [flight-delay-pipeline-plan.md](flight-delay-pipeline-plan.md) for the full design.

## Status

Phase 0 — project scaffolding. Skeleton installs and tests pass.

## Quickstart

```bash
# Install everything (runtime + dev tools) into a local .venv
uv sync

# Run the checks
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

## Layout

```
src/flight_delay/
  ingest/    # API clients + pollers (raw data in)
  features/  # trajectory + weather feature logic
  training/  # dataset build, train, evaluate
  serving/   # FastAPI app (predictions out)
  common/    # config, schemas, storage IO
tests/       # unit + contract tests (no live network in CI)
```
