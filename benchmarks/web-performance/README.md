# BlueMap web performance experiments

This directory defines reproducible, comparative tests for:

1. the existing PHP-FPM/NGINX SQL endpoint;
2. the existing standalone Java server;
3. the minimally optimized standalone Java server;
4. the standalone Rust server.

The benchmark is deliberately split into origin and delivery tests. Origin
tests target a Kubernetes Service directly and measure the server/database
path. Delivery tests use the public Traefik and Cloudflare path and measure
the user-facing caching result.

## Safety boundary

All Kubernetes resources created by this experiment must:

- use names beginning with `bluemap-perf-`;
- have `app.kubernetes.io/part-of: bluemap-web-performance`;
- have a unique `bluemap.guenter.cloud/experiment-id` label;
- treat the existing `minecraft-data` PVC as read-only;
- never patch, restart, scale, replace, or delete `deployment/minecraft`;
- never patch, replace, resize, or delete `pvc/minecraft-data`.

Database, webserver, ingress, load-generator, Secret, ConfigMap, and temporary
PVC resources created by the experiment are disposable. Delete them by their
exact experiment label after evidence has been copied out.

## Immutable snapshot prerequisite

A comparison matrix is valid only while the source and imported database
snapshot are immutable. Before generating a manifest or starting a case:

1. finish the snapshot import jobs and verify that they succeeded;
2. ensure no Minecraft/BlueMap writer, importer, migration, or cleanup process
   can write to either benchmark database;
3. keep the source `minecraft-data` mount read-only;
4. generate the request manifest once, archive its SHA-256, and reuse that
   exact file for every variant and repetition;
5. use the same already-imported database instance for every SQL variant in
   one matrix. Do not re-import between variants.

The runner records the manifest digest and selected map IDs, but it cannot
prove that an external database writer is absent. That operational freeze is a
prerequisite, not an assumption the results can repair afterwards.

## Provisioning the disposable databases

Generate the short-lived PostgreSQL test certificates, then create the
MariaDB and PostgreSQL instances:

```shell
benchmarks/web-performance/kubernetes/prepare-postgres-tls.sh
kubectl --kubeconfig /root/.kube/guenter-cloud apply \
  -f benchmarks/web-performance/kubernetes/databases.yaml
```

Both databases have a 2 CPU/4 GiB limit and a deliberately lower scheduling
request so they fit alongside existing cluster workloads. They are pinned to
the same node and must not be benchmarked concurrently. Record actual node
utilization and throttling with every run.

Create the importer ConfigMap from the checked-in source and submit the two
snapshot import jobs:

```shell
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  create configmap bluemap-perf-importer \
  --from-file=import_snapshot.py=benchmarks/web-performance/importer/import_snapshot.py \
  --from-file=requirements.txt=benchmarks/web-performance/importer/requirements.txt \
  --dry-run=client -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud label --local -f - \
    app.kubernetes.io/part-of=bluemap-web-performance \
    bluemap.guenter.cloud/experiment-id=snapshot-import -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud apply -f -

kubectl --kubeconfig /root/.kube/guenter-cloud apply \
  -f benchmarks/web-performance/kubernetes/import-jobs.yaml
```

The jobs mount `minecraft-data` read-only and import only
`/snapshot/bluemap/web`. They normalize hires tiles and texture data to one
zstd representation and copy uncompressed low-resolution PNGs, settings,
assets, markers, and player data. Database credentials and TLS material are
mounted from operator-generated Secrets and are never written to artifacts.

Generate one single-map manifest after the source snapshot is frozen. Selecting
`world` avoids false 404s from benchmarking maps that a candidate was not
configured to serve:

```shell
python3 benchmarks/web-performance/tools/generate_manifest.py \
  /mnt/minecraft/bluemap/web \
  --map-id world \
  --output benchmarks/web-performance/artifacts/snapshot/manifest.json
sha256sum benchmarks/web-performance/artifacts/snapshot/manifest.json
```

