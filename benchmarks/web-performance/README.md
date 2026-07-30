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

All explicitly managed Kubernetes resources created by this experiment must:

- use names beginning with `bluemap-perf-`;
- have `app.kubernetes.io/part-of: bluemap-web-performance`;
- have a unique `bluemap.guenter.cloud/experiment-id` label;
- allow only the snapshot-copy Job to mount the existing `minecraft-data` PVC,
  and mount it strictly read-only;
- never patch, restart, scale, replace, or delete `deployment/minecraft`;
- never patch, replace, resize, or delete `pvc/minecraft-data`;
- never patch, restart, replace, or delete
  `pod/minecraft-maintenance-holder`.

Database, webserver, ingress, load-generator, Secret, ConfigMap, and temporary
PVC resources created by the experiment are disposable. Helm and database
operators do not reliably propagate both labels to every generated child
Service, ConfigMap, Secret, StatefulSet, Pod, or PVC, so a label selector is
not a complete cleanup mechanism. After evidence has been copied out, remove
exact Helm release names and exact operator parent resources, then review an
explicit `bluemap-perf-*` allowlist for generated children. Never use a broad
namespace-wide or prefix-only deletion.

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

Generate the short-lived PostgreSQL test certificates with the explicit
benchmark kubeconfig, then create the MariaDB and PostgreSQL instances:

```shell
BLUEMAP_BENCHMARK_KUBECONFIG=/root/.kube/guenter-cloud \
  benchmarks/web-performance/kubernetes/prepare-postgres-tls.sh

mariadb_mtls_password="$(openssl rand -hex 24)"
kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  create secret generic bluemap-perf-mariadb-mtls \
  --from-literal=username=bluemap_mtls \
  --from-literal=password="$mariadb_mtls_password" \
  --dry-run=client -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud label --local -f - \
    k8s.mariadb.com/watch=true \
    app.kubernetes.io/part-of=bluemap-web-performance \
    bluemap.guenter.cloud/experiment-id=bootstrap -o yaml |
  kubectl --kubeconfig /root/.kube/guenter-cloud apply -f -

kubectl --kubeconfig /root/.kube/guenter-cloud apply \
  -f benchmarks/web-performance/kubernetes/databases.yaml
```

Both databases have a 2 CPU/4 GiB limit and a deliberately lower scheduling
request so they fit alongside existing cluster workloads. They are pinned to
the same node and must not be benchmarked concurrently. Record actual node
utilization and throttling with every run. The MariaDB operator reconciles the
`bluemap_mtls` account from the generated Secret, requires a valid client X.509
certificate, and grants only `SELECT` on the disposable `bluemap` database.

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
`kubernetes/file-live-fixtures-values.yaml` as the last Helm values file. Helm
replaces list values, so this self-contained overlay declares both the
read-only snapshot mount and the two `world/live` ConfigMap projections. It
serves the immutable source snapshot with its configured gzip representation;
the SQL importer separately normalizes its benchmark rows to zstd.

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

Both legacy and optimized Java benchmark values set
`-XX:MaxRAMPercentage=70.0` through `JAVA_TOOL_OPTIONS`. The JVM otherwise
defaults to a heap near 25% of the container limit, which cannot hold the
concurrent 19 MiB SQL responses in the large-object workload. Using the same
percentage gives both Java implementations the same heap-to-container ratio
while leaving 30% for metaspace, thread stacks, direct buffers, and other native
memory. This changes only the disposable benchmark deployments.

## Published optimized Java and Rust candidates

Install optimized Java and Rust from the immutable OCI chart produced by the
same full commit as their images. Do not install these benchmark values from a
local chart or a branch alias: both image tags are deliberately empty and
therefore resolve to the packaged chart's `appVersion`,
`sha-<full-40-character-commit>`.

