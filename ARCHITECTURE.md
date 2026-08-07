# Architecture

How this system is put together, and why it is put together that way.

> **Read this first:** this document describes both the **target** architecture
> and what is **actually built today**. The two are clearly marked throughout.
> Sections describing unbuilt components are labelled `PLANNED`. Do not mistake
> the diagrams for reality: see [Implementation status](#implementation-status)
> for the honest inventory.
>
> Task-level detail lives in
> [flight-delay-pipeline-plan.md](flight-delay-pipeline-plan.md). This document
> covers structure and rationale; the plan covers sequencing.

---

## 1. What the system does

Ingest live flight positions and weather, detect anomalous approach patterns
(go-arounds, extended holds), and predict near-term airport delay state.

The point of the project is the **pipeline**, not the model. A notebook that
predicts delays is a different and much easier artifact. This is built as a
production-shaped system: scheduled, monitored, tested, versioned, and able to
recover from the API it depends on going down at 3am.

---

## 2. Design principles

These are the rules that decide arguments. They are listed first because most of
the specific choices later in this document follow from them.

| Principle | What it means in practice |
|---|---|
| **Simplest thing that is still production-shaped** | Parquet + DuckDB instead of a database server. Local-first, swappable later. |
| **Parse once, at the boundary** | External data becomes typed, validated objects the moment it arrives. No dict-shuffling downstream. |
| **Fail loudly at the edge, not quietly in the middle** | A malformed API response raises at ingestion, where the error names the cause, rather than surfacing later as a wrong number. |
| **Derive, do not duplicate** | Bounding boxes are computed from airport coordinates. Two stored copies of the same fact will eventually disagree. |
| **Just-in-time infrastructure** | A service arrives in the commit that needs it, not "up front". Standing up six empty containers on day one teaches nothing and debugs nothing. |
| **Constraints belong as early as possible** | Config-time beats runtime; runtime beats "discovered in the morning". |
| **No live network in tests** | Every external call is behind an injectable client. The suite runs offline, deterministically, in under two seconds. |

---

## 3. System overview `TARGET`

```mermaid
flowchart TB
    subgraph sources["External data sources"]
        OS["OpenSky REST<br/>positions, altitude, vertical rate"]
        WX["NOAA aviationweather<br/>METAR / TAF"]
        FAA["FAA NAS Status<br/>ground stops, GDP"]
        BTS["BTS On-Time Performance<br/>monthly CSV, ~2 month lag"]
    end

    subgraph ingest["Ingestion"]
        POLL["Pollers<br/>auth, retry, credit budget"]
    end

    subgraph lake["Data lake: Parquet + DuckDB"]
        BRONZE["bronze<br/>raw as received"]
        SILVER["silver<br/>cleaned, typed, deduped"]
        GOLD["gold<br/>airport x 15-min features"]
    end

    subgraph ml["Model lifecycle"]
        TRAIN["Training<br/>LightGBM + MLflow"]
        REG["Model registry<br/>staging to production"]
        SCORE["Batch scorer"]
    end

    subgraph serve["Serving"]
        API["FastAPI<br/>/health /predict /events"]
    end

    subgraph ops["Observability"]
        PROM["Prometheus + Grafana"]
        EVID["Evidently drift"]
    end

    ORCH["Orchestrator: Prefect"]

    OS --> POLL
    WX --> POLL
    FAA --> POLL
    POLL --> BRONZE
    BRONZE --> SILVER
    SILVER --> GOLD
    BTS --> TRAIN
    GOLD --> TRAIN
    GOLD --> SCORE
    TRAIN --> REG
    REG --> SCORE
    SCORE --> API
    ORCH -.schedules.-> POLL
    ORCH -.schedules.-> SILVER
    ORCH -.schedules.-> GOLD
    ORCH -.schedules.-> SCORE
    POLL -.metrics.-> PROM
    API -.metrics.-> PROM
    GOLD -.reference vs live.-> EVID
```

**Stack:** Python 3.12, uv, Pydantic, httpx, Parquet + DuckDB, Prefect, LightGBM,
MLflow, FastAPI, Docker Compose, GitHub Actions, Evidently, Prometheus/Grafana.

---

## 4. Data sources and the constraint that shapes everything

| Need | Source | Notes |
|---|---|---|
| Live positions, altitude, velocity, vertical rate | OpenSky `/states/all` | OAuth2 client credentials. Daily credit budget. Bounding boxes keep cost down. |
| Aircraft type, registration, build year | OpenSky metadata CSV | Static snapshot, refreshed monthly. |
| Weather | NOAA aviationweather.gov | METAR and TAF. No key required. |
| Airport delay programs | FAA NAS Status | US only. Ground stops and GDPs. |
| Training labels | BTS On-Time Performance | Monthly CSV, published with a 1 to 2 month lag. |

**The constraint:** OpenSky provides no schedule or delay data, only what is
derivable from ADS-B. Labels therefore come from BTS, which lags by roughly two
months, while features come from live sources. Those two do not overlap until
the pipeline has been ingesting for 4 to 8 weeks.

Three consequences that shape the design:

1. Scope is limited to a few busy US airports (KJFK, KEWR, KORD) so that labels,
   weather, and live positions all cover the same ground.
2. The first model must train on weather and BTS-derivable features only.
   Trajectory features are an additive upgrade once overlap exists.
3. **Ingestion must start early and stay running.** Accumulated data is on the
   critical path, which raises the value of reliability work in Phase 1.

---

## 5. Data architecture: the medallion layers

```mermaid
flowchart LR
    API["External APIs"] -->|"append-only<br/>partitioned by date/hour"| B

    subgraph B["bronze"]
        B1["Raw as received.<br/>Nothing dropped.<br/>ingested_at + payload hash."]
    end

    B -->|"dedup, type-cast,<br/>reject malformed"| S

    subgraph S["silver"]
        S1["state_vectors<br/>metar<br/>faa_events<br/>aircraft<br/>flight_segments"]
    end

    S -->|"detect events,<br/>window, aggregate, lag"| G

    subgraph G["gold"]
        G1["airport_features<br/>one row per airport x 15 min"]
    end

    G --> M["Training / scoring"]
```

Why three layers rather than parsing straight into features:

- **Bronze is append-only and never edited.** When a feature turns out to be
  wrong, you can recompute it from bronze. If ingestion had already dropped the
  data, it is gone permanently. The rejected rows are themselves a monitoring
  signal: a spike in malformed records means the upstream API changed.
- **Silver is where correctness lives.** Deduplication, type enforcement, schema
  validation. One place to fix a data bug.
- **Gold is modelling-ready** and nothing else reads it, so its shape can follow
  the model's needs without disturbing anything upstream.

Storage is Parquet files partitioned as
`data/{layer}/{source}/date=YYYY-MM-DD/hour=HH/*.parquet`, queried through
DuckDB. No database server to operate in v1, and the swap to Postgres or S3 does
not touch feature code.

---

## 6. Code structure

```
src/flight_delay/
  common/     config, reference data, schemas, storage IO
  ingest/     API clients and pollers          (raw data in)
  features/   trajectory + weather feature logic
  training/   dataset build, train, evaluate
  serving/    FastAPI app                       (predictions out)
config/       committed domain reference data
tests/        unit + contract tests, no live network
```

One installable package (`flight_delay.ingest`, not a bare top-level `ingest`)
so generic names cannot collide with other installed libraries.

### Current module map `BUILT`

```mermaid
flowchart TD
    ENV[".env<br/>deployment settings"] --> CFG
    TOML["config/airports.toml<br/>domain reference data"] --> APT

    CFG["common/config.py<br/>Settings"]
    APT["common/airports.py<br/>Airport, BoundingBox"]

    CFG -->|"active ICAO codes"| RES["resolve_active_airports"]
    APT -->|"reference entries"| RES
    RES --> BBOX["bounding_box radius_nm"]

    CFG -->|"credentials, urls, timeout"| AUTH["ingest/opensky_auth.py<br/>OpenSkyTokenProvider<br/>OpenSkyAuth"]
    AUTH -->|"httpx.Auth"| CLI["ingest/opensky_client.py<br/>OpenSkyClient"]
    BBOX -->|"lamin/lamax/lomin/lomax"| CLI
    CLI --> SV["StateVector<br/>typed, validated"]
```

| Module | Responsibility |
|---|---|
| [`common/config.py`](src/flight_delay/common/config.py) | `Settings`: environment-varying configuration and secrets |
| [`common/airports.py`](src/flight_delay/common/airports.py) | `Airport`, `BoundingBox`, reference loading, bbox derivation |
| [`ingest/opensky_auth.py`](src/flight_delay/ingest/opensky_auth.py) | OAuth2 client credentials, token caching and refresh |
| [`ingest/opensky_client.py`](src/flight_delay/ingest/opensky_client.py) | `/states/all` calls, positional array parsing |
| [`ingest/credit_budget.py`](src/flight_delay/ingest/credit_budget.py) | Daily credit accounting and the proactive spend gate |
| [`ingest/retry.py`](src/flight_delay/ingest/retry.py) | Backoff policy for transient failures |

---

## 7. Configuration architecture

Configuration is split into two layers because two genuinely different kinds of
data were being conflated.

```mermaid
flowchart LR
    subgraph dep["Deployment settings"]
        E1[".env / environment<br/>FDP_ prefixed"]
        E2["Varies by environment<br/>laptop, CI, prod"]
        E3["Poll intervals, paths,<br/>secrets, active airports"]
    end

    subgraph dom["Domain reference data"]
        D1["config/airports.toml<br/>committed"]
        D2["Does not vary.<br/>Facts about the world."]
        D3["Coordinates, METAR station,<br/>runway headings"]
    end

    dep -->|"which airports"| J["resolve_active_airports"]
    dom -->|"what they are"| J
    J --> R["tuple of validated Airport"]
```

KJFK's latitude is identical on your laptop, in CI, and in production. It is not
a *setting*. Forcing it through environment variables would mean encoding nested
structure as JSON in a string: unreadable, undiffable, and impossible to comment.
So it lives in version control and gets code-reviewed like source.

Precedence for settings, highest first: explicit kwargs, environment variables,
`.env` file, field defaults.

**Validation encodes domain constraints, not just types.** For example:

```python
opensky_poll_seconds: float = Field(ge=MIN_OPENSKY_POLL_SECONDS, ...)
```

That 60 second floor is the OpenSky credit budget expressed as a type
constraint. Setting it to 5 produces a startup crash rather than an exhausted
daily quota discovered the next morning.

The join between layers fails loudly: an active ICAO code with no reference
entry raises rather than being skipped. Skipping silently would produce a
pipeline that runs green while collecting nothing.

---

## 8. The ingestion path `BUILT`

### 8.1 Authentication

OpenSky issues bearer tokens valid for about 30 minutes. A poller running for
weeks needs roughly 340 refreshes per week, all unattended.

```mermaid
sequenceDiagram
    participant P as Poller
    participant A as OpenSkyAuth
    participant T as TokenProvider
    participant K as Keycloak auth server
    participant O as OpenSky API

    P->>A: GET /states/all
    A->>T: get_token()

    alt cached and not near expiry
        T-->>A: cached token
    else expired or within skew margin
        T->>K: POST grant_type=client_credentials
        K-->>T: access_token, expires_in=1800
        Note over T: deadline = monotonic_now<br/>+ expires_in - skew
        T-->>A: fresh token
    end

    A->>O: GET /states/all + Bearer token
    alt 200
        O-->>A: state vectors
    else 401 revoked early
        O-->>A: 401
        A->>T: invalidate()
        A->>O: retry once with fresh token
    end
    A-->>P: StatesResponse
```

Three decisions inside that flow:

- **Refresh proactively, by a safety margin.** Waiting for a 401 costs a failed
  request on every expiry. The margin is clamped to half the token lifetime, so
  a short-lived token cannot put the deadline in the past and cause a refresh
  loop.
- **Monotonic clock, not wall clock.** Expiry is an elapsed duration. Wall clock
  jumps when NTP corrects it; a backward jump would make an expired token look
  valid and 401 every request afterward.
- **Two separate HTTP clients.** The token request goes through a client with no
  auth attached. Authenticating the token request with a token you do not have
  yet recurses forever.

### 8.2 Parsing: the most fragile point in the system

OpenSky returns each aircraft as a bare array with no field names:

```json
["3c6444","DLH9LF  ","Germany",1458564120,1458564120,6.15,50.19,9639.3,false,...]
```

Index 6 means latitude only by convention. **If a field is ever inserted
upstream, every value shifts one slot, nothing raises, and the pipeline keeps
producing confidently wrong numbers.** Models train on nonsense. Nobody finds out
for months.

The positional layout is therefore written down in exactly one place
(`_STATE_FIELDS`), converted to named fields at the boundary, and validated hard
enough that a shape change fails immediately. The `icao24` pattern
`^[0-9a-f]{6}$` is load-bearing: under a shift, position 0 becomes a callsign or
a country name and is rejected instantly.

Tolerances are asymmetric on purpose. A **shorter** array is unambiguously
broken and rejected. A **longer** one is accepted, because OpenSky appends
optional fields and refusing to ingest over a field we do not read would be
self-inflicted downtime.

---

## 9. Cross-cutting conventions

These apply everywhere and exist to prevent specific, recurring classes of bug.

### Units live in field names

`baro_altitude_m`, `velocity_ms`, `true_track_deg`. OpenSky reports SI units
while aviation thresholds are written in feet and knots. A bare `altitude`
invites comparing metres against a 1,500 ft threshold and getting a
plausible-looking wrong answer. Unit-suffixed names make the mistake visible at
the call site.

### All timestamps are timezone-aware UTC

Aircraft positions get joined against METAR observations and BTS schedules across
timezones. A naive datetime silently adopts whatever timezone the reader assumes,
and an off-by-one-hour join yields a model that looks fine and is wrong.

### Error taxonomy drives retry behaviour

Every ingestion error carries a `retryable` class flag. The retry policy reads
only that flag, so the decision lives at the raise site where the cause is
actually known. A list of retryable types maintained elsewhere drifts out of
sync silently, and it fails in both directions: transient errors stop being
retried, or bad credentials get hammered until access is suspended.

```mermaid
flowchart TD
    E["Ingestion failure"] --> Q{"retryable?"}

    Q -->|"False"| P["Permanent"]
    Q -->|"True"| T["Transient"]

    P --> P1["OpenSkyAuthError<br/>4xx from token endpoint"]
    P --> P2["OpenSkyApiError<br/>401/403 after refresh,<br/>malformed body, bad request"]
    P --> P3["CreditBudgetExhausted<br/>we chose not to call"]

    T --> T1["OpenSkyTransportError<br/>DNS, TLS, timeout"]
    T --> T2["OpenSkyServerError<br/>5xx"]
    T --> T3["OpenSkyRateLimitError<br/>429, carries retry_after"]
    T --> T4["OpenSkyAuthUnavailableError<br/>token endpoint unreachable"]

    P1 --> STOP["Raise immediately.<br/>A human must act."]
    P2 --> STOP
    P3 --> STOP
    T1 --> BACK["Exponential backoff<br/>with jitter, bounded attempts"]
    T2 --> BACK
    T4 --> BACK
    T3 --> RA["Honour Retry-After,<br/>capped by the ceiling"]
```

Default is non-retryable. A new error type is safe until someone deliberately
opts it in, which is the right direction to be wrong in.

### Budget and retry are separate mechanisms

Credit budgeting is **proactive**: refuse a request we cannot afford.
Retry is **reactive**: respond to a failure that already happened. Conflating
them produces a client that retries its way through a budget it already spent.

```mermaid
flowchart TD
    A["get_states(bbox)"] --> B{"retry loop"}
    B --> C["budget.consume(1)"]
    C -->|"exhausted"| X["CreditBudgetExhausted<br/>no network call made"]
    C -->|"ok"| D["HTTP GET /states/all"]
    D --> E{"outcome"}
    E -->|"permanent error"| Y["raise"]
    E -->|"transient error"| B
    E -->|"200"| F["parse + validate"]
    F --> G["budget.reconcile(header)"]
    G --> H["StatesResponse"]
```

Two ordering decisions in that diagram:

- **The budget gate is inside the retry loop**, so every attempt is charged. A
  retried request costs the same as a fresh one; charging only the logical call
  would let a retry storm spend several times its share of the budget that
  exists to prevent exactly that.
- **The server's header wins over local counting.** Local counting gates the
  first request (no response has arrived yet) but drifts across restarts and
  across processes sharing one account. `X-Rate-Limit-Remaining` is
  authoritative and corrects the counter after every success.

The budget resets on the **wall clock** at UTC midnight, deliberately unlike
token expiry which uses the monotonic clock. "Has the calendar date changed" and
"how much time has elapsed" are different questions, and a monotonic counter
cannot answer the first one at all.

### Secrets

`SecretStr` for the client secret, unwrapped exactly once at the point of use.
Logs record token lifetime, never the token. Credentials escape through logs and
tracebacks far more often than through commits. `.env` is gitignored;
`.env.example` is the committed template.

### Testing

No test touches the network or sleeps. Two injection points make that possible:

- `time_source` (defaults to `time.monotonic`) lets a 30-minute expiry be tested
  in microseconds.
- `http_client` (an `httpx.Client`, mocked with `httpx.MockTransport`) answers
  requests in-process and records what was actually sent.

A test that sleeps for 1800 seconds is a test nobody runs, and a gate that never
runs is not a gate.

---

## 10. Quality gates

```mermaid
flowchart LR
    W["Working tree"] -->|"git commit"| PC["pre-commit<br/>hygiene, large files,<br/>private keys, no-em-dash,<br/>ruff, mypy"]
    PC -->|"git push"| PP["pre-push<br/>pytest"]
    PP --> CI["CI (PLANNED)<br/>all of the above<br/>+ docker build"]
    CI --> M["merge"]
```

Hooks invoke tools through `uv run`, so `uv.lock` is the single source of version
truth. Pinning a tool separately in the hook config lets the two drift, producing
hooks that pass while CI fails on identical code.

pytest runs at **pre-push**, not pre-commit. A commit is a local checkpoint (WIP,
mid-refactor, bisect points), and gating that on a green suite pushes people
toward `--no-verify`, which disables every hook at once including the secret
scan. A push is where work becomes shared.

Pre-commit is a convenience, not a boundary: it is trivially bypassable. **CI is
the actual gate**, which is why it runs the same checks.

---

## 11. Implementation status

| Component | Status | Location |
|---|---|---|
| Repo scaffolding, uv, ruff, mypy, pytest | **Built** | `pyproject.toml` |
| Pre-commit and pre-push hooks | **Built** | `.pre-commit-config.yaml` |
| Line-ending and binary policy | **Built** | `.gitattributes` |
| Settings and secrets | **Built** | `common/config.py` |
| Airport reference data, bbox derivation | **Built** | `common/airports.py`, `config/airports.toml` |
| OpenSky OAuth2 and auto-refresh | **Built** | `ingest/opensky_auth.py` |
| OpenSky `/states/all` and typed parsing | **Built** | `ingest/opensky_client.py` |
| Credit budgeting, retry, backoff | **Built** | `ingest/credit_budget.py`, `ingest/retry.py` |
| METAR / TAF client | Planned (1.4, 1.5) | |
| FAA NAS status client | Planned (1.6) | |
| Aircraft metadata | Planned (1.7) | |
| Bronze Parquet writer, structured logging | Planned (1.8 to 1.10) | |
| Silver layer, schemas, flight segments | Planned (Phase 2) | |
| Go-around and hold detectors, gold features | Planned (Phase 3) | |
| BTS labels, training, MLflow gate | Planned (Phase 4) | |
| Batch scorer, FastAPI | Planned (Phase 5) | |
| Prefect flows | Planned (Phase 6) | |
| CI/CD | Planned (Phase 7) | |
| Prometheus, Grafana, Evidently | Planned (Phase 8) | |
| Docker Compose | Deferred by decision, returns with the first service to containerise | |

**Test suite:** 131 tests, no network, no sleeping, runs in under three seconds.

**Not yet verified against the live API.** Everything above is tested against
mocks built from documented behaviour. The first real call is where endpoint
URLs, header names, and response shapes get confirmed. That is blocked on
registering an OpenSky API client (plan task 0.7).

---

## 12. Decision log

Decisions where the plan was changed, or where a non-obvious option was chosen.
Recorded so the reasoning survives.

| # | Decision | Rationale |
|---|---|---|
| 1 | Defer `docker-compose.yml` instead of stubbing six services in Phase 0 | Debugging empty containers with no payload teaches nothing. Infrastructure arrives with the code that needs it. |
| 2 | One installable package `flight_delay`, not bare top-level `ingest`/`common` | Generic top-level names collide with other installed libraries and make imports ambiguous. |
| 3 | Split config into `.env` and `config/airports.toml` | Deployment settings and domain facts are different things. Nested reference data in env vars is unreviewable. |
| 4 | Derive bounding boxes from coordinates, do not store them | Two stored copies of one fact drift. A bbox that no longer contains its airport looks like "the airport got quiet", not like a bug. |
| 5 | TOML for reference data, not YAML | `tomllib` is stdlib since 3.11, so zero dependencies, and the format is already familiar from `pyproject.toml`. |
| 6 | `httpx` used synchronously, not async | Three requests per 90 seconds is not a concurrency problem. Async would complicate every call site and test for no benefit. `httpx.MockTransport` also removes the need for a mocking dependency. |
| 7 | Units in field names, deviating from the plan's schema names | Metres versus feet is a real and recurring source of silent numeric error. |
| 8 | Hooks call tools via `uv run` rather than pre-commit's tool repos | One version source (`uv.lock`). Separate pins drift and desynchronise hooks from CI. |
| 9 | pytest at pre-push, not pre-commit | Preserves commits as local checkpoints and avoids training people into `--no-verify`. |
| 10 | Line endings handled by `.gitattributes`, not the linter | The hook and `core.autocrlf` were actively undoing each other. Line endings are git's concern. |
| 11 | No committed `requirements.txt` | A derived artifact that nothing regenerates goes stale and eventually installs wrong versions. Generate on demand. |
| 12 | Tolerate longer state arrays, reject shorter ones | Appended optional fields are normal upstream behaviour; refusing them is self-inflicted downtime. Short arrays are unambiguously broken. |
| 13 | `retryable` flag on the exception class, not a list of types in the retry policy | A list maintained away from the raise site drifts, and fails silently in both directions. Default False so new errors are safe until deliberately opted in. |
| 14 | Budget gate inside the retry loop, charging every attempt | Charging only the logical call lets a retry storm overspend the budget that exists to prevent it. |
| 15 | Server `X-Rate-Limit-Remaining` overrides local counting | Local counting cannot see other processes on the same account, and resets on restart. The server knows the truth. |
| 16 | Budget uses the wall clock, token expiry uses monotonic | Different questions. Date rollover is inherently wall-clock; elapsed duration must survive NTP corrections. |
| 17 | Cap `Retry-After` rather than obeying it unbounded | An absurd value would park the poller for hours. Better to fail the cycle and let the scheduler return on its own cadence. |
| 18 | Budget and retry enabled by default in `client_from_settings` | Opt-in safety means the safe configuration is the one you have to remember, and the live API is where forgetting is expensive. |

---

## 13. Known risks

| Risk | Mitigation |
|---|---|
| OpenSky credit exhaustion | **Mitigated.** Proactive daily budget gate, 60 second poll floor enforced at config validation, server credit header reconciled on every response, latched low-water warning. Prometheus metric still to come in 8.1 |
| Label lag: BTS publishes ~2 months late | Accepted for v1. Start ingesting immediately; bootstrap the first model on weather and BTS-derivable features only |
| ADS-B coverage gaps at low altitude | Some landings and go-arounds will be missed. Measure detector coverage rather than assuming completeness |
| Silent upstream schema change | Strict boundary validation and contract tests. `icao24` pattern catches index drift |
| Temporal leakage in training | All splits by date, never random. No feature may include future information |
| METAR station is not always the airport | Modelled as an explicit per-airport field so the exception is representable |
| OpenSky free tier is non-commercial | Fine for this project. Revisit before any commercial use |

---

## 14. Explicitly out of scope for v1

Multi-airport graph modelling of true cascade propagation. Postgres/TimescaleDB
or S3 + Iceberg. Sequence models over raw trajectory windows. Learned anomaly
detection replacing rule-based detectors. Paid schedule APIs for real-time
labels. Kubernetes, a feature store, canary rollouts.
