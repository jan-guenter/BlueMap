# Simple web throughput comparison

This directory contains a small fixed-concurrency comparison of three HTTP
data paths:

1. the built-in server from an exact upstream revision;
2. that same upstream revision's `sql.php` endpoint behind PHP-FPM and an HTTP
   server;
3. the new standalone Java server.

The runner does not deploy those targets. Keep comparator setup here or in
disposable local infrastructure; it is not part of the production image or
Helm chart.

## Fair setup

All three targets must:

- use distinct direct-origin URLs, without a CDN, caching reverse proxy, or
  workstation HTTP proxy;
- read the same immutable external SQL snapshot;
- receive the same CPU and memory limits and run on otherwise idle comparable
  hardware;
- use the same aggregate database-connection ceiling;
- serve the exact path list supplied to this runner;
- return the same stored content coding, normally `zstd`.

Build the first target from the exact upstream revision and launch its CLI with
the webserver-only option. Serve the unmodified `sql.php` from that same
revision for the second target; changing connection settings is configuration,
not a reason to alter its request logic. Build the third target from the exact
new Java revision. Record immutable revisions or image digests in
`UPSTREAM_ID` and `NEW_JAVA_ID`.

Use existing objects only. The path file is UTF-8, one unique absolute,
canonical URL-safe `/maps/...` path per line. Blank lines and lines beginning
with `#` are ignored. Percent escapes, queries, fragments, missing objects, and
static web-app paths are deliberately rejected. Start from
[paths.example.txt](paths.example.txt), replacing its tile coordinates with
objects that exist in the frozen dataset.

Copy [setup.example.json](setup.example.json), replace every placeholder, and
review it before the run. It records the target runtime/configuration identities,
host and protocol, equal CPU/memory limits, database snapshot, and shared
aggregate connection ceiling. Its `database.snapshotId` must exactly match
`DATASET_ID`; declared CPU and memory limits must match across all three
targets. Keep credentials and other secrets out of this manifest.

Before any timed request, the runner fetches every path from all three targets
without an HTTP proxy. It requires HTTP 200, the configured `Content-Encoding`,
and byte-identical raw response bodies. This prevents a fast error response,
different snapshot, or transparent recompression from becoming a throughput
result.

## Run

Requirements are Python 3.11 or newer and a local `k6` executable.

```bash
export UPSTREAM_URL=http://127.0.0.1:8101
export UPSTREAM_PHP_URL=http://127.0.0.1:8102
export NEW_JAVA_URL=http://127.0.0.1:8103
export UPSTREAM_ID=e664c1abdf697c64703401dca1d7e1956f755f65
export NEW_JAVA_ID=<exact-revision-or-image-digest>
export DATASET_ID=<snapshot-sha256-or-other-immutable-id>
export SETUP_MANIFEST="$PWD/benchmarks/web-throughput/setup.json"
export PATHS_FILE="$PWD/benchmarks/web-throughput/paths.txt"

benchmarks/web-throughput/run.sh
```

Defaults are 12 virtual users, a 15-second warmup, a 60-second measurement,
and three repetitions. Target order rotates on each repetition. Override them
when needed:

```bash
VUS=24 DURATION=2m WARMUP_DURATION=30s REPETITIONS=3 \
OUTPUT_DIR=/tmp/bluemap-throughput \
benchmarks/web-throughput/run.sh
```

Do not raise concurrency until errors appear and then publish only the last
successful run. Choose one concurrency before the comparison and retain every
repetition, including failures.

## Evidence and metrics

The default output is a new timestamped directory under `results/`. It
contains:

- `metadata.json` and `terminal.json`, including terminal validity, target and
  dataset identities, settings, tool versions, and benchmark-file hashes;
- the exact `setup-manifest.json`, its hash, `paths.txt`, and incremental
  `preflight.json`;
- unmodified k6 `--summary-export` JSON when produced, plus a console log for
  every attempted warmup and measurement;
- `runs.json` and `measurements.csv` for individual measurements;
- `summary.json`, `summary.csv`, and `SUMMARY.md` with per-target medians.

The summary reports requests per second, received MiB per second, p95 response
time, and errors. Requests per second and MiB per second should be interpreted
together because object sizes differ. A nonzero warmup exit, measured k6 exit,
HTTP failure rate, missing or malformed evidence, or validation error marks the
result invalid and remains in the evidence. Aggregate metrics are emitted only
when every expected repetition for all three targets is present and valid; an
invalid or incomplete matrix retains its rows but publishes no medians.

Run the benchmark-local tests with:

```bash
python3 -m unittest discover -s benchmarks/web-throughput/tests -p 'test_*.py'
bash -n benchmarks/web-throughput/run.sh
shellcheck benchmarks/web-throughput/run.sh
python3 -m py_compile benchmarks/web-throughput/run_benchmark.py
```

## Limitations

This deliberately small comparison has important limits:

- Fixed-concurrency throughput is not a capacity curve or production sizing
  model.
- One host, database, dataset, object distribution, concurrency, and cache
  state cannot represent every deployment.
- It measures direct-origin map-data reads, not TLS termination, ingress, CDN,
  browser rendering, static assets, or geographically distributed clients.
- Database and operating-system caches warm over time. Warmups and rotated
  order reduce but do not remove that effect.
- The workload does not exercise conditional cache validators, update races,
  writes, slow clients, overload recovery, or horizontal scaling.
- It records no CPU, memory, garbage-collection, database, or energy use and
  therefore cannot support a resource-efficiency conclusion.
- k6 summary exports are aggregate evidence, not per-request traces. They cannot
  reconstruct an individual request timeline after the run.
- The PHP endpoint's per-request connection lifecycle and Java's connection
  pool remain architectural differences even with equal aggregate ceilings.
- Results are comparable only while all correctness preflight checks pass and
  every measured repetition is retained.