```shell
FULL_SHA=FULL_40_CHARACTER_COMMIT_SHA
CHART_VERSION="0.1.0-dev.sha.$FULL_SHA"
CHART=oci://ghcr.io/jan-guenter/charts/bluemap-web

test "$(helm show chart "$CHART" --version "$CHART_VERSION" |
  awk '$1 == "appVersion:" {print $2}')" = "sha-$FULL_SHA"

helm upgrade --install bluemap-perf-java-new-postgresql "$CHART" \
  --version "$CHART_VERSION" \
  --kubeconfig /root/.kube/guenter-cloud \
  --namespace minecraft \
  -f benchmarks/web-performance/kubernetes/java-optimized-postgresql-values.yaml

helm upgrade --install bluemap-perf-rust-postgresql "$CHART" \
  --version "$CHART_VERSION" \
  --kubeconfig /root/.kube/guenter-cloud \
  --namespace minecraft \
  -f benchmarks/web-performance/kubernetes/rust-postgresql-values.yaml
```

The two base files produce exact Deployment names and experiment labels,
verify PostgreSQL TLS against the existing `bluemap-perf-postgres-ca` Secret,
and reference credentials from the existing `bluemap-perf-postgres` Secret.
They contain no credential values. Each web replica has matching one-CPU and
one-GiB requests and limits and is pinned to the benchmark web node.

For horizontal scaling, install separate three-replica releases with the
corresponding overlay:

```shell
helm upgrade --install bluemap-perf-java-new-postgresql-r3 "$CHART" \
  --version "$CHART_VERSION" \
  --kubeconfig /root/.kube/guenter-cloud \
  --namespace minecraft \
  -f benchmarks/web-performance/kubernetes/java-optimized-postgresql-values.yaml \
  -f benchmarks/web-performance/kubernetes/java-optimized-postgresql-r3-values.yaml

helm upgrade --install bluemap-perf-rust-postgresql-r3 "$CHART" \
  --version "$CHART_VERSION" \
  --kubeconfig /root/.kube/guenter-cloud \
  --namespace minecraft \
  -f benchmarks/web-performance/kubernetes/rust-postgresql-values.yaml \
  -f benchmarks/web-performance/kubernetes/rust-postgresql-r3-values.yaml
```

The one-replica values allow 12 database connections. The replica-three
overlays allow four per process, preserving the same aggregate 12-connection
budget. Rust admits 24 in-flight HTTP responses for r1 and eight per process
for r3, preserving a separate aggregate HTTP ceiling of 24 while allowing
response streaming to overlap database work. The PHP baseline sets
`pm.max_children` to 12 because each active request owns one transient PDO
connection. Each r3 process keeps the one-CPU/one-GiB limit, but requests 500
millicores and 512 MiB so three replicas can be scheduled on the fixed
benchmark node. The exact requests, limits, utilization, and node noise are
recorded with every run. Run only one candidate release at a time during
measurement.

Use the same immutable chart to deploy the separate MariaDB mutual-TLS
correctness candidate:

```shell
helm upgrade --install bluemap-perf-rust-mariadb-mtls "$CHART" \
  --version "$CHART_VERSION" \
  --kubeconfig /root/.kube/guenter-cloud \
  --namespace minecraft \
  --values benchmarks/web-performance/kubernetes/rust-mariadb-mtls-values.yaml
```

This candidate is not part of the PostgreSQL performance matrix. It verifies
the MariaDB backend, full server-certificate validation, and client-certificate
authentication against the operator-managed `bluemap_mtls` account.

## Fixed comparison controls

For a valid comparison, every variant in a matrix run uses:

- the same source-data snapshot and generated SQL rows;
- the same stored compression;
- the same database instance and TLS mode;
- the same web pod CPU and memory limits within each r1/r3 comparison;
- explicit, fixed scheduling requests recorded for every variant;
- the same aggregate database connection budget;
- the same node placement policy;
- request logging disabled;
- the same HTTP protocol and keep-alive behavior;
- the same warm-up, measurement duration, and workload order;
- the same request `TRACE_SEED`, offered-rate gate, and p95/p99 gates;
- no server-side response cache unless that cache is the feature being tested.

