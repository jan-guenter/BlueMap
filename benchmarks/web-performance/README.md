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
5. five repetitions in randomized variant order.

Short exploratory runs may be used to find saturation points, but they are
not included in final comparative statistics.

### Profiles

| Profile | Purpose |
| --- | --- |
| `static` | Root HTML and fingerprinted web assets |
| `hot-tile` | Repeated access to one representative tile |
| `random-tiles` | Broad tile set to reduce application/DB page-cache locality |
| `large-tile` | Large hires payload and slow-client sensitivity |
| `conditional` | Initial `200`, followed by `If-None-Match` revalidation |
| `live-viewers` | Player polling every second and marker polling every ten seconds |
| `browser-mixed` | Weighted static, map metadata, tile, asset, and live traffic |

The harness uses an open-model constant-arrival-rate executor for throughput
profiles so a slow server does not silently reduce offered load. The
`live-viewers` profile uses constant virtual users because its unit is a
viewer with fixed polling intervals.

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
  --case-id java-postgresql-browser-mixed-001 \
  --service bluemap-perf-java \
  --service-port 8100 \
  --manifest benchmarks/web-performance/artifacts/snapshot/manifest.json \
  --web-deployment bluemap-perf-java \
  --web-pod bluemap-perf-java-POD-SUFFIX \
  --database-pod bluemap-perf-postgres-0 \
  --profile browser-mixed \
  --rate 100 \
  --accept-encoding zstd \
  --stored-encoding zstd \
  --contract-mode enhanced \
  --warmup 2m \
  --measurement 5m \
  --cooldown-seconds 60 \
  --repetitions 5 \
  --prometheus-url \
    http://rancher-monitoring-prometheus.cattle-monitoring-system.svc:9090 \
  --prometheus-step-seconds 15
```

Repeat `--web-deployment`, `--web-pod`, or `--database-pod` for a
horizontally scaled case. Use `--contract-mode legacy` only for the unchanged
PHP baseline, which intentionally lacks the enhanced validator contract.
Variant ordering is randomized by the matrix operator outside this
single-case runner.

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
redacted while Secret references remain visible. Secrets and ConfigMaps are
refused by the snapshot helper. A result is failed if the HTTP contract fails,
k6 reports a failed check or threshold, an expected artifact is missing, a
metrics sample fails, a configured Prometheus capture fails, a selected
container restarts, or fewer than the requested repetitions complete. An empty
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
- `Vary: Accept-Encoding` on encoded data;
- `private, no-store` for player positions;
- revalidating marker/settings policies;
- `no-store` for missing tiles;
- database TLS hostname and CA verification;
- failure with a wrong hostname, unknown CA, or missing required client cert;
- health transition during database loss and recovery;
- clean draining of an in-flight large response on SIGTERM.

## Low-level k6 invocation

Generate a non-sensitive request manifest for the selected data snapshot, then
run one profile:

```shell
k6 run \
  --summary-export artifacts/EXPERIMENT_ID/summary.json \
  -e BASE_URL=http://SERVICE.NAMESPACE.svc:PORT \
  -e MANIFEST=artifacts/EXPERIMENT_ID/manifest.json \
  -e PROFILE=browser-mixed \
  -e RATE=100 \
  -e DURATION=5m \
  -e EXPERIMENT_ID=EXPERIMENT_ID \
  benchmarks/web-performance/k6/bluemap.js
```

For public delivery tests, use
`https://bluemap-test.guenter.cloud` and a unique experiment ID. Any temporary
Cloudflare exception must match both that hostname and the generated
`BlueMap-Performance/<experiment-id>` User-Agent, and must be removed after
the run.

The Runpod token is supplied only at runtime. Never put it in this repository,
a Kubernetes Secret, a command-line argument captured in process listings, or
an artifact.
