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
docker run -d --name flight-delay-ingest --restart=unless-stopped --init \
  --env-file /etc/flight-delay.env \
  -v /mnt/bronze:/data \
  YOUR-ECR-REPO/flight-delay-ingest:latest
```

`--restart=unless-stopped` matters: the scheduler exits deliberately on a
non-retryable error, and you want it back after a reboot or once you have fixed
the cause. `--init` gives a real PID 1 to reap strays; the image already uses an
exec-form CMD so SIGTERM reaches Python directly (verified: `docker stop`
returns in ~800 ms with exit code 0, not a 10 s SIGKILL).

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