The runtime resource envelope is one CPU and 1 GiB memory per web replica.
One-replica candidates request that full envelope; three-replica candidates
use the documented lower scheduling requests while retaining identical
limits. Database resources and placement are recorded with every run rather
than silently assumed.

The calibrated common baseline and live-viewer cases use 15 requests or
viewers per second. The optimized horizontal-scaling case uses 40 requests per
second: a preflight at that rate completed without shedding after the bounded
Rust HTTP admission ceiling was separated from its smaller database pool,
while higher exploratory rates crossed the predeclared no-error gate. The
large-object case uses one 19 MiB response per second.

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
both hashes before the first run. `matrix.example.json` deliberately contains
invalid `REPLACE_WITH_...` values and cannot generate a schedule as checked
in. This prevents an unresolved example from being mistaken for a formal
matrix. `matrix.schema.json` and `schedule.schema.json` describe format
version 3; `generate_schedule.py` additionally enforces ordering,
cross-reference, balance, and placeholder rules.

First deploy each frozen candidate and collect its expected runtime identity
with read-only queries. `pod-images` includes every normal, init, and
ephemeral container and uses the resolved `status.*ContainerStatuses[].imageID`
digest, not an image tag or registry index digest:

```shell
benchmark_identity_dir="$(mktemp -d)"
variant_id=java-new-postgresql
web_pod=bluemap-perf-java-new-postgresql-POD-SUFFIX

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  get pod "$web_pod" -o json |
  python3 benchmarks/web-performance/tools/runtime_identity.py pod-images \
  > "$benchmark_identity_dir/$variant_id-images.json"

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  get configmap \
    bluemap-perf-java-new-postgresql-config \
    bluemap-perf-java-new-postgresql-storage \
    -o json |
  python3 benchmarks/web-performance/tools/runtime_identity.py configmaps \
  > "$benchmark_identity_dir/$variant_id-config.json"

kubectl --kubeconfig /root/.kube/guenter-cloud -n minecraft \
  get \
    service/bluemap-perf-java-new-postgresql \
    deployment/bluemap-perf-java-new-postgresql \
    -o json |
  python3 benchmarks/web-performance/tools/runtime_identity.py runtime-specs \
  > "$benchmark_identity_dir/$variant_id-runtime-spec.json"
```

Repeat this for every variant using exactly the ConfigMaps that will be passed
through its repeated `--configmap` arguments. All selected replicas of one
variant must report the same complete image array. For configuration, the
helper first applies the benchmark ConfigMap sanitizer. It hashes canonical
JSON for each sanitized `.data` object, sorts the
`{name, sanitizedDataSha256}` entries, then hashes that canonical array.
ConfigMap names are therefore part of the identity; capture timestamps,
resource versions, labels, and Secrets are not. The output's
`sanitizedConfigSha256` is the variant's
`expectedSanitizedConfigSha256`. The runtime-spec identity separately hashes
the exact Service routing and load-balancing fields plus each selected
Deployment selector and sanitized Pod template. It excludes only the assigned
`clusterIP`/`clusterIPs` values while including whether the Service is
headless, its address-family policy, external and load-balancer IPs, and any
health-check node port. Deployment replica counts and Secret contents are
excluded; replica counts are checked independently against the schedule.
Literal sensitive environment values are redacted, while Secret names and keys
remain part of the identity. Its `sanitizedRuntimeSpecSha256` is the variant's
`expectedSanitizedRuntimeSpecSha256`.

Copy the example, replace `benchmarkGitRevision` with the exact checked-out
40-character commit, replace `manifestSha256`, and set each variant's complete
`expectedImages` array, `expectedSanitizedConfigSha256`, and
`expectedSanitizedRuntimeSpecSha256` from those helper outputs. Do not edit the
tracked example in place because formal runs require a clean tracked worktree:

