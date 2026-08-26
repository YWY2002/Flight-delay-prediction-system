# Deploying the ingestion service

EC2 runs the container, an EBS volume holds the live bronze lake, and an hourly
job mirrors it to S3. EBS is a staging buffer, not the archive: `BronzeWriter`
depends on `os.replace` for atomic rewrites, which needs a filesystem. S3 is the
durable copy and the one you query from a laptop.

```
EC2 ──docker──► /mnt/bronze (EBS) ──systemd timer──► S3 ──► DuckDB / Polars / Spark
                  live day file          hourly         durable
```

Nothing here has been run against real AWS. The sync script's guards and the
container itself are tested; bucket names, ARNs and mount steps are not.

## 1. Bucket

```bash
aws s3api create-bucket --bucket YOUR-BUCKET --region ap-southeast-1 \
  --create-bucket-configuration LocationConstraint=ap-southeast-1
aws s3api put-public-access-block --bucket YOUR-BUCKET \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket YOUR-BUCKET \
  --versioning-configuration Status=Enabled
```

Versioning matters more here than in a normal lake. The current day file is
rewritten on every poll and re-uploaded to the **same key** each sync, so
without versioning a corrupted local file overwrites the only good copy.
With it, every hourly sync leaves a recoverable prior version.

That does accumulate: roughly 24 versions a day of a file growing to a couple of
MB. Expire the noncurrent ones so it stays bounded.

```bash
aws s3api put-bucket-lifecycle-configuration --bucket YOUR-BUCKET \
  --lifecycle-configuration '{"Rules":[
    {"ID":"expire-noncurrent","Status":"Enabled","Filter":{"Prefix":"bronze/"},
     "NoncurrentVersionExpiration":{"NoncurrentDays":7}},
    {"ID":"archive-old-bronze","Status":"Enabled","Filter":{"Prefix":"bronze/"},
     "Transitions":[{"Days":90,"StorageClass":"GLACIER_IR"}]}
  ]}'
```

## 2. Data volume

Attach a second EBS volume, **not** the root volume. Root volumes default to
`DeleteOnTermination: true`, and rebuilding the instance is a far more likely
way to lose this data than a disk failure.

```bash
sudo mkfs -t xfs /dev/xvdf                       # first time only
sudo mkdir -p /mnt/bronze
echo '/dev/xvdf /mnt/bronze xfs defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a
```

`nofail` keeps a missing volume from blocking boot, which otherwise leaves you
with an instance you cannot SSH into to fix it.

## 3. Credentials

Put the OpenSky client credentials in SSM, not in a file on the box:

```bash
aws ssm put-parameter --name /flight-delay/opensky-client-id --type SecureString --value 'xxx'
aws ssm put-parameter --name /flight-delay/opensky-client-secret --type SecureString --value 'yyy'
```

Render them into an env file at boot (or on each container start):

```bash
umask 077
{
  echo "FDP_OPENSKY_CLIENT_ID=$(aws ssm get-parameter --name /flight-delay/opensky-client-id --with-decryption --query Parameter.Value --output text)"
  echo "FDP_OPENSKY_CLIENT_SECRET=$(aws ssm get-parameter --name /flight-delay/opensky-client-secret --with-decryption --query Parameter.Value --output text)"
  echo "FDP_AIRPORTS=WSSS"
} | sudo tee /etc/flight-delay.env >/dev/null
```

## 4. IAM

Attach `iam/instance-policy.json` to the EC2 instance role. It grants write to
`bronze/*`, plus the `ListBucket`/`GetObject` that `aws s3 sync` needs to work
out what has changed, plus SSM read for the credentials above.

`iam/reader-policy.json` is for your laptop or a query role: read only, scoped
to the same prefix. Do not reuse the instance role for querying.

Replace `CHANGE-ME-bucket` in both.

## 5. Run the container

```bash
cd ~/Flight-delay-prediction-system
docker compose up -d --build
docker compose ps
docker compose logs -f ingest
```

Compose owns the container name and the restart policy, so use it rather than
`docker run`: `docker run` creates a new container on every call, and two
pollers against one day file means the later atomic rename silently drops the
other's rows.

The build context is the repo root even though the Dockerfile now lives at
`src/flight_delay/data_ingestion/Dockerfile`; `docker-compose.yml` wires that up.

### If `docker compose` is not available

