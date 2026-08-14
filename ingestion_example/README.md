# Ingestion, from scratch

This folder is the **finished reference**: the ingestion component that used to
live at `src/flight_delay/ingest/`, plus its 232 tests. It is here to be read
when you get stuck, not to be run - the modules still import
`flight_delay.common.*`, so they will not execute from this location.

Your job is to rebuild it at `src/flight_delay/ingest/`. What survives in the
live tree is everything ingestion *depends on* but is not part of:

    src/flight_delay/common/   config, airports, logging, time helpers
    config/airports.toml       committed reference data
    tests/                     test_airports, test_config, test_smoke (29 tests, green)

## How to use this guide

Ten steps, in dependency order. Each one ends with **"you are done when"** - a
test you can write and run before moving on. Resist reading ahead into
`ingest/`; try the step first, then compare. The comparison is where the
learning is.

Two ordering choices are deliberate and worth understanding:

- **Storage before network.** You can write and fully test the Parquet writer
  with hand-made dictionaries, no API involved. Getting that solid first means
  that when the network work starts, any bug is in the network layer.
- **Weather before OpenSky.** METAR needs no authentication and returns plain
  JSON. OpenSky needs OAuth2 and a credit budget. Build the easy one end to end
  first so you have a working shape to copy.

---

## Step 1 - Project setup

You already have this, so mostly read and confirm. From an empty directory it
would be:

```bash
uv init --package flight-delay-pipeline
```

The layout that matters:

```
src/flight_delay/     one installable package, not a bare top-level `ingest`
  common/             config, reference data, shared helpers
  ingest/             <- you are building this
tests/
config/               committed reference data
```

`src/` layout specifically: it forces you to install the package to import it,
which means your tests exercise the same import path a user would. A flat
layout lets tests accidentally pass by picking up the working directory.

Add dependencies **with the code that imports them**, never speculatively:

```bash
uv add httpx pydantic-settings pyarrow structlog tenacity
```

Then wire up the tooling once, so it never drifts:

```bash
uv add --dev ruff mypy pytest pre-commit
```

**You are done when** `uv run pytest` runs (zero tests is fine) and
`uv run mypy` passes on an empty package.

---

## Step 2 - Configuration

Everything downstream reads settings, so this comes first.

Build a `Settings` class with **pydantic-settings**, env-prefixed (`FDP_`), with
every value defaulted so the project runs with no `.env` at all. Put the
validation *in the type*: a poll interval below the safe floor should fail at
startup, not at 3am when the quota drains.

Two things worth getting right now rather than later:

- **Secrets use `SecretStr`.** Then assert in a test that the secret does not
  appear in `repr(settings)`. Credentials leak through logs and tracebacks, and
  the fix is much cheaper before there are twenty call sites.
- **Derived paths hang off one root.** `data_dir` → `bronze_dir`. One env var
  moves the whole lake.

**You are done when** you can set `FDP_BRONZE_DIR` in the environment and see it
land in `get_settings()`, and an out-of-range poll interval raises at
construction. Reference: `../src/flight_delay/common/config.py`, already written.

---

## Step 3 - Reference data and geometry

Airports live in committed TOML, loaded into frozen pydantic models. The
interesting part is deriving a bounding box from a centre point and a radius.

The trap: a degree of longitude shrinks as you move away from the equator, so
`radius_nm / 60` is correct for latitude and wrong for longitude. Widen the
longitude span by `1 / cos(latitude)`.

**You are done when** a test asserts the box contains its own airport, that the
latitude span is `2 * radius / 60` degrees, and that the longitude span is
strictly wider at 40°N than the latitude span. Reference:
`../src/flight_delay/common/airports.py`, already written.

---

## Step 4 - The bronze writer (no network yet)

The storage layer, built and tested with dictionaries you type by hand.

Requirements, each of which is a test:

1. **Hive-style partitions**: `{source}/date=YYYY-MM-DD/hour=HH/`. Query engines
   read `date` and `hour` as real columns instead of parsing paths.
2. **Never overwrite.** Filenames carry a timestamp plus a random suffix. Bronze
   is append-only; a rerun that clobbers yesterday is unrecoverable.