```shell
benchmark_git_revision="$(git rev-parse --verify 'HEAD^{commit}')"
manifest_sha256="$(sha256sum \
  benchmarks/web-performance/artifacts/snapshot/manifest.json |
  awk '{print $1}')
cp benchmarks/web-performance/matrix.example.json \
  benchmarks/web-performance/artifacts/snapshot/matrix.json

# Resolve every REPLACE_WITH_... value in the copied matrix. For each variant,
# paste the entire *-images.json array, the corresponding config output's
# sanitizedConfigSha256, and the runtime-spec output's
# sanitizedRuntimeSpecSha256. Then set the two common identities:
matrix_tmp="$(mktemp)"
jq \
  --arg revision "$benchmark_git_revision" \
  --arg manifest "$manifest_sha256" \
  '.benchmarkGitRevision = $revision | .manifestSha256 = $manifest' \
  benchmarks/web-performance/artifacts/snapshot/matrix.json \
  > "$matrix_tmp"
mv "$matrix_tmp" \
  benchmarks/web-performance/artifacts/snapshot/matrix.json

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
replica count, resolved image identities, and sanitized rendered-configuration
and runtime-spec identities. The exact benchmark Git revision is copied into
the schedule and every entry. The generator deterministically shuffles case
order and creates a seeded variant base order, then rotates the latter across
blocks so every variant occupies each ordinal position equally, or within one
occurrence when the repetition count is not divisible by the variant count.
Its validator reconstructs the complete schedule and rejects omissions,
duplicates,
reordering, position imbalance, or edits. Pass `--matrix`, `--schedule`, the
exact `--schedule-entry`, and all four variant identity flags to every formal
runner invocation. The runner rejects any identity, service shape, case ID,
profile, rate, viewer count, encoding, contract mode, trace seed, or latency
gate that differs from the selected entry. It independently requires the
schedule replica count to equal both the number of named web Pods and the sum
of desired replicas in the named Deployments. Before any correctness request,
warm-up, or measured load, it also requires a clean tracked worktree at the
scheduled commit, verifies the committed runner/workload/helper bytes, captures
the selected Service, Deployments, Pods, and ConfigMaps, verifies each Pod's
exact Pod-to-ReplicaSet-to-selected-Deployment controller chain and UID, and
requires the actual image, sanitized configuration, and runtime-spec identities
to exactly match the entry. A mismatch is written to
`cluster/runtime-identity-before.json` and aborts the run before load
generation. The runner rechecks every selected web Pod's complete normal,
init, and ephemeral-container image identity from each live restart snapshot,
including immediately before the restart-count baseline and after load. This
prevents a restart and mutable-tag repull between the initial snapshot and
measurement from silently changing the tested image. Every sample must also
retain the original Pod UID, and that UID remains part of restart-count
comparisons, so a same-name Pod replacement cannot reset the baseline.

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
and iteration metrics include only `traffic=workload`. k6's aggregate
sent/received byte totals include the one setup response and are reported as
such rather than as a nonexistent request-tagged submetric.

`map-data-mixed` is the common PHP/Java/Rust origin comparison. The unchanged
PHP endpoint cannot serve the static web UI and therefore must not be compared
with `browser-mixed` or `static`.

## Metrics

Every measured run records:

- offered iterations per second and completed iterations divided by the
  configured phase duration;
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
any change or sampling failure rejects the case. Each named web Pod must also
be controlled by a ReplicaSet controlled by one of the exact named
Deployments, with matching UIDs, the Deployment's current revision, and
per-Deployment desired replica counts. Each Deployment must have fully
converged on its current Pod template; paused, rolling, or surplus old-revision
Pods reject the case.

The runner does not apply, patch, scale, restart, delete, or replace Kubernetes
resources. It only reads exact resources and `metrics.k8s.io`, opens a local
port-forward for the HTTP contract check, and executes k6 in the load-generator
Pod. It copies the manifest and workload into that Pod with `kubectl cp` and
verifies their SHA-256 hashes before use. When explicitly configured, it also
opens a read-only port-forward to one exact cluster-local Prometheus Service.
The copied workload files and k6 output are written to the load-generator
Pod's disposable `/artifacts` `emptyDir`.

Use a unique case ID and current, exact Pod names:

```shell
BENCHMARK_PYTHON=/path/to/venv/bin/python \
benchmarks/web-performance/tools/run_origin_case.sh \
  --case-id map-mixed-r15-java-new-postgresql-b1 \
  --matrix benchmarks/web-performance/artifacts/snapshot/matrix.json \
  --schedule benchmarks/web-performance/artifacts/snapshot/schedule.json \
  --schedule-entry map-mixed-r15/java-new-postgresql/block-1 \
  --variant-id java-new-postgresql \
  --implementation java \
  --storage-type sql \
  --database-backend postgresql \
  --service bluemap-perf-java-new-postgresql \
  --service-port 8100 \
  --manifest benchmarks/web-performance/artifacts/snapshot/manifest.json \
  --map-id world \
  --configmap bluemap-perf-java-new-postgresql-config \
  --configmap bluemap-perf-java-new-postgresql-storage \
  --web-deployment bluemap-perf-java-new-postgresql \
  --web-pod bluemap-perf-java-new-postgresql-POD-SUFFIX \
  --database-pod bluemap-perf-postgres-0 \
  --profile map-data-mixed \
  --rate 15 \
  --trace-seed bluemap-web-performance-v1 \
  --latency-p95-ms 10000 \
  --latency-p99-ms 20000 \
  --pre-allocated-vus 256 \
  --max-vus 512 \
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