Amazon Linux's `docker` package ships without the Compose plugin, so
`docker compose up -d` fails with `unknown shorthand flag: 'd' in -d`. Install
the plugin (note `uname -m` gives `aarch64` on Graviton):

```bash
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)"   -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
```

Compose v2 then needs **buildx 0.17.0+** for `--build`, and Amazon Linux does
not ship that either (`compose build requires buildx 0.17.0 or later`). Note the
two projects name their release assets differently: compose uses `x86_64` /
`aarch64` so `uname -m` drops straight in, while buildx uses `amd64` / `arm64`
and embeds the version in the filename, so the `latest/download` shortcut does
not work for it.

```bash
BX_VER=$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest   | grep -m1 '"tag_name"' | cut -d'"' -f4)
case "$(uname -m)" in
  x86_64)  BX_ARCH=amd64 ;;
  aarch64) BX_ARCH=arm64 ;;
esac
echo "installing buildx ${BX_VER} for ${BX_ARCH}"
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -fsSL "https://github.com/docker/buildx/releases/download/${BX_VER}/buildx-${BX_VER}.linux-${BX_ARCH}"   -o /usr/local/lib/docker/cli-plugins/docker-buildx
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
docker buildx version
```

buildx is not optional here: the Dockerfile uses `RUN --mount=type=cache` and
`--mount=type=bind`, which only BuildKit understands. The legacy builder fails
to parse them outright.

Or skip Compose entirely. This is the same container, flag for flag:

```bash
docker build -f src/flight_delay/data_ingestion/Dockerfile -t flight-delay-ingest .

docker run -d --name flight-delay-ingest --restart unless-stopped --init   --env-file .env -e FDP_LOG_JSON=false   -v flight-delay-bronze:/data   --health-cmd "find /data/bronze -name '*.parquet' -mmin -10 | grep -q ."   --health-interval 2m --health-timeout 10s --health-retries 3 --health-start-period 5m   --log-opt max-size=10m --log-opt max-file=3   flight-delay-ingest
```

Run that **once**. Afterwards use `docker restart flight-delay-ingest`, never
`docker run` again.

Storage defaults to the named volume `flight-delay-bronze`. To put bronze on the
EBS volume instead, swap the `volumes:` entry in `docker-compose.yml` for
`- /mnt/bronze:/data`, which is what the sync in the next section reads.

## 6. Hourly sync

```bash
sudo install -m 0755 sync-to-s3.sh /usr/local/bin/sync-to-s3.sh
sudo sed -i 's|s3://CHANGE-ME-bucket/bronze|s3://YOUR-BUCKET/bronze|' flight-delay-sync.service
sudo cp flight-delay-sync.service flight-delay-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flight-delay-sync.timer
```

Check it:

```bash
systemctl list-timers flight-delay-sync.timer
sudo systemctl start flight-delay-sync.service   # run once now
journalctl -u flight-delay-sync.service -n 20
```

Prefer cron? The timer is only there for logging and catch-up:

```
17 * * * * BRONZE_DIR=/mnt/bronze S3_URI=s3://YOUR-BUCKET/bronze /usr/local/bin/sync-to-s3.sh
```

### What the script refuses to do

Both are deliberate, and both are tested:

- **No `--delete`.** An unmounted volume would otherwise propagate an empty
  local tree into S3 and destroy the only durable copy.
- **Exits non-zero rather than reporting success on an empty tree.** A sync that
  cheerfully uploads nothing for a month is how you find out at training time.

It also excludes `*.tmp`, the scratch file `BronzeWriter` renames over the real
one. Any `.parquet` on disk is complete; a `.tmp` is not.

## 7. Reading the data

```sql
INSTALL httpfs; LOAD httpfs;
CREATE SECRET (TYPE s3, PROVIDER credential_chain, REGION 'ap-southeast-1');
SELECT day, count(*) FROM read_parquet('s3://YOUR-BUCKET/bronze/opensky/**/*.parquet',
                                       hive_partitioning=true)
WHERE year=2026 AND month=8 GROUP BY day ORDER BY day;
```

Point readers at `bronze/opensky`, never at `bronze/`. A reader given the parent
does not error: it infers a schema from whichever source it meets first
(alphabetically METAR) and returns every OpenSky row as nulls.

For Spark, note that PySpark bundles **no** S3 jars. You need `hadoop-aws`
matching the bundled Hadoop (3.5.0 for pyspark 4.2.0) and the `s3a://` scheme,
not `s3://`.
