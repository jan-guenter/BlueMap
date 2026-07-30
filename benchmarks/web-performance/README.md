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
- allow only the snapshot-copy Job to mount the existing `minecraft-data` PVC,
  and mount it strictly read-only;
- never patch, restart, scale, replace, or delete `deployment/minecraft`;
- never patch, replace, resize, or delete `pvc/minecraft-data`.

Database, webserver, ingress, load-generator, Secret, ConfigMap, and temporary
PVC resources created by the experiment are disposable. Delete them by their
exact experiment label after evidence has been copied out.

## Immutable snapshot prerequisite

A comparison matrix is valid only when every candidate reads the same
content-addressed disposable snapshot. The import Jobs never mount
`minecraft-data`; they read the `bluemap-perf-snapshot` PVC at `/snapshot`.
Before generating a manifest or starting a case:

1. finish and verify the one-time snapshot-copy Job;
2. ensure no Minecraft/BlueMap writer, importer, migration, or cleanup process
   can write to either benchmark database;
3. mount `bluemap-perf-snapshot` read-only everywhere after the copy Job;
4. generate the request manifest once, archive its SHA-256, and reuse that
   exact file for every variant and repetition;
5. use the same already-imported database instance for every SQL variant in
   one matrix. Do not re-import between variants.

The copy tool inventories normalized relative POSIX paths, byte lengths, and
SHA-256 hashes, copies without metadata-dependent timestamps, re-inventories
the destination, and writes `/snapshot/SNAPSHOT.json`. It refuses symlinks,
non-regular files, or any pre-existing destination entry except
`lost+found`. A partial or failed copy is never resumed: delete only the
disposable snapshot Job/PVC and start again.

Create the tool ConfigMap, PVC, and copy Job:

```shell
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  create configmap bluemap-perf-snapshot-tool \
  --from-file=copy_snapshot.py=benchmarks/web-performance/tools/copy_snapshot.py \
  --dry-run=client -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud label --local -f - \
    app.kubernetes.io/part-of=bluemap-web-performance \
    bluemap.guenter.cloud/experiment-id=immutable-snapshot -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud apply -f -

kubectl --kubeconfig /root/.kube/guenter-cloud apply \
  -f benchmarks/web-performance/kubernetes/snapshot-copy.yaml
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft wait \
  --for=condition=complete --timeout=30m job/bluemap-perf-snapshot-copy
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft logs \
  job/bluemap-perf-snapshot-copy |
  tee benchmarks/web-performance/artifacts/snapshot/copy.log
grep -E \
  '^SNAPSHOT_VERIFIED treeSha256=[0-9a-f]{64} files=[1-9][0-9]* bytes=[1-9][0-9]*$' \
  benchmarks/web-performance/artifacts/snapshot/copy.log
```

The final `grep` is a required gate, not just informational output. Preserve
the log next to the manifest. To retry a failed or stale snapshot, remove only
the disposable targets and then repeat the commands:

```shell
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft delete \
  job/bluemap-perf-snapshot-copy --ignore-not-found
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft delete \
  pvc/bluemap-perf-snapshot --ignore-not-found
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft delete \
  configmap/bluemap-perf-snapshot-tool --ignore-not-found
```

These exact names deliberately exclude `pvc/minecraft-data`.

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

Generate deterministic live data before importing. This produces 32 players
and 64 valid POI markers instead of benchmarking two-byte `{}` responses. The
files and `SHA256SUMS` are generated outside both source PVCs:

```shell
fixture_dir="$(mktemp -d)"
python3 benchmarks/web-performance/tools/generate_live_fixtures.py \
  "$fixture_dir"
(cd "$fixture_dir" && sha256sum --check SHA256SUMS)

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  create configmap bluemap-perf-live-fixtures \
  --from-file=players.json="$fixture_dir/players.json" \
  --from-file=markers.json="$fixture_dir/markers.json" \
  --dry-run=client -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud label --local -f - \
    app.kubernetes.io/part-of=bluemap-web-performance \
    bluemap.guenter.cloud/experiment-id=live-fixtures -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud apply -f -
```