The manifest records `"mapIds": ["world"]`; both the runner and k6 reject map
routes outside that set.

## Unchanged PHP/PostgreSQL baseline

The PHP baseline uses the released `sql.php` without source changes. Install
the chart with the common PostgreSQL values and the PHP overlay, then apply
the benchmark-only TLS patch:

```shell
helm upgrade --install bluemap-perf-java charts/bluemap-web \
  --kubeconfig /root/.kube/guenter-cloud \
  --namespace minecraft \
  -f benchmarks/web-performance/kubernetes/java-postgresql-values.yaml \
  -f benchmarks/web-performance/kubernetes/php-postgresql-baseline-values.yaml

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft patch \
  deployment bluemap-perf-java-php \
  --type strategic \
  --patch-file benchmarks/web-performance/kubernetes/php-postgresql-tls-patch.yaml

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft scale \
  deployment bluemap-perf-java --replicas 0
```

PHP-FPM receives 800 millicores and NGINX 200 millicores, for the same aggregate
one-CPU/one-GiB envelope used by the other one-replica variants. Scaling the
disposable Java Deployment separately is necessary because the chart rejects a
zero replica count. The patch relies on libpq's
`PGSSLMODE=verify-full` and `PGSSLROOTCERT` settings, so TLS is verified without
adding TLS-specific PDO fields or changing the legacy endpoint.

## Fixed comparison controls

For a valid comparison, every variant in a matrix run uses:

- the same source-data snapshot and generated SQL rows;
- the same stored compression;
- the same database instance and TLS mode;
- the same web pod CPU and memory requests/limits;
- the same aggregate database connection budget;
- the same node placement policy;
- request logging disabled;
- the same HTTP protocol and keep-alive behavior;
- the same warm-up, measurement duration, and workload order;
- no server-side response cache unless that cache is the feature being tested.

The initial resource envelope is one CPU and 1 GiB memory per web replica.
Database resources and placement are recorded with every run rather than
silently assumed.

## Workload phases

Each measured case has:

1. readiness and correctness probes;
2. two minutes of JVM/database warm-up;
3. a five-minute measurement;
4. a one-minute cool-down;
5. one measured repetition per runner invocation.

Short exploratory runs may be used to find saturation points, but they are
not included in final comparative statistics.

Run the five formal repetitions as interleaved blocks. For each workload and
offered rate, create a deterministic randomized order of the variants for
block 1, run each variant exactly once with `--repetitions 1`, then repeat with
a newly randomized order for blocks 2 through 5. Keep the one-minute cool-down
between every case. For example:

```text
block 1: rust-r1, java-new-r1, php-r1, java-old-r1
block 2: java-old-r2, rust-r2, java-new-r2, php-r2
...
block 5: php-r5, java-new-r5, java-old-r5, rust-r5
```

Archive the generated schedule before block 1 and do not reorder it in
response to intermediate results. This interleaving limits time-of-day,
database-cache, and cluster-background-load bias.

### Profiles

| Profile | Purpose |
| --- | --- |
| `static` | Root HTML and fingerprinted web assets |
| `hot-tile` | Repeated access to one representative tile |
| `random-tiles` | Broad tile set to reduce application/DB page-cache locality |
| `large-tile` | Large hires payload and slow-client sensitivity |
| `settings` | Settings objects only |
| `textures` | Texture manifests only, isolated from small settings objects |
| `large-object` | Largest non-tile map object, normally the texture manifest |
| `missing-tile` | Known-absent tile; every request must be exactly `204` |
| `conditional` | One pre-seeded `200`, then workload requests exactly `304` |
| `live-viewers` | Separate evenly scheduled player and marker polling |
| `map-data-mixed` | SQL-comparable map data only; no static web assets |
| `browser-mixed` | Static UI plus the weighted map-data workload |