The command defaults remain strict `p(95) < 500 ms` and `p(99) < 1000 ms`,
but the formal matrix uses deliberately broad, predeclared latency ceilings.
Those ceilings detect a wedged server or invalid run; they are harness-integrity
guardrails, not ranking SLOs. Results that exceed a ceiling remain reportable
and must not be selectively discarded or rerun based on which implementation
was slower. k6 enforces the configured ceilings only on workload-tagged
measurement traffic; the runner independently writes and checks
`latency-gate.json`. Warm-up does not enforce latency. For `large-object` only,
explicit
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
non-target CPU range, mean, or maximum. Defaults are 0.5, 3, and 4 cores;
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
- the scheduled benchmark revision plus the expected/actual resolved image and
  sanitized rendered-configuration identities checked before load generation;
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

The origin-case runner directly enforces the HTTP contract and steady-state
health checks. TLS/mTLS negative cases, database loss/recovery, browser UI
behavior, and graceful drain are destructive or fault-injection experiments,
so they are run separately and archived under the experiment's correctness
artifacts. A formal matrix result is not accepted for the final comparison
until those predeclared receipts exist; `run_origin_case.sh` does not claim to
perform those faults itself.

The HTTP checker emits one JSON diagnostic event when each request starts,
when its response headers arrive, and when its body finishes. If a request
fails, the final event identifies the request path and whether it failed while
opening the response or reading its body. Query strings and exception messages
are deliberately excluded so correctness artifacts do not expose credentials.

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
  -e RATE=15 \
  -e DURATION=5m \
  -e CONTRACT_MODE=enhanced \
  -e MIN_ACHIEVED_RATE_RATIO=0.99 \
  -e TRACE_SEED=bluemap-web-performance-v1 \
  -e LATENCY_P95_MS=10000 \
  -e LATENCY_P99_MS=20000 \
  -e PRE_ALLOCATED_VUS=256 \
  -e MAX_VUS=512 \
  -e EXPERIMENT_ID=EXPERIMENT_ID \
  benchmarks/web-performance/k6/bluemap.js