The importer logs both fixture hashes and injects the same payload into every
map without changing either PVC. For file-storage candidates, apply
`kubernetes/file-live-fixtures-values.yaml` as an additional Helm values file;
it projects the same ConfigMap over the two `world/live` files while keeping
the snapshot mount read-only.

Create the importer ConfigMap from the checked-in source and submit the two
database import Jobs:

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

The jobs mount only `bluemap-perf-snapshot` read-only and import
`/snapshot/bluemap/web`. They normalize hires tiles and texture data to one
zstd representation and copy uncompressed low-resolution PNGs, settings,
assets, and the generated markers/player fixtures. Database credentials and
TLS material are mounted from operator-generated Secrets and are never written
to artifacts.

Generate one single-map manifest after the source snapshot is frozen. Selecting
`world` avoids false 404s from benchmarking maps that a candidate was not
configured to serve:

```shell
python3 benchmarks/web-performance/tools/generate_manifest.py \
  /mnt/minecraft/bluemap/web \
  --map-id world \
  --players-fixture "$fixture_dir/players.json" \
  --markers-fixture "$fixture_dir/markers.json" \
  --output benchmarks/web-performance/artifacts/snapshot/manifest.json
sha256sum benchmarks/web-performance/artifacts/snapshot/manifest.json
```

The manifest records `"mapIds": ["world"]`; both the runner and k6 reject map
routes outside that set. It also records both fixture hashes. Generate it while
the source writer is frozen for the one-time copy, then use only the copied
PVC and imported databases. The saved copy log proves which content tree was
frozen; never silently regenerate the manifest later.

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
- the same request `TRACE_SEED`, offered-rate gate, and p95/p99 gates;
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
offered rate, create one seeded base order of the variants for block 1, run
each variant exactly once with `--repetitions 1`, then rotate that order by one
position for each later block. Keep the one-minute cool-down between every
case.

Formal work starts from a frozen machine-readable matrix. Copy and edit the
example once, generate the seeded balanced schedule, validate it, and archive
both hashes before the first run:

```shell
MANIFEST_SHA256=$(sha256sum \
  benchmarks/web-performance/artifacts/snapshot/manifest.json |
  awk '{print $1}')
jq --arg sha256 "$MANIFEST_SHA256" \
  '.manifestSha256 = $sha256' \
  benchmarks/web-performance/matrix.example.json \
  > benchmarks/web-performance/artifacts/snapshot/matrix.json
python3 benchmarks/web-performance/tools/generate_schedule.py generate \
  benchmarks/web-performance/artifacts/snapshot/matrix.json \
  benchmarks/web-performance/artifacts/snapshot/schedule.json
python3 benchmarks/web-performance/tools/generate_schedule.py validate \
  benchmarks/web-performance/artifacts/snapshot/matrix.json \
  benchmarks/web-performance/artifacts/snapshot/schedule.json
sha256sum \
  benchmarks/web-performance/artifacts/snapshot/matrix.json \
  benchmarks/web-performance/artifacts/snapshot/schedule.json
```

Every block contains every case/variant combination exactly once. Each matrix
variant explicitly records its implementation, storage type, database backend,
and replica count. The generator deterministically shuffles case order and
creates a seeded variant base order, then rotates the latter across blocks so
every variant occupies each ordinal position equally, or within one occurrence
when the repetition count is not divisible by the variant count. Its validator
reconstructs the complete schedule and rejects omissions, duplicates,
reordering, position imbalance, or edits. Pass `--matrix`, `--schedule`, the
exact `--schedule-entry`, and all four variant identity flags to every formal
runner invocation. The runner rejects any identity, service shape, case ID,
profile, rate, viewer count, encoding, contract mode, trace seed, or latency
gate that differs from the selected entry. It independently requires the
schedule replica count to equal both the number of named web Pods and the sum
of desired replicas in the named Deployments.

The resulting order has this shape:

```text
block 1: rust-r1, java-new-r1, php-r1, java-old-r1
block 2: java-new-r2, php-r2, java-old-r2, rust-r2
...
block 5: rust-r5, java-new-r5, php-r5, java-old-r5
```

Archive the generated schedule before block 1 and do not reorder it in
response to intermediate results. This interleaving limits time-of-day,
database-cache, and cluster-background-load bias.

