# Flight Delay Prediction Pipeline — Implementation Plan (Minimal Viable MLOps)

**Goal:** Ingest live flight + weather data, detect anomalous approach patterns (go-arounds, extended holds), and predict airport-level delay cascades — built as a real pipeline with CI/CD and monitoring, not a notebook.

**Design philosophy:** Every component is the *simplest thing that is still production-shaped*. Local-first, Docker-everywhere, one repo, swappable parts.

---

## 1. Data Sources (Decision)

| Need | Source | Cost | Notes |
|---|---|---|---|
| Live positions, altitude, velocity, vertical rate, heading | **OpenSky Network REST API** (`/states/all`) | Free | OAuth2 client-credentials; daily credit budget (~400 anonymous / ~4,000 registered / ~8,000 if you feed ADS-B). Use bounding boxes to save credits. |
| Aircraft type, registration, built year (→ age) | **OpenSky Aircraft Metadata Database** | Free | Downloadable CSV snapshot, keyed by `icao24`. Updated irregularly — download once, refresh monthly. |
| Weather (wind, visibility, ceiling, precip, flight category) | **NOAA aviationweather.gov Data API** (`/api/data/metar`, `/api/data/taf`) | Free, no key | JSON output; can query ~15 days back. US + worldwide stations. |
| Airport-level delay programs (ground stops, ground delay programs) | **FAA NAS Status API** (nasstatus.faa.gov) | Free | US airports only. Real-time delay/closure advisories. |
| Historical delay labels for training | **BTS On-Time Performance data** (transtats.bts.gov) | Free | Monthly CSVs, US domestic flights, scheduled vs. actual times. Published with ~1–2 month lag. |

**⚠️ Key constraint that shapes the whole design:** OpenSky provides *no schedule or delay data* — only what can be derived from ADS-B. So:

- **Training labels** come from BTS historical data (US, delayed publication).
- **Live features** come from OpenSky + METAR + FAA status.
- Therefore: **scope v1 to 1–3 busy US airports** (e.g., KJFK, KEWR, KORD) so labels, weather, and live data all overlap, and the OpenSky credit budget survives.

---

## 2. Target Architecture (v1)

```
                ┌────────────────────────────────────────────────┐
                │                Orchestrator (Prefect)          │
                └────────────────────────────────────────────────┘
                     │                │                 │
              every 60–120s        hourly           weekly
                     ▼                ▼                 ▼
┌─────────────┐  ┌──────────┐  ┌──────────────┐  ┌───────────────┐
│ OpenSky     │→ │ Ingest   │→ │ Feature      │→ │ Train/Eval    │
│ METAR/TAF   │  │ Service  │  │ Builder      │  │ (LightGBM +   │
│ FAA Status  │  │ (raw     │  │ (go-arounds, │  │  MLflow)      │
└─────────────┘  │ Parquet) │  │ holds, wx)   │  └──────┬────────┘
                 └────┬─────┘  └──────┬───────┘         │ registry
                      ▼               ▼                 ▼
                 ┌─────────────────────────┐   ┌────────────────┐
                 │  Data Lake: Parquet +   │   │ FastAPI serving │
                 │  DuckDB (local, S3 later)│  │ /predict        │
                 └─────────────────────────┘   └────────────────┘
                              │
                 ┌────────────▼────────────┐
                 │ Monitoring: data quality │
                 │ checks, Evidently drift, │
                 │ Prometheus + Grafana     │
                 └─────────────────────────┘
        CI/CD: GitHub Actions (lint → test → build → train → promote)
```

**Stack (all free/OSS):** Python 3.12 · uv · Pydantic · Parquet + DuckDB · Prefect (OSS) · LightGBM · MLflow · FastAPI · Docker Compose · GitHub Actions · Evidently · Prometheus/Grafana.

---

## 3. Phase 0 — Project Scaffolding

**Outcome:** A repo where `docker compose up` runs, tests pass, and CI is green on the first commit.

- [x] 0.1 Create mono-repo layout:
  ```
  flight-delay-pipeline/
  ├── src/
  │   ├── ingest/          # API clients + pollers
  │   ├── features/        # trajectory + weather feature logic
  │   ├── training/        # dataset build, train, evaluate
  │   ├── serving/         # FastAPI app
  │   └── common/          # config, schemas, storage IO
  ├── flows/               # Prefect flow definitions
  ├── tests/
  ├── docker/              # Dockerfiles per service
  ├── .github/workflows/
  ├── pyproject.toml
  └── docker-compose.yml
  ```