The harness uses an open-model constant-arrival-rate executor for throughput
profiles so a slow server does not silently reduce offered load. The
`live-viewers` profile uses separate constant-arrival-rate scenarios: player
polls are spread across every second, while marker polls are spread across the
configured interval and start 500 ms later. This avoids synchronized polling
bursts.

All routes known to exist require exactly `200`; random tiles no longer accept
`204`, and ordinary GETs no longer accept `304`. The missing-tile profile alone
requires `204`. The conditional profile performs one setup request per k6
phase, requires its ETag, and sends that validator on every workload request.

`map-data-mixed` is the common PHP/Java/Rust origin comparison. The unchanged
PHP endpoint cannot serve the static web UI and therefore must not be compared
with `browser-mixed` or `static`.

## Metrics

Every measured run records:

- offered and achieved requests per second;
- response status counts and failure rate;
- p50, p90, p95, p99, p99.9, maximum, and time to first byte;
- bytes received and sent;
- web pod CPU, throttling, RSS, restarts, and network traffic;
- Java heap, allocation, garbage collection, virtual threads, and open FDs;
- Rust RSS, allocator/process CPU, open FDs, and Tokio/runtime metrics where
  available without changing the tested build;
- database CPU, memory, storage IO, query rate/latency, active connections,
  locks, and WAL/redo volume;
- startup-to-ready and graceful termination time;
- image size and uncompressed static binary size;
- cache response status, `Age`, `CF-Cache-Status`, ETag, and transferred bytes
  for delivery tests.

Resource samples, exact manifests, image digests, source SHAs, workload
parameters, and raw k6 output belong in an experiment-specific artifact
directory. Do not commit credentials or unredacted cluster Secrets.

## Reproducible origin-case runner

`tools/run_origin_case.sh` runs one complete origin case against an exact
`bluemap-perf-*` Service. It requires the existing
`bluemap-perf-loadgen` Pod and explicit names for every web Deployment, web
Pod, and database Pod; it does not discover targets through broad selectors.
It also requires the exact map IDs recorded in the manifest and the names of
all non-secret ConfigMaps that render the tested server configuration. Before
and after every repetition it verifies that the Service's ready EndpointSlice
Pod set exactly equals the named web Pods.

The runner does not apply, patch, scale, restart, delete, or replace Kubernetes
resources. It only reads exact resources and `metrics.k8s.io`, opens a local
port-forward for the HTTP contract check, and executes k6 in the load-generator
Pod. When explicitly configured, it also opens a read-only port-forward to one
exact cluster-local Prometheus Service. The copied workload files and k6 output
are written to the load-generator Pod's disposable `/artifacts` `emptyDir`.

Use a unique case ID and current, exact Pod names:

```shell
BENCHMARK_PYTHON=/path/to/venv/bin/python \
benchmarks/web-performance/tools/run_origin_case.sh \
  --case-id java-postgresql-map-data-mixed-r1 \
  --service bluemap-perf-java \
  --service-port 8100 \
  --manifest benchmarks/web-performance/artifacts/snapshot/manifest.json \
  --map-id world \
  --configmap bluemap-perf-java-config \
  --configmap bluemap-perf-java-storage \
  --web-deployment bluemap-perf-java \
  --web-pod bluemap-perf-java-POD-SUFFIX \
  --database-pod bluemap-perf-postgres-0 \
  --profile map-data-mixed \
  --rate 100 \
  --accept-encoding zstd \
  --stored-encoding zstd \
  --contract-mode enhanced \
  --warmup 2m \
  --measurement 5m \
  --cooldown-seconds 60 \
  --repetitions 1 \
  --prometheus-url \
    http://rancher-monitoring-prometheus.cattle-monitoring-system.svc:9090 \
  --prometheus-step-seconds 15
```

