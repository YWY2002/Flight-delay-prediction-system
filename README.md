# Flight Delay Prediction Pipeline

Live flight + weather ingestion, approach-anomaly detection (go-arounds, holds),
and airport-level delay-cascade prediction - built as a production-shaped ML
pipeline, not a notebook.

- [ARCHITECTURE.md](ARCHITECTURE.md) - components, data flow, conventions, and
  the decision log. Start here to understand how the system fits together.
- [flight-delay-pipeline-plan.md](flight-delay-pipeline-plan.md) - the
  task-by-task implementation plan.

## Status

**Phase 1 (ingestion) is complete.** All four sources are built and running:
OpenSky state vectors, METAR, TAF, and FAA NAS status, each on its own cadence,
landing in append-only partitioned Parquet.

Live-verified: OpenSky, METAR, and FAA closures. Still unverified: TAF, and the
FAA ground-stop and GDP sections (no programs were active when the feed was
captured). See
[implementation status](ARCHITECTURE.md#12-implementation-status) for the full
inventory and
[tech stack](ARCHITECTURE.md#1-tech-stack) for what the project runs on.

Next: Phase 2, the silver layer.

## Quickstart

```bash
# Install everything (runtime + dev tools) into a local .venv
uv sync
```

```bash
# Install the git hooks (once per clone). Wires up BOTH the pre-commit and
# pre-push stages via default_install_hook_types.
uv run pre-commit install
```

```bash
# Copy the config template and fill in your OpenSky credentials
cp .env.example .env
```

## Checks

The hooks run these automatically. To run them by hand:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run mypy
uv run pytest
```

To run every hook against the whole repo without committing:

```bash
uv run pre-commit run --all-files
```

### What runs when

| Stage | Checks |
|---|---|
| pre-commit | file hygiene, large-file + private-key guards, no-em-dash, ruff format, ruff lint, mypy |
| pre-push | pytest |
| CI (Phase 7) | all of the above, so nothing depends on local trust |

pytest is on pre-push rather than pre-commit deliberately. A commit is a local
checkpoint (WIP, mid-refactor, bisect points); gating that on a green suite
fights how git is meant to be used and pushes people toward `--no-verify`, which
disables *every* hook including the secret scan. A push is where work becomes
shared, so that is the right gate for tests.

## Conventions

- **`uv.lock` is the single source of truth for versions.** The hooks invoke
  tools through `uv run` rather than pre-commit's own tool repos, so the hook
  and CI provably run the same binary. Pinning a tool in two places lets them
  drift, which yields hooks that pass while CI fails on identical code.
- **No `requirements.txt`.** It is a derived artifact and goes stale silently.
  If a deploy target ever needs one, generate it on demand:
  `uv export --format requirements-txt -o requirements.txt`. Use `-o`, not shell
  redirection: PowerShell's `>` writes UTF-16 and pip cannot read it.
- **Dependencies are added with the code that imports them**, never
  speculatively: `uv add <pkg>` (or `uv add --dev <pkg>` for tooling).

## Layout

```
src/flight_delay/
  ingest/    # API clients + pollers (raw data in)
    poller.py            # the orchestrator, one cadence per source
    bronze.py errors.py http.py retry.py   # shared by every source
    opensky/ weather/ faa/                 # one subpackage per upstream API
  features/  # trajectory + weather feature logic
  training/  # dataset build, train, evaluate
  serving/   # FastAPI app (predictions out)
  common/    # config, schemas, storage IO
tests/       # unit + contract tests (no live network in CI)
```