```

Every arrival-rate run has two independent saturation gates:
`dropped_iterations` must be zero, and each workload scenario must complete at
least its offered rate times the configured phase duration times
`MIN_ACHIEVED_RATE_RATIO` (0.99 by default). k6 enforces scenario-tagged
iteration-count thresholds. The runner independently rechecks the exported
counts and writes `arrival-gate.json`. It derives achieved throughput as
completed iterations divided by the configured phase duration; k6's
wall-clock `iterations.rate` is retained only as a diagnostic because it
includes a slow final request or graceful tail after scheduling has stopped.

For `live-viewers-r15`, 15 player polls per second are combined with 1.5 marker
polls per second because markers are requested once per viewer every ten
seconds. Player and marker polling are checked independently, and
their completed counts must sum exactly to k6's overall iteration count. A
well-performing scenario therefore cannot hide an under-delivering peer.
Measurement runs also enforce p95/p99 on
`http_req_duration{traffic:workload}`. Conditional setup traffic is separately
tagged and excluded from the workload latency and status metrics.

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
separately with the guarded helper against one exact disposable
`bluemap-perf-*` Pod. Supply the exact Deployment and Pod names shown by the
recorded case inventory; do not use a selector or a generated shell pipeline:

```shell
WEB_DEPLOYMENT=bluemap-perf-java-new-postgresql
WEB_POD=bluemap-perf-java-new-postgresql-REPLICASET-POD
EXPERIMENT_ID=java-new-postgresql

python3 benchmarks/web-performance/tools/run_guarded_slow_reader.py \
  --kubeconfig /root/.kube/guenter-cloud \
  --namespace minecraft \
  --deployment "$WEB_DEPLOYMENT" \
  --pod "$WEB_POD" \
  --experiment-id "$EXPERIMENT_ID" \
  --confirm-delete-pod "$WEB_POD" \
  --manifest benchmarks/web-performance/artifacts/snapshot/manifest.json \
  --artifact-dir \
    "benchmarks/web-performance/artifacts/drain/$EXPERIMENT_ID" \
  --bytes-per-second 1048576 \
  --initial-delay-seconds 2 \
  --request-timeout-seconds 90 \
  --grace-period-seconds 30 \
  --rollout-timeout-seconds 120
```

The helper fails closed unless both exact names begin with `bluemap-perf-`;
it explicitly rejects `minecraft`, `minecraft-data`, and
`minecraft-maintenance-holder`. Both the Deployment and Pod must have
`app.kubernetes.io/part-of=bluemap-web-performance` and the exact, nonempty
experiment ID supplied on the command line. The Pod must be Running and Ready,
be selected by the named Deployment, and be controlled by a ReplicaSet owned
by that exact Deployment and UID.

Those identities and labels are checked before opening the local forwarding
connection, after it opens, and immediately before termination. Termination
uses the verified Pod UID as an API precondition, so a same-name replacement
cannot be removed by a time-of-check/time-of-use race. No other resource is
deleted. The helper waits for the complete transferred representation, the
old Pod to disappear, and the named Deployment rollout to become ready.

The artifact directory must not already exist. It receives the verified
target identities, expected response headers/hash/length, ready/result JSON,
termination response, logs, and final run state. A successful run has
`complete: true`, `podDeletionSubmitted: true`, and
`replacementRolloutReady: true` in the printed result.

Choose a byte rate that makes the response last several seconds but complete
comfortably inside both the application and Kubernetes grace periods. Archive
the Pod events, termination timestamps, and server logs.
Wait for the replacement Deployment to become fully available before a
measured case. Never run this procedure against `deployment/minecraft`,
`pod/minecraft-maintenance-holder`, or any non-disposable Pod, and never pass
a resource type, selector, wildcard, or partial name where an exact name is
required.

For public delivery tests, use
`https://bluemap-test.guenter.cloud` and a unique experiment ID. Any temporary
Cloudflare exception must match both that hostname and the generated
`BlueMap-Performance/<experiment-id>` User-Agent, and must be removed after
the run.

The Runpod token is supplied only at runtime. Never put it in this repository,
a Kubernetes Secret, a command-line argument captured in process listings, or
an artifact.