Repeat `--web-deployment`, `--web-pod`, `--database-pod`, `--map-id`, or
`--configmap` when a case needs more than one. ConfigMaps are accepted only
when explicitly named; binary data and private keys are refused, obvious
credential fields are redacted, and the sanitized data hash must remain
unchanged for the whole case. Use `--contract-mode legacy` only for the
unchanged PHP baseline, which intentionally lacks the enhanced validator
contract. Variant ordering comes from the pre-recorded interleaved schedule
outside this single-case runner.

The selected Python must have the `zstandard` package used by the HTTP contract
gate. Set `BENCHMARK_PYTHON`, pass `--python /path/to/venv/bin/python`, or
activate such a virtual environment before running the case.

Prometheus is optional and may instead be set with `PROMETHEUS_URL` and
`PROMETHEUS_STEP_SECONDS`. If it is omitted, the periodic `metrics.k8s.io`
sampler remains active. A cluster-local `SERVICE.NAMESPACE.svc` URL is reached
through an exact Service port-forward; directly reachable HTTP(S) URLs are
queried as supplied. URLs containing credentials are rejected.

The local `artifacts/<case-id>/` directory contains:

- exact copies and SHA-256 hashes of the manifest, k6/contract scripts,
  runner helpers, and workload parameters;
- sanitized before/after Service, Deployment, and Pod specs;
- sanitized explicitly selected ConfigMaps, their content hashes, and a
  before/after immutability check;
- the expected and actual ready EndpointSlice Pod set before and after each
  repetition;
- resolved container image IDs/digests and per-repetition restart counts;
- timestamped CPU and memory samples for the load generator and every selected
  web/database Pod, read directly from `metrics.k8s.io`;
- when enabled, one bounded Prometheus `query_range` bundle for the exact case
  start/end timestamps, containing selected-pod cAdvisor CPU, working set,
  throttling and network series plus PostgreSQL connections, transactions,
  blocks and statement execution series where those metrics exist;
- the HTTP contract output;
- k6 console output, summary JSON, and raw metric NDJSON for both warmup and
  measurement in every repetition;
- phase timestamps, explicit failure messages, and a final `result.json`.

Literal sensitive environment values and credential-like command arguments are
redacted while Secret references remain visible. Secrets are refused by the
general snapshot helper; explicitly selected ConfigMaps use the stricter
non-secret configuration sanitizer. A result is failed if the HTTP contract
fails, k6 reports a failed check or threshold, an expected artifact is missing,
a metrics sample fails, a configured Prometheus capture fails, the achieved
iteration rate is below the configured fraction of offered load, k6 drops an
iteration, a selected container restarts, a ConfigMap changes, an EndpointSlice
target differs, or fewer than the requested repetitions complete. An empty
PostgreSQL series is retained as a valid result because exporter metric names
vary; it is not silently substituted with a broader query.

## Correctness gates

Performance results are rejected unless the variant passes:

- byte-equivalent decoded bodies for supported representations;
- correct map routing and coordinate parsing;
- `406 Not Acceptable` for an unsupported configured encoding;
- no runtime recompression or alternate stored representation;
- `If-None-Match` precedence over `If-Modified-Since`;
- bodyless `304` with current validators/cache headers;
- exact stored-byte `Content-Length` on `HEAD`;
- `Vary: Accept-Encoding` on encoded data;
- `no-transform` on every map-data response to preserve its stored encoding;
- `private, no-store` for player positions;
- revalidating marker/settings policies;
- `no-store` for missing tiles;
- `no-store` for checked `404` and `405` errors;
- database TLS hostname and CA verification;
- failure with a wrong hostname, unknown CA, or missing required client cert;
- health transition during database loss and recovery;
- clean draining of an in-flight large response on SIGTERM.

Legacy and enhanced correctness are intentionally separate. The unchanged PHP
baseline's `legacy` gate verifies byte-equivalent bodies and exact `200`/`204`
routing for tiles, settings, textures, assets, players, and markers. It does
not pretend that PHP implements validators, encoding negotiation, privacy, or
the new cache policy. The Java/Rust `enhanced` gate includes those
requirements, including HEAD metadata, ETag and Last-Modified behavior, 304
precedence, 406 negotiation, and cache directives. The `conditional` profile
is rejected in legacy mode.