- [x] 0.2 Init `pyproject.toml` with `uv`; pin dependencies; add `ruff` + `mypy` + `pytest`.
- [x] 0.3 Add `pre-commit` hooks (ruff format, ruff lint, mypy).
  - Also: file hygiene, large-file guard, private-key scan, `no-em-dash`, and
    `pytest` at the **pre-push** stage. Plus `.gitattributes` for LF policy.
- [x] 0.4 Central config with Pydantic Settings: airports list, bounding boxes, poll intervals, storage paths — all via env vars / `.env` (never hardcode).
  - **Deviation:** split into two layers. Deployment settings live in `.env`
    (`Settings`); domain reference data lives in committed `config/airports.toml`.
    Bounding boxes are **derived** from coordinates rather than stored, so the
    two can never disagree. See decision log 3 and 4 in `ARCHITECTURE.md`.
- [x] 0.5 Secrets: `.env` locally (gitignored), GitHub Actions secrets for CI. Store OpenSky client id/secret here.
  - `.env.example` committed as the template. GitHub Actions secrets land with CI (7.1).
- [ ] 0.6 `docker-compose.yml` with placeholder services: `ingest`, `serving`, `mlflow`, `prefect`, `grafana`, `prometheus`.
  - **Deferred by decision.** Standing up six empty containers before any code
    needs them means debugging infrastructure with no payload. Each service
    arrives in the commit that needs it. See decision log 1.
- [ ] 0.7 Register an OpenSky account + create API client (client id/secret) — raises daily credit budget vs. anonymous.
  - **Blocked on you.** Nothing has been run against the live API yet.

**Definition of done:** `uv run pytest` passes (with one trivial test), `docker compose config` validates, pre-commit runs clean.

**Actual status:** 131 tests pass, pre-commit runs clean. `docker compose config`
is not applicable while 0.6 is deferred.

---

## 4. Phase 1 — Ingestion Service

**Outcome:** Raw data lands on disk continuously, append-only, partitioned by date, and survives API failures.

### 4.1 OpenSky client
- [x] 1.1 OAuth2 client-credentials flow: exchange id/secret for bearer token; auto-refresh (tokens expire ~30 min).
  - `ingest/opensky_auth.py`. Proactive refresh with a skew margin clamped to
    half the token lifetime; monotonic clock so NTP corrections cannot revive an
    expired token; one 401 retry for server-side revocation.
- [x] 1.2 `get_states(bbox)` — call `/states/all` with `lamin/lamax/lomin/lomax` per airport (e.g., ~60 nm box around each). Parse the positional array into a typed Pydantic model (`icao24, callsign, lon, lat, baro_alt, geo_alt, velocity, heading, vertical_rate, on_ground, ts`).
  - `ingest/opensky_client.py`. **Deviation:** units are in the field names
    (`baro_altitude_m`, `velocity_ms`, `true_track_deg`) because OpenSky reports
    SI while Phase 3 thresholds are in feet. See decision log 7.