Request paths are also deterministic. k6 hashes `TRACE_SEED`, profile,
scenario name, and `exec.scenario.iterationInTest`; it never uses VU-local
randomness. Manifest arrays must be sorted and unique. Keep the same trace
seed for all variants, phases, and repetitions so each scenario iteration
selects the same endpoint class and path even if k6 schedules another VU.

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
The seed request is tagged `traffic=setup`; workload status, TTFB, latency,
and sent/received byte submetrics include only `traffic=workload`.

`map-data-mixed` is the common PHP/Java/Rust origin comparison. The unchanged
PHP endpoint cannot serve the static web UI and therefore must not be compared
with `browser-mixed` or `static`.

## Metrics

Every measured run records:

- offered and achieved requests per second;
- response status counts and failure rate;
- p50, p90, p95, p99, p99.9, maximum, and time to first byte;
- bytes received and sent;
- selected Pod CPU/memory from `metrics.k8s.io`, restarts, image digests, and
  exact ready EndpointSlice membership;
- optional Prometheus target-Pod and selected-node cAdvisor/node-exporter
  CPU, throttling, memory, disk, and network series;
- optional PostgreSQL exporter connection/transaction/block/statement series
  when those exact metrics are present.

JVM heap/GC, Rust allocator/Tokio internals, MariaDB query/lock/redo metrics,
startup timing, image size analysis, and graceful termination are separate
optional experiments unless their artifacts are explicitly captured. They are
not implied by an origin-runner result. Delivery cache status, `Age`, ETag,
and transferred bytes come from the separate executable cache probe described
below, not from the origin runner.

Resource samples, exact manifests, image digests, source SHAs, workload
parameters, and raw k6 output belong in an experiment-specific artifact
directory. Do not commit credentials or unredacted cluster Secrets.

## Reproducible origin-case runner

`tools/run_origin_case.sh` runs one complete origin case against an exact
`bluemap-perf-*` Service. It requires the existing
`bluemap-perf-loadgen` Pod and explicit names for every web Deployment, web
Pod, and, for SQL cases, database Pod. File-storage cases omit
`--database-pod`; it does not discover targets through broad selectors. It
also requires the exact map IDs recorded in the manifest and the names of all
non-secret ConfigMaps that render the tested server configuration. Standard
ConfigMap volume, projected-volume, `envFrom`, and `configMapKeyRef`
references are derived automatically from selected Deployments/Pods, and a
missing explicit `--configmap` rejects the case.

Ready EndpointSlice membership must equal the named web Pods before/after the
case, repetition, correctness, warm-up, measurement, and cool-down boundaries.
It is additionally sampled throughout measurement at the metrics interval;
any change or sampling failure rejects the case.

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
  --case-id map-mixed-r100-java-new-postgresql-b1 \
  --matrix benchmarks/web-performance/artifacts/snapshot/matrix.json \
  --schedule benchmarks/web-performance/artifacts/snapshot/schedule.json \
  --schedule-entry map-mixed-r100/java-new-postgresql/block-1 \
  --variant-id java-new-postgresql \
  --implementation java \
  --storage-type sql \
  --database-backend postgresql \
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
  --trace-seed bluemap-web-performance-v1 \
  --latency-p95-ms 500 \
  --latency-p99-ms 1000 \
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
`--configmap` when a case needs more than one. Omit `--database-pod` for a
file-storage case; database metrics remain active whenever exact database Pods
are provided. ConfigMaps are accepted only when explicitly named; binary data
and private keys are refused, obvious credential fields are redacted, and the
sanitized data hash must remain unchanged for the whole case. Automatic
reference completeness covers standard Pod-spec ConfigMap references;
dynamically resolved application configuration remains an explicit operator
responsibility and the archived specs expose that limitation. Use
`--contract-mode legacy` only for a frozen baseline, such as unchanged PHP or
the pre-enhancement Java server, that intentionally lacks the enhanced
validator contract. Variant ordering comes from the pre-recorded interleaved
schedule outside this single-case runner.