## Low-level k6 invocation

Generate a non-sensitive request manifest for the selected data snapshot, then
run one profile:

```shell
k6 run \
  --summary-export artifacts/EXPERIMENT_ID/summary.json \
  -e BASE_URL=http://SERVICE.NAMESPACE.svc:PORT \
  -e MANIFEST=artifacts/EXPERIMENT_ID/manifest.json \
  -e PROFILE=map-data-mixed \
  -e RATE=100 \
  -e DURATION=5m \
  -e CONTRACT_MODE=enhanced \
  -e MIN_ACHIEVED_RATE_RATIO=0.99 \
  -e EXPERIMENT_ID=EXPERIMENT_ID \
  benchmarks/web-performance/k6/bluemap.js
```

Every arrival-rate run has two independent saturation gates:
`dropped_iterations` must be zero and the achieved iteration rate must be at
least `MIN_ACHIEVED_RATE_RATIO` (0.99 by default) times the offered rate. The
runner independently rechecks both values in k6's exported summary and writes
`arrival-gate.json`.

## Graceful-drain slow-reader check

The origin runner stays read-only. Run the destructive Pod-termination check
separately against one exact disposable `bluemap-perf-*` Pod. This procedure
holds a large response open, sends SIGTERM through normal Kubernetes Pod
deletion, and requires the transferred representation to finish:

```shell
WEB_DEPLOYMENT=bluemap-perf-WEB-DEPLOYMENT
WEB_POD=bluemap-perf-WEB-POD
LARGE_OBJECT=$(jq -r '.largeObject' \
  benchmarks/web-performance/artifacts/snapshot/manifest.json)
rm -f /tmp/bluemap-slow-reader.ready.json \
  /tmp/bluemap-slow-reader.result.json

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  port-forward "pod/$WEB_POD" 18100:8100 \
  > /tmp/bluemap-slow-reader-port-forward.log 2>&1 &
PORT_FORWARD_PID=$!

python3 benchmarks/web-performance/tools/slow_reader.py \
  "http://127.0.0.1:18100$LARGE_OBJECT" \
  --accept-encoding zstd \
  --bytes-per-second 1048576 \
  --initial-delay-seconds 2 \
  --timeout-seconds 90 \
  --ready-file /tmp/bluemap-slow-reader.ready.json \
  --output /tmp/bluemap-slow-reader.result.json &
SLOW_READER_PID=$!

while test ! -s /tmp/bluemap-slow-reader.ready.json; do
  if ! kill -0 "$SLOW_READER_PID" 2>/dev/null; then
    wait "$SLOW_READER_PID"
    exit 1
  fi
  sleep 0.1
done
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  delete pod "$WEB_POD" --grace-period=30 --wait=false

wait "$SLOW_READER_PID"
jq -e '.complete == true' /tmp/bluemap-slow-reader.result.json
kill "$PORT_FORWARD_PID" 2>/dev/null || true
wait "$PORT_FORWARD_PID" 2>/dev/null || true
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  rollout status "deployment/$WEB_DEPLOYMENT" --timeout=120s
```

Choose a byte rate that makes the response last several seconds but complete
comfortably inside both the application and Kubernetes grace periods. Archive
the ready/result JSON, Pod events, termination timestamps, and server logs.
Wait for the replacement Deployment to become fully available before a
measured case. Never run this procedure against `deployment/minecraft` or any
non-disposable Pod.

For public delivery tests, use
`https://bluemap-test.guenter.cloud` and a unique experiment ID. Any temporary
Cloudflare exception must match both that hostname and the generated
`BlueMap-Performance/<experiment-id>` User-Agent, and must be removed after
the run.

The Runpod token is supplied only at runtime. Never put it in this repository,
a Kubernetes Secret, a command-line argument captured in process listings, or
an artifact.
