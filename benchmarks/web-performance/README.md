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

## Running k6

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