The default measured latency gates are strict `p(95) < 500 ms` and
`p(99) < 1000 ms`. k6 enforces them only on workload-tagged measurement
traffic; the runner independently writes and checks `latency-gate.json`.
Warm-up does not enforce latency. For `large-object` only, explicit
`--large-object-latency-p95-ms` and
`--large-object-latency-p99-ms` overrides are allowed and recorded.

The selected Python must have the `zstandard` package used by the HTTP contract
gate. Set `BENCHMARK_PYTHON`, pass `--python /path/to/venv/bin/python`, or
activate such a virtual environment before running the case.

Prometheus is optional and may instead be set with `PROMETHEUS_URL` and
`PROMETHEUS_STEP_SECONDS`. If it is omitted, the periodic `metrics.k8s.io`
sampler remains active. A cluster-local `SERVICE.NAMESPACE.svc` URL is reached
through an exact Service port-forward; directly reachable HTTP(S) URLs are
queried as supplied. URLs containing credentials are rejected.

When enabled, the runner derives exact nodes from the selected Pods. It
captures selected-Pod cAdvisor metrics, aggregate selected-node container
CPU/throttling/network, node-exporter idle/steal CPU, disk IO and network
joined through `node_uname_info{nodename,instance}`, plus non-target container
CPU on those nodes. Every measurement repetition is rejected if any selected
node has fewer than two background samples or exceeds the configured
non-target CPU range, mean, or maximum. Defaults are 0.5, 2, and 3 cores;
all three are configurable and recorded.

Absolute gates catch a single noisy run, but accepted cases can still have
different background levels. Before comparing variants, group the Prometheus
`nodeNoise.repetitions[].nodes[]` mean/maximum values by schedule block and
node, and reject/re-run a block whose cross-case background baseline is not
comparable. Never tune a threshold after looking at which variant won.

The cluster's MariaDB exporter is disabled. MariaDB parity therefore consists
of database-Pod cAdvisor data and application-visible connection behavior;
do not claim MariaDB query, lock, or redo metrics. Empty PostgreSQL exporter
series remain empty rather than being replaced with broad selectors.

The local `artifacts/<case-id>/` directory contains:

- exact copies and SHA-256 hashes of the manifest, k6/contract scripts,
  runner helpers, workload parameters, and formal matrix/schedule entry when
  configured;
- sanitized before/after Service, Deployment, and Pod specs;
- sanitized explicitly selected ConfigMaps, their content hashes, and a
  before/after immutability check, plus the automatically derived reference
  set used for completeness validation;
- the expected and actual ready EndpointSlice Pod set before and after each
  phase and repetition, plus measurement-time membership samples;
- resolved container image IDs/digests and per-repetition restart counts;
- timestamped CPU and memory samples for the load generator and every selected
  web/database Pod, read directly from `metrics.k8s.io`;
- when enabled, one bounded Prometheus `query_range` bundle for the exact case
  start/end timestamps, containing selected-pod cAdvisor CPU, working set,
  throttling and network series, selected-node/background series, and
  PostgreSQL connections, transactions, blocks and statement execution series
  where those metrics exist;
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
iteration, measured p95/p99 exceeds its gate, selected-node background noise
exceeds its gates, a selected container restarts, a ConfigMap changes, an
EndpointSlice target differs at a boundary/sample, or fewer than the requested
repetitions complete. An empty PostgreSQL series is retained as a valid result
because exporter metric names vary; it is not silently substituted with a
broader query.

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
  -e TRACE_SEED=bluemap-web-performance-v1 \
  -e LATENCY_P95_MS=500 \
  -e LATENCY_P99_MS=1000 \
  -e EXPERIMENT_ID=EXPERIMENT_ID \
  benchmarks/web-performance/k6/bluemap.js