3. **Atomic writes.** Write to a temp file, then `os.replace`. A crash mid-write
   must leave no partial Parquet, because a partial file poisons every future
   read of that partition.
4. **Explicit, pinned schemas.** Never infer. A batch where every `sensors`
   value is null infers a different type than one with values, and the two files
   fail to union at query time - months later, far from the cause.
5. **Empty batch writes nothing.** A quiet hour is normal, not an error.
6. **Content hash per record**, so the next layer can deduplicate. It must
   exclude ingestion time and be independent of key order, or the same
   observation polled twice will hash differently and dedup silently fails.

**You are done when** you can write two batches an hour apart, read them back
with `pl.read_parquet`, and see identical hashes for identical observations.
Reference: `ingest/bronze.py`, `tests/test_bronze.py` (19 tests).

---

## Step 5 - Your first real source: METAR

No auth, plain JSON, all stations in one request. The whole shape of a source
in one sitting.

Three modules, and keeping them separate is the point - they change for
different reasons:

```
weather/client.py   HTTP + parse into a typed model     (upstream API changes)
weather/bronze.py   model -> row + the pinned schema    (storage shape changes)
weather/poller.py   one cycle: fetch, map, write        (unit of work changes)
```

Parse into **pydantic models at the boundary**, once. After that, no code
indexes into raw JSON. Real feeds are messier than the docs suggest, so write
tests for: a visibility of `"10+"` (a string where you expect a number), a
variable wind direction, absent optional fields, and `null` where you expect a
list.

Test the client with `httpx.MockTransport`. Inject the `httpx.Client` rather
than constructing it inside your class - that one decision is what makes every
later test possible without a network.

**You are done when** `poll_metar_once` writes a readable Parquet file, and
polling three times produces three files that share one content hash.
Reference: `ingest/weather/`, `tests/test_weather_client.py` (26 tests).

---

## Step 6 - Errors and retry

Now that you have a source that can fail, give failure a shape.

Define an error family with one flag: `retryable`, defaulting to **False**. New
error types are non-retryable until someone deliberately decides otherwise -
that is the safe direction to be wrong in. Put the flag on the exception, not in
a list kept elsewhere; a list maintained at a distance falls out of sync
silently.

Then a retry policy on **tenacity** with three properties:

1. Only retryable failures retry. Hammering bad credentials is how API access
   gets suspended.
2. A server's `Retry-After` beats your own backoff. Guessing shorter earns
   another 429.
3. Jitter, always. Sources that fail together must not retry in lockstep.

**You are done when** tests prove backoff grows and stays under the ceiling,
that `Retry-After` overrides it, and that an auth error is *not* retried.
Reference: `ingest/errors.py`, `ingest/retry.py`, `tests/test_retry.py`.

---

## Step 7 - OpenSky, using `opensky_api`

You have chosen the official library, which is a reasonable trade. Be clear-eyed
about what it does and does not give you.

```bash
uv add opensky-api
```

(It was removed from this project's dependencies, so you will need to re-add it.
It pulls in `requests`.)

**What it saves you.** OAuth2 client-credentials and token refresh via
`TokenManager`, the positional-array-to-named-fields mapping, and a client-side
rate limiter. That is genuinely the two hardest modules in the reference
implementation - `opensky/auth.py` and half of `opensky/client.py` - so this is
real savings.

```python
from opensky_api import OpenSkyApi

api = OpenSkyApi(client_id=..., client_secret=...)
states = api.get_states(bbox=(lamin, lamax, lomin, lomax))  # a 4-tuple, in that order
```

**What you must add yourself.** Three gaps, and the first is serious:

1. **It returns `None` for failure *and* for rate-limit refusal.** From the
   source: `if not self._check_rate_limit(...): return None`, and the docstring
   says "OpenSkyStates if request was successful, `None` otherwise". So `None`
   means rate-limited, or a 5xx, or a network error - indistinguishable. A
   pipeline that writes nothing on `None` loses data and never says why.
   **Wrap it**: turn `None` into an explicit exception carrying what you can
   determine, so Step 6's retry policy has something to classify. This is the
   single most important thing you will write in this step.