- [x] 1.3 Credit budgeting: track requests/day; poll every 60–120 s per bbox; back off on 429/5xx with exponential retry (tenacity).
  - `ingest/credit_budget.py` (proactive gate, UTC-midnight reset, reconciled
    against the server's `X-Rate-Limit-Remaining` header) and `ingest/retry.py`
    (tenacity; only `retryable` errors, `Retry-After` honoured over our own
    backoff, jittered, bounded attempts).
  - The budget gate sits **inside** the retry loop, so every attempt is charged
    and a retry storm cannot spend past the daily allowance.
  - **Note:** the 60–120 s poll cadence itself is the poller loop, which lands
    with 1.8–1.10. The 60 s floor is already enforced in `Settings`.

### 4.2 Weather client
- [ ] 1.4 `get_metar(icao_ids)` — `https://aviationweather.gov/api/data/metar?ids=KJFK,KEWR&format=json`; poll every 10 min (METARs update hourly + SPECIs).
- [ ] 1.5 `get_taf(icao_ids)` — same pattern, poll hourly.

### 4.3 FAA NAS status client
- [ ] 1.6 Poll airport status/advisories every 5 min; parse ground stop / GDP events per airport.

### 4.4 Aircraft metadata
- [ ] 1.7 One-off script: download OpenSky aircraft database CSV → keep `icao24, typecode, model, built, operator`; derive `aircraft_age = current_year − built`. Store as a reference Parquet. Add a monthly refresh task.

### 4.5 Raw storage (bronze layer)
- [ ] 1.8 Writer: append Parquet files partitioned as `data/bronze/{source}/date=YYYY-MM-DD/hour=HH/*.parquet`.
- [ ] 1.9 Every record carries `ingested_at` and raw payload hash for dedup.
- [ ] 1.10 Structured logging (structlog): one log line per poll with counts, latency, credit estimate.

### 4.6 Tests
- [ ] 1.11 Unit tests with recorded API fixtures (respx/vcr-style) — no live calls in CI.
  - **OpenSky done; METAR and FAA pending** (those clients do not exist yet).
    Uses `httpx.MockTransport` rather than respx: it is built into httpx, so no
    extra dependency. Clocks and HTTP clients are injected, so no test sleeps or
    touches the network.
- [x] 1.12 Contract test: parsing fails loudly (not silently) if OpenSky changes array shape.
  - The positional layout lives in one place (`_STATE_FIELDS`). A strict
    `icao24` pattern catches index drift; short arrays are rejected while
    appended fields are tolerated. See `tests/test_opensky_client.py`.

**Definition of done:** Run ingest for 1 hour locally → bronze Parquet exists for all 3 sources; killing/restarting the service loses no more than one poll cycle.

---

## 5. Phase 2 — Storage & Data Model (Silver Layer)

**Outcome:** Clean, queryable tables via DuckDB over Parquet. No database server to babysit in v1; swap to Postgres/Timescale or S3+Athena later without touching feature code.

- [ ] 2.1 Define schemas (Pydantic + pandera) for silver tables:
  - `state_vectors(icao24, callsign, ts, lat, lon, alt_baro, velocity, heading, vertical_rate, on_ground, airport_bbox)`
  - `metar(station, obs_time, wind_dir, wind_speed, gust, visibility, ceiling, wx_string, flight_category, temp, dewpoint, altimeter)`
  - `faa_events(airport, event_type, start, end, reason)`
  - `aircraft(icao24, typecode, model, built, age)`
- [ ] 2.2 Bronze→silver job: dedup, type-cast, drop malformed rows (count them — that's a monitoring metric), attach nearest airport.
- [ ] 2.3 Flight-segment assembly: group state vectors by `icao24` + time-gap splitting (> 15 min gap ⇒ new segment) → `flight_segments` table with segment ids.
- [ ] 2.4 DuckDB views over the Parquet partitions for ad-hoc SQL.
- [ ] 2.5 Retention/compaction task: daily job merges small files.

**Definition of done:** `SELECT count(*) FROM state_vectors WHERE date = today` works in DuckDB; pandera validation runs in the pipeline and rejects bad batches.

---

## 6. Phase 3 — Feature Engineering (Approach Anomalies + Weather)

**Outcome:** Per-airport, per-time-window feature rows capturing "how stressed is this airport right now."

### 6.1 Trajectory event detection (the interesting part)
- [ ] 3.1 **Approach detection:** segment is "on approach" to airport A when within ~25 nm, descending (vertical_rate < −2 m/s), altitude < ~8,000 ft, closing distance to field.
- [ ] 3.2 **Go-around detection (rule-based v1):** aircraft on approach descends below ~1,500 ft AGL near the field, then vertical_rate flips strongly positive (> +5 m/s sustained ≥ 30 s) and altitude regains > 1,000 ft without an `on_ground` touch. Emit `go_around_event(airport, icao24, ts)`.
- [ ] 3.3 **Holding detection (rule-based v1):** within 40 nm of airport, near-constant altitude (±300 ft) while cumulative heading change ≥ 720° in ≤ 10 min, or racetrack pattern (alternating ~180° turns). Emit `hold_event` with duration.
- [ ] 3.4 Validate heuristics manually: plot 20 detected events over a map/altitude profile; tune thresholds. Keep thresholds in config, not code.
- [ ] 3.5 Unit-test detectors against synthetic trajectories (crafted go-around, crafted hold, normal landing) — these are pure functions, easy to test.

### 6.2 Windowed airport features (gold layer)
- [ ] 3.6 For each airport × 15-min window compute: `n_arrivals, n_go_arounds, n_holds, mean_hold_duration, arrival_rate_trend, mean_approach_speed`, fleet mix (`share_widebody`, `mean_aircraft_age`).
- [ ] 3.7 Join current METAR: `wind_speed, gust, crosswind_component (needs runway headings — static config per airport), visibility, ceiling, flight_category, precip flag`; plus TAF-based "weather deteriorating in next 2h" flag.
- [ ] 3.8 Join FAA events: active ground stop / GDP indicator.
- [ ] 3.9 Lag features for cascade dynamics: same features at t−15, t−30, t−60 min (delay cascades = temporal propagation).
- [ ] 3.10 Persist `gold/airport_features` Parquet, one row per airport-window.

**Definition of done:** A day of ingested data produces a feature table with zero nulls in required columns; detector unit tests pass in CI.

---

## 7. Phase 4 — Labels, Training & Evaluation

**Outcome:** A registered model that predicts near-term delay state per airport, with honest evaluation.

### 7.1 Labels
- [ ] 4.1 Download 12+ months of BTS On-Time Performance CSVs for chosen airports; build `labels(airport, window_ts, target)`.
- [ ] 4.2 Define v1 target (keep it simple): **binary — "will average departure delay at this airport exceed 15 min in the next 60 min?"** (Cascade severity regression comes later.)
- [ ] 4.3 ⚠️ Gap to accept in v1: BTS history won't overlap your freshly-ingested OpenSky features (BTS lags ~2 months). Mitigation: keep ingesting; after 4–8 weeks you'll have overlapping feature+label months to train on. Until then, bootstrap with weather+BTS-derivable features only, and treat trajectory features as an additive upgrade once overlap exists. Document this in the README.

### 7.2 Training pipeline (a script, not a notebook)
- [ ] 4.4 `training/build_dataset.py`: join gold features ↔ labels on airport+window; temporal train/val/test split (**never random** — split by date).
- [ ] 4.5 `training/train.py`: baseline = predict majority class + a logistic regression; model = LightGBM. Log params, metrics, feature importance, and the exact data date-range to MLflow.
- [ ] 4.6 Metrics: PR-AUC (delays are imbalanced), recall@precision=0.8, calibration curve, and lead-time analysis (does it fire *before* the delay?).
- [ ] 4.7 `training/evaluate.py`: compare candidate vs. current production model on the same held-out window; emit pass/fail against thresholds (e.g., PR-AUC must not regress > 2%).
- [ ] 4.8 Register passing models in MLflow Model Registry with stage `staging` → manual promote to `production` (v1: promotion = CI job with manual approval).

**Definition of done:** `uv run python -m training.train` end-to-end produces an MLflow run + registered model; evaluation gate demonstrably blocks a bad model (test it by training on shuffled labels).

---

## 8. Phase 5 — Serving

**Outcome:** Predictions available two ways — batch (primary) and REST (demo/interactive).

- [ ] 5.1 Batch scorer flow: every 15 min, compute latest feature row per airport → load `production` model from MLflow → write `predictions(airport, ts, p_delay, model_version)` Parquet + expose latest via API.
- [ ] 5.2 FastAPI app:
  - `GET /health` — liveness + model version loaded.
  - `GET /predict/{airport}` — latest prediction + top contributing features (LightGBM feature contributions).
  - `GET /events/{airport}` — recent go-arounds/holds (great for demos).
- [ ] 5.3 Model loading: pull by registry alias at startup; refresh on a timer or SIGHUP — deploys don't require rebuilding the image.
- [ ] 5.4 Dockerize serving; add to compose; pin model version in the response payload (traceability).
- [ ] 5.5 API tests: schema tests + a golden-file prediction test with a frozen model artifact.

**Definition of done:** `curl localhost:8000/predict/KJFK` returns JSON with probability + model version; `docker compose up serving` is all it takes.

---

## 9. Phase 6 — Orchestration

**Outcome:** Nothing runs by hand. One place shows every scheduled run, its status, and retries.

- [ ] 6.1 Prefect flows (thin wrappers over `src/` functions — logic stays importable and testable):
  - `ingest_flow` — every 1–2 min (OpenSky), 10 min (METAR), 5 min (FAA).
  - `silver_flow` — every 15 min (bronze→silver + validation).
  - `features_flow` — every 15 min after silver.
  - `score_flow` — every 15 min after features.
  - `train_flow` — weekly (or manual trigger).
- [ ] 6.2 Retries + failure notifications (start with a Slack/Discord webhook).
- [ ] 6.3 Idempotency: every flow takes a time-window argument and can be re-run/backfilled safely.
- [ ] 6.4 Run Prefect server in compose; deploy flows via `prefect deploy` from CI.

**Definition of done:** Laptop/VM left running overnight → morning shows green scheduled runs and fresh predictions with zero manual steps.

---

## 10. Phase 7 — CI/CD (GitHub Actions)

**Outcome:** Merges are gated on quality; images and models ship automatically.

- [ ] 7.1 `ci.yml` (on PR): ruff lint + format check → mypy → pytest (unit + contract tests, no network) → `docker build` all images. Cache uv + Docker layers.
- [ ] 7.2 `cd.yml` (on merge to main): build + push images to GHCR tagged with git SHA; `prefect deploy` updated flows.
- [ ] 7.3 `train.yml` (weekly cron + manual dispatch): run training in Actions (or self-hosted runner if data is local), log to MLflow, run evaluation gate, register to `staging`.
- [ ] 7.4 `promote.yml` (manual, with environment approval): move `staging` → `production` alias; serving picks it up on next refresh.
- [ ] 7.5 Branch protection: CI required, no direct pushes to main.
- [ ] 7.6 Add a smoke-test job: spin up compose in CI, hit `/health`, assert 200.

**Definition of done:** A PR that breaks a detector test cannot merge; merging to main publishes images; a bad candidate model is blocked by the evaluation gate automatically.

---

## 11. Phase 8 — Monitoring & Observability

**Outcome:** You find out about problems from dashboards/alerts, not from stale predictions.

### 11.1 Pipeline health (Prometheus + Grafana)
- [ ] 8.1 Instrument ingest + serving with `prometheus-client`: rows ingested per source, API error rate, poll latency, OpenSky credits used, prediction latency, model version gauge.
- [ ] 8.2 Grafana dashboard: ingestion freshness per source ("minutes since last successful poll"), rows/hour, flow success rates.
- [ ] 8.3 Alerts (Grafana alerting → webhook): data staleness > 15 min, API error rate > 20%, no predictions in 30 min.

### 11.2 Data quality
- [ ] 8.4 pandera checks in `silver_flow` (types, ranges: lat/lon bounds, altitude ≥ −100 ft, wind ≤ 200 kt); publish "rows rejected" as a metric; alert on spikes.

### 11.3 ML monitoring
- [ ] 8.5 Evidently report (daily job): feature drift of live gold features vs. training reference; prediction distribution drift.
- [ ] 8.6 Delayed ground-truth loop: when new BTS data drops (monthly), auto-backfill actual outcomes → compute realized PR-AUC/calibration for past predictions → log to MLflow + dashboard. This is your real model health signal.
- [ ] 8.7 Alert thresholds: drift score above threshold or realized PR-AUC below floor ⇒ open a "retrain/investigate" notification.

**Definition of done:** Unplug the OpenSky poller → alert fires within 15 min. Feed the drift job scrambled features → drift alert fires.

---

## 12. Build Order & Milestones

| Milestone | Contains | Success signal |
|---|---|---|
| **M1 — Skeleton** (Phase 0) | Repo, CI green, compose stub | PR checks gate merges |
| **M2 — Data flowing** (Phases 1–2) | Ingest + silver tables | 24 h of clean data queryable in DuckDB |
| **M3 — Events** (Phase 3) | Go-around/hold detectors + gold features | Detected events verified by eye; tests in CI |
| **M4 — First model** (Phase 4) | BTS labels, LightGBM, MLflow gate | Registered model beats baseline on temporal holdout |
| **M5 — Live loop** (Phases 5–6) | Scoring + API + Prefect schedules | Fresh prediction every 15 min, hands-free |
| **M6 — Trust** (Phases 7–8) | Full CI/CD + monitoring | Kill-the-poller alert test passes; promotion is gated |

Timebox suggestion: M1–M2 first week; M3 is the most iterative (budget real tuning time); M4 partly blocked on data accumulation (see 4.3) — start BTS-only, upgrade later.

---

## 13. Later Extensions (explicitly out of scope for v1)

- Multi-airport network graph → true cascade propagation modeling (delay at ORD → downstream at EWR).
- Postgres/TimescaleDB or S3 + Iceberg instead of local Parquet.
- Sequence models (temporal transformers) over trajectory windows instead of window aggregates.
- Learned anomaly detection for approaches (autoencoder on trajectories) replacing rule-based detectors.
- Paid schedule/delay APIs (FlightAware AeroAPI, aviationstack) for real-time labels and non-US coverage.
- Kubernetes deployment, feature store (Feast), canary model rollout.

---

## 14. Risks & Gotchas Checklist

- [x] OpenSky credit exhaustion → keep bboxes tight, poll ≥ 60 s, monitor credit metric (8.1).
  - Mitigated in 1.3: proactive daily budget gate, 60 s poll floor enforced at
    config validation, server credit header reconciled on every response,
    low-water warning. The Prometheus metric itself still lands with 8.1.
- [ ] Label lag (BTS ~2 months) → accept in v1; plan data-accumulation period (4.3).
- [ ] ADS-B coverage gaps at low altitude → some landings/go-arounds will be missed; measure detector coverage, don't assume completeness.
- [ ] Non-commercial terms: OpenSky free tier is for research/non-commercial use — fine for this project, revisit before any commercial use.
- [ ] Temporal leakage: all splits by time; never let a window's features include future information.
- [ ] Weather station ≠ airport edge cases: METAR station mapping is per-airport static config; validate ICAO ids once.