```

Every arrival-rate run has two independent saturation gates:
`dropped_iterations` must be zero and the achieved iteration rate must be at
least `MIN_ACHIEVED_RATE_RATIO` (0.99 by default) times the offered rate. The
runner independently rechecks both values in k6's exported summary and writes
`arrival-gate.json`. Measurement runs also enforce p95/p99 on
`http_req_duration{traffic:workload}` and expose workload-only byte
submetrics; conditional setup traffic is separately tagged and excluded.

## Delivery/cache probe

Origin throughput does not establish browser-cache UX. After the intended
temporary Cloudflare Cache Rule is active, run the executable delivery probe
through the public hostname:

```shell
PROBE_ID="block-1-$(date -u +%Y%m%d%H%M%S)"
python3 benchmarks/web-performance/tools/probe_delivery_cache.py \
  https://bluemap-test.guenter.cloud \
  benchmarks/web-performance/artifacts/snapshot/manifest.json \
  --probe-id "$PROBE_ID" \
  --accept-encoding zstd \
  --require-cloudflare-cache \
  --output \
    "benchmarks/web-performance/artifacts/cache-$PROBE_ID.json"
jq -e '.passed == true' \
  "benchmarks/web-performance/artifacts/cache-$PROBE_ID.json"
```

The unique query key provides a cold cache key without purging unrelated
content. For a representative tile, settings, markers, and player payload, the
probe performs a cold GET, an immediate warm GET, and an `If-None-Match`
revalidation. It records status, duration, `Age`, `CF-Cache-Status`, ETag,
Last-Modified, Cache-Control, Content-Encoding, declared length, transferred
bytes, and stored-byte SHA-256 for every request. It requires stable cold/warm
bodies and ETags, bodyless `304`, and `private,no-store` players. Player
responses must never report `CF-Cache-Status: HIT` or carry `Age`, including
the revalidated response. With `--require-cloudflare-cache`, the tile must
transition from `MISS` to `HIT` and expose `Age`.

Use a new probe ID for each cold sequence and archive the JSON. Remove the
temporary Cache Rule after testing; the probe itself never changes Cloudflare
or Kubernetes state.

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
  /tmp/bluemap-slow-reader.result.json \
  /tmp/bluemap-slow-reader.expected.json \
  /tmp/bluemap-slow-reader.expected-body \
  /tmp/bluemap-slow-reader.expected-headers

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  port-forward "pod/$WEB_POD" 18100:8100 \
  > /tmp/bluemap-slow-reader-port-forward.log 2>&1 &
PORT_FORWARD_PID=$!
for _ in {1..100}; do
  grep -q 'Forwarding from 127.0.0.1:' \
    /tmp/bluemap-slow-reader-port-forward.log && break
  if ! kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
    wait "$PORT_FORWARD_PID"
    exit 1
  fi
  sleep 0.1
done
grep -q 'Forwarding from 127.0.0.1:' \
  /tmp/bluemap-slow-reader-port-forward.log

# Fetch one complete stored representation first. curl dechunks the transfer
# but does not decode zstd because --compressed is deliberately absent.
curl --fail --silent --show-error \
  --header 'Accept-Encoding: zstd' \
  --dump-header /tmp/bluemap-slow-reader.expected-headers \
  --output /tmp/bluemap-slow-reader.expected-body \
  "http://127.0.0.1:18100$LARGE_OBJECT"
EXPECTED_LENGTH=$(wc -c < /tmp/bluemap-slow-reader.expected-body)
EXPECTED_SHA256=$(sha256sum /tmp/bluemap-slow-reader.expected-body |
  awk '{print $1}')
jq -n \
  --arg path "$LARGE_OBJECT" \
  --arg encoding zstd \
  --argjson length "$EXPECTED_LENGTH" \
  --arg sha256 "$EXPECTED_SHA256" \
  '{
    path: $path,
    acceptEncoding: $encoding,
    storedRepresentationLength: $length,
    storedRepresentationSha256: $sha256
  }' > /tmp/bluemap-slow-reader.expected.json

python3 benchmarks/web-performance/tools/slow_reader.py \
  "http://127.0.0.1:18100$LARGE_OBJECT" \
  --accept-encoding zstd \
  --expected-length "$EXPECTED_LENGTH" \
  --expected-sha256 "$EXPECTED_SHA256" \
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

`slow_reader.py` refuses to run without an independently captured expected
length or SHA-256; the canonical procedure supplies both. Archive
`expected.json`, the response headers, and the ready/result JSON. The
temporary expected body may be deleted after its metadata has been archived.

Choose a byte rate that makes the response last several seconds but complete
comfortably inside both the application and Kubernetes grace periods. Archive
the Pod events, termination timestamps, and server logs.
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