2. **No length validation.** `StateVector.__init__` is
   `self.__dict__ = dict(zip(StateVector.keys, arr))`. A short array silently
   yields an object with missing attributes; an inserted upstream field shifts
   every value one slot with no error. Validate what you get back - at minimum
   assert `icao24` matches `^[0-9a-f]{6}$`, which catches a shift immediately.
3. **No credit accounting.** That is Step 8.

Then map the library's `StateVector` to your row dict and write it. Note its
field names differ from the wire (`baro_altitude`, `velocity`), and carry no
units - consider renaming to `baro_altitude_m` and `velocity_ms` in your row
schema. OpenSky reports metres and metres/second while aviation thresholds are
written in feet and knots, and a bare `altitude` invites comparing metres
against a 1,500 ft threshold.

**You are done when** one poll of one airport writes a Parquet file whose
`icao24` values are all six lowercase hex digits, and a forced `None` from the
library raises rather than silently writing zero rows.
Reference: `ingest/opensky/` - read `client.py` for the validation approach even
though you are not writing the HTTP yourself.

---

## Step 8 - The credit budget

OpenSky meters usage in credits that refill daily (~400 anonymous, 4000
registered). At 90 s per poll across 3 airports you spend 2,880/day, so this is
not theoretical.

Build a gate that is **proactive**: it refuses *before* the request, so an
exhausted budget costs nothing. Three details that make it correct:

- **Reset at UTC midnight**, matching the server, not local midnight.
- **Reconcile against the server's `X-Rate-Limit-Remaining` header.** The
  server's count is authoritative; local counting drifts across restarts and
  across processes sharing an account.
- **Charge every attempt, including retries.** Put the gate *inside* the retry
  loop. Charging only the logical call lets a retry storm spend several times
  its share of the budget that exists to prevent exactly that.

Note the library's own rate limiter does not know about your daily credits, so
this is still yours to write.

**You are done when** a test shows a retry storm stops when the budget runs out
mid-storm, and that exhaustion raises *without* touching the network.
Reference: `ingest/opensky/credit_budget.py`, `tests/test_credit_budget.py`.

---

## Step 9 - The scheduler

Four sources, four cadences: OpenSky 90 s, FAA 5 min, METAR 10 min, TAF 60 min.
Polling everything at the fastest rate burns credits for data that has not
changed.

Give each source its own next-due time and sleep only until the earliest one, so
a fast source is never held back by a slow one's interval. Two rules:

- **Use the monotonic clock.** This is "has enough time elapsed", not "what time
  is it". An NTP correction must not make a source look an hour overdue.
- **Poll everything once at startup** rather than waiting out the first
  interval, or a restart means an hour of silence while the TAF timer runs down.

Wrap each source's poll so one failure cannot kill the loop - catching bare
`Exception`, not just your own error type. Ingestion history is unrecoverable;
a parser bug in one source must not stop the other three from collecting.

**You are done when** a test with a fake clock shows a 90 s source ticking 40
times while a 3600 s source ticks once, and a source that always raises does not
stop the others. Reference: `ingest/poller.py`, `tests/test_poller.py`.

---

## Step 10 - Observability

One structured log line per poll, with counts, duration, and credits remaining,
so ingestion health is answerable from logs alone. Use **structlog** with
key-value fields, not f-strings - you want to grep and aggregate these later.

**Persist them.** The reference implementation logs to stdout only, which means
a week of successful collection leaves no evidence it happened. Send them to a
file from day one.

**You are done when** `poll.completed` and `poll.failed` lines land in a file
you can count, and the run summary reports polls and failures per source.

---

## The dependency rule to keep

As it grows, keep imports flowing one way:

```
poller.py (scheduler)  ->  {opensky,weather,faa}/poller.py  ->  context.py
                                                            ->  bronze.py, errors.py, retry.py
```

`opensky/`, `weather/`, and `faa/` never import each other. Anything two sources
both need moves up to the flat layer. A module's location then tells you its
blast radius, and `SourceContext` sits at the flat layer precisely because both
the scheduler and the source pollers use it - putting it next to the scheduler
would make that a cycle.

## Suggested order of attack

Steps 1-4 in one sitting gives you a tested storage layer with no network.
Step 5 gives you a complete working source. Everything after that is hardening
one axis at a time - and each is independently useful, so stopping after Step 6
still leaves you something real.
