# Simple web throughput comparison

This directory contains a small fixed-concurrency comparison of three HTTP
data paths:

1. the built-in server from an exact upstream revision;
2. that same upstream revision's `sql.php` endpoint behind PHP-FPM and an HTTP
   server;
3. the new standalone Java server.

The runner does not deploy those targets. The approved execution topology uses
five disposable, digest-pinned RunPod CPU Pods in one EU region: three target
Pods, one MariaDB Pod, and one load-generator Pod. All measured HTTP originates
on the load-generator Pod and reaches the candidates through direct RunPod
public TCP mappings without an HTTP intermediary. Cloudflare, the Rancher
cluster, and the workstation are outside the
measured path. This benchmark infrastructure is not part of the production
image or Helm chart.

## Fair setup

All three targets must:

- use distinct direct-origin URLs, without a CDN, caching reverse proxy, or
  workstation HTTP proxy;
- read the same immutable, sanitized MariaDB snapshot over identity-verified
  TLS;
- receive the same CPU and memory limits and run on otherwise idle comparable
  hardware;
- use the same aggregate database-connection ceiling of exactly 12;
- serve the exact profile path list supplied to this runner;
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
review it before the run. It records immutable image and Pod identities,
out-of-band process/runtime/configuration hashes, equal CPU/memory limits, the
database TLS identity and snapshot, load-generator hardware, and the shared
aggregate connection ceiling. Freeze the load-generator download cap and each
target's upload cap from the RunPod identities as positive bits-per-second
values; they are admission inputs, not estimates derived from benchmark
traffic. Its `database.snapshotId` must exactly match
`DATASET_ID`; declared CPU and memory limits must match across all targets.
Keep credentials and other secrets out of this manifest.

Before any timed request, the runner fetches every path from all three targets
without an HTTP proxy. It requires HTTP 200, the configured stored encoding,
content type, and byte-identical stored HTTP representations. The frozen zstd
tool inside the immutable load-generator image independently decodes each
representation; its path, version, and executable hash are evidence. Preflight
also requires byte-identical decoded content. It accepts either a correct
`Content-Length` or its standards-compliant absence, and freezes that framing
per target. It rejects Cloudflare and common proxy/cache headers, records ETag
and Last-Modified, and validates an empty `304` response for each validator a
target advertises. A target that does not advertise a validator is recorded as
unsupported rather than being given a synthetic one. These checks prevent a
fast error response, a different snapshot, or transparent recompression from
becoming a throughput result.

## Run

Requirements inside the load-generator image are Python 3.11 or newer and the
pinned `k6` executable. Run the command there, not from the workstation.

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

The approved runner requires 12 virtual users, a 30-second warmup, a 120-second
measurement, and exactly five randomized, rotated blocks. The randomization seed and full
schedule are frozen before preflight. A different profile is a separate run and
must never be pooled with this one. The approved matrix requires five blocks
and exactly 12 VUs:

```bash
DURATION=120s WARMUP_DURATION=30s REPETITIONS=5 \
OUTPUT_DIR=/tmp/bluemap-throughput \
benchmarks/web-throughput/run.sh
```

The approved comparison freezes concurrency at 12 VUs, matching the aggregate
database connection ceiling. Retain every block including failures; do not
search for a favorable concurrency while collecting the reported matrix.

## Evidence and metrics

The default output is a new timestamped directory under `results/`. It
contains:

- `metadata.json` and `terminal.json`, including terminal validity, target and
  dataset identities, settings, tool versions, and benchmark-file hashes;
- the exact `setup-manifest.json`, its hash, `paths.txt`, and incremental
  `preflight.json`;
- unmodified k6 summary JSON, load-generator procfs/disk telemetry, and a
  console log for every attempted warmup and measurement;
- `runs.json` and `measurements.csv` for individual measurements;
- `summary.json`, `summary.csv`, and `SUMMARY.md` with per-target medians.

The summary reports requests per second, normalized stored-representation MiB
per second, diagnostic network-receive MiB per second, p50/p95/p99 response
time, and explicit correctness/error counters. Stored MiB/s is computed by a
custom counter from the preflight-frozen representation sizes and is the
comparable payload-goodput value. Network MiB/s is k6's socket-byte count and
also includes HTTP response headers and chunk framing, so it is diagnostic and
can differ structurally for the PHP endpoint. Requests per second and stored
MiB per second should be interpreted together because object sizes differ. A
nonzero warmup or k6 exit, HTTP/transport/correctness error, dropped iteration,
load-generator saturation, missing or inconsistent summary evidence, low disk
headroom, identity mismatch, incomplete telemetry timing, or insufficient
network-link headroom marks
the run invalid and remains in evidence. Aggregate metrics are emitted only
when all fifteen measurements are present and valid; an incomplete matrix
retains its rows but publishes no medians.

All fifteen warmups and all fifteen measurements are independently admitted
from raw one-second load-generator counters. Each telemetry file retains the
cumulative procfs samples, interval start/end times, interval lengths, phase
and capture boundaries, edge lag, and derived rates. A phase requires at least
90% of its exact duration in intervals (27 for a 30-second warmup and 108 for a
120-second measurement), at least 90% time coverage, no interval longer than
two seconds, and start/end evidence within two seconds of the k6 subprocess
boundaries. The runner recomputes nearest-rank p95 receive bytes per second and
requires it to be no more than 70% of the lower of the frozen load-generator
download and active-target upload caps. Failure is retained per phase and
prevents publication of aggregate results.

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
- Real-time per-request metric streaming is deliberately disabled because its
  CPU, I/O, and disk cost can bias the load generator. Summary counters prove
  every response completed the validation path, but they cannot reconstruct
  the timing of an individual response or a short-lived stall.
- One RunPod region, CPU generation, database, small hot dataset, object
  distribution, concurrency, and cache state cannot represent every
  deployment.
- It measures direct-origin map-data reads. MariaDB uses TLS, while measured
  HTTP deliberately does not include TLS termination, ingress, CDN, browser
  rendering, static assets, or geographically distributed clients.
- Database and operating-system caches warm over time. Warmups and rotated
  order reduce but do not remove that effect.
- Preflight exercises advertised conditional cache validators, but the timed
  primary profile does not model update races, writes, slow clients, or
  overload recovery. Horizontal statelessness is validated separately.
- It records load-generator CPU, memory, and network for saturation and frozen
  link-cap admission. It
  does not provide a comparative candidate resource, garbage-collection,
  database-energy, or cost-efficiency result.
- The link gate observes delivered receive bytes only at the load generator.
  It does not capture target-interface transmit counters, packet loss or TCP
  retransmissions, candidate or MariaDB utilization, or connection-pool
  counters. It can reject insufficient advertised-link headroom, but cannot
  diagnose a bottleneck or attribute a throughput plateau.
- k6 summary exports are aggregate evidence, not per-request traces. They cannot
  reconstruct an individual request timeline after the run.
- Full stored and decoded response bytes are hashed and compared during
  preflight. Timed requests re-check status, encoding, content type, the exact
  target-specific presence or value of `Content-Length`, decoded body length,
  and validators but do not hash every body; k6 transparently decodes zstd for
  JavaScript validation. An
  implementation returning same-length corrupt bytes only during a timed phase
  could therefore evade that phase's checks.
- The PHP endpoint's per-request connection lifecycle and Java's connection
  pool remain architectural differences even with equal aggregate ceilings.
- Results are comparable only while all correctness preflight checks pass and
  every measured repetition is retained.
