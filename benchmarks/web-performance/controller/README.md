# Durable formal benchmark controller

This directory packages one frozen RunPod-backed formal benchmark into a
run-specific controller image. The image contains an exact clean checkout of
the benchmark revision, its reviewed controller and analyzer, and the tracked
matrix, schedule, admission identities, and map manifest.

The image build fails closed if the bundle targets another revision, a
controller hash differs, or the analyzer still expects a Kubernetes
load-generator Pod instead of frozen RunPod identity and per-phase capacity
evidence.

## Freeze and publish before building

Finish, commit, and push the benchmark/harness source revision **S** first.
Build the load-generator image from that exact revision (the
`runpod-loadgen-image` job in `.github/workflows/web.yml`) and resolve the
published tag to its immutable manifest reference:

```shell
source_revision="$(git rev-parse --verify HEAD^{commit})"
load_generator_image="ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:<digest-built-from-$source_revision>"
```

Do not provision the paid RunPod yet. From the clean source-S checkout,
initialize the controller binding:

```shell
python benchmarks/web-performance/controller/prepare_frozen_bundle.py init-lock
```

The confirmation printed by the freezer binds the current revision and
controller hashes. Run it against only the six disposable candidates, then
publish the result into the tracked `controller/frozen` directory:

```shell
confirmation="$(
  python benchmarks/web-performance/controller/formal/freeze.py validate \
    --load-generator-image "$load_generator_image" |
  jq -r .requiredConfirmation
)"
revision="$(git rev-parse --short=12 HEAD)"
fresh_freeze="benchmarks/web-performance/artifacts/snapshot/formal-inputs-$revision-runpod-staging"
python benchmarks/web-performance/controller/formal/freeze.py run \
  --load-generator-image "$load_generator_image" \
  --confirm "$confirmation" \
  --manifest benchmarks/web-performance/artifacts/snapshot/manifest.json \
  --output-dir "$fresh_freeze"
python benchmarks/web-performance/controller/prepare_frozen_bundle.py publish \
  --source-root "$fresh_freeze" \
  --manifest benchmarks/web-performance/artifacts/snapshot/manifest.json
```

Review and commit the generated frozen directory as a bundle-only follow-up
commit. Never copy a previous formal bundle forward. This two-commit design
avoids a self-referential Git hash: the matrix names the clean benchmark
commit, while the follow-up commit makes its complete frozen input available
to GitHub Actions. The publish workflow rejects merges, intermediate commits,
modifications, and unrelated files: the bundle commit must be the direct
single-parent child of the matrix revision, and its complete diff must be the
addition of exactly the six generated JSON files enforced by
`.github/workflows/formal-controller.yml`.

The dedicated `fresh_freeze` path is intentional. The preserved historical
`artifacts/snapshot/formal-inputs` directory is neither read, overwritten,
moved, nor deleted by this workflow.

Run the `Formal benchmark controller image` workflow. It validates the tracked
bundle, checks out the matrix's exact benchmark revision inside the image, and
publishes:

```text
ghcr.io/jan-guenter/bluemap-perf-controller:formal-<benchmark-revision>
ghcr.io/jan-guenter/bluemap-perf-controller:bundle-<bundle-commit>
```

Both tags resolve to the same image. Deploy only its immutable registry
digest. A local equivalent from the bundle commit is:

```shell
revision="$(
  jq -r .benchmarkGitRevision \
    benchmarks/web-performance/controller/frozen/formal-inputs/matrix.json
)"

docker buildx build \
  --platform linux/amd64 \
  --file benchmarks/web-performance/controller/Dockerfile \
  --build-arg "REVISION=$revision" \
  --build-arg "VERSION=formal-$revision" \
  --build-arg "CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag "ghcr.io/jan-guenter/bluemap-perf-controller:formal-$revision" \
  --push \
  .
```

Resolve the registry digest and replace every zero digest, revision, and run
ID placeholder in `kubernetes/formal-controller.yaml`. The frozen bundle is
carried by the image; it is not uploaded to a ConfigMap or copied from the
workstation at runtime. Only after the bundle commit **B** and controller
image exist should the paid RunPod be provisioned. Its immutable image and
baked source revision must repeat the bundle binding exactly:

```shell
benchmarks/web-performance/tools/manage_runpod_loadgen.py create \
  --run-id <formal-run-id> \
  --confirm-create <formal-run-id> \
  --image "$load_generator_image" \
  --source-revision "$source_revision" \
  --data-center EU-NL-1 \
  --ssh-public-key /secure/runpod-formal/id_ed25519.pub \
  --ssh-private-key /secure/runpod-formal/id_ed25519 \
  --output-dir /secure/runpod-formal/<formal-run-id>
```

The four-key `bundle-manifest.json.loadGenerator` object is authoritative:
`backend`, the full immutable `image`, its `imageDigest`, and
`sourceRevision` S. Its canonical SHA-256 is repeated in freeze confirmation,
plans, run/preflight state, and analysis output. Preflight and the formal run
compare it with the frozen RunPod identity before creating artifact roots,
reading Kubernetes, locking, or scaling; analysis then joins it to the live
remote identity recorded for every phase.

## Runtime model and inputs

The Kubernetes Job is only the durable control plane. It validates the fixed
benchmark mux route, scales the exact allowlisted candidates, records
Kubernetes and Prometheus evidence, and sends every k6 phase over SSH to the
frozen RunPod CPU machine. Neither the Job nor the workstation is a benchmark
request source.

The canonical Job uses only `ssh-l4-traefik` and
`http://bluemap-test.guenter.cloud` over a fixed SSH L4 tunnel
(`127.0.0.1:18080` to
`rke2-traefik.kube-system.svc.cluster.local:80`). It still exercises the same
Traefik host rule and `bluemap-perf-public:8100` mux, but makes no edge-bypass
claim. Its tunnel endpoints are hardcoded controller identity, not runtime
configuration. These are the checked-in ConfigMap defaults:

```yaml
BLUEMAP_TRAFFIC_MODE: "ssh-l4-traefik"
BLUEMAP_TRAFFIC_BASE_URL: "http://bluemap-test.guenter.cloud"
```

The runner and dry-run tooling retain `cloudflare-https` for separate
diagnostic work, but the mandatory preflight and its following 80-entry formal
run reject it. The direct mode, URL, tunnel object, and absence of an
edge-bypass claim are persisted in both execution and workload identities.

Before the 80-entry schedule, the same Job runs a non-resumable six-entry
preflight. It derives a one-block matrix from the validated formal matrix,
copying the exact immutable Java/Rust image, sanitized-config, and runtime
identities. The fixed cases are:

- `large-object` at rate 1 for the enhanced single-replica Java and Rust
  candidates;
- `map-data-mixed` at rate 15 for those same candidates;
- `map-data-mixed` at rate 40 for the three-replica Java and Rust candidates.

Each entry uses a 30-second warm-up, two-minute measurement, 15-second
cool-down, and zstd storage/accept encoding. The derived matrix, generated
six-entry schedule, provenance, checksums, per-case evidence, and final report
are preserved under the sibling `<run-id>-preflight` artifact directory. The
formal `run` subcommand independently reloads and validates that exact passed
report before it creates formal state or scales a candidate, so bypassing the
entrypoint does not bypass the gate. The formal state archives the report hash
and identity. The handoff must occur on the same live controller Pod within
five minutes. The offline analyzer reconstructs the one-block schedule itself,
semantically replays all six raw case directories and lifecycle events, and
recomputes the relay summary from the preserved metrics samples before it
accepts the 80-entry run.

During preflight, the controller samples the exact admitted controller Pod
through the existing namespaced `metrics.k8s.io` permission. A bounded
readiness wait allows the newly started PodMetrics object to appear; those
attempts are preserved separately, and the measured interval begins only with
the first valid sample. Any later sampling error fails the gate. The Pod name
must match both its downward-API value and container hostname, and its run
label, ServiceAccount, Job owner, UID, readiness, and fixed 2 CPU/2 GiB limits
are persisted and revalidated. The gate also requires fresh and continuous
unique samples, p95 CPU at most 70% of the limit, maximum CPU at most 90%, and
maximum memory at most 80%. This is deliberately described as a coarse
container CPU/memory headroom gate: metrics-server cannot attribute usage to
the SSH relay process and provides neither bandwidth nor CPU-throttling proof.
Rancher's Prometheus currently exposes no `traefik_*` series, while scraping
Traefik's separate three-replica metrics ClusterIP would observe only one
load-balanced endpoint. The preflight records that limitation instead of
claiming an incomplete Traefik counter gate; exact k6 status/error checks are
the request-scoped 5xx gate.

The Job additionally requires:

- ConfigMap `bluemap-perf-runpod-identity`, key `identity.json`, containing
  the non-secret frozen RunPod identity;
- Secret `bluemap-perf-runpod-ssh`, key `id_ed25519`, containing only the
  dedicated ephemeral private key.

Create them from the provisioner's output without committing them:

```shell
run_id="$(jq -r .runId /absolute/path/to/identity.json)"
kubectl -n minecraft create configmap bluemap-perf-runpod-identity \
  --from-file=identity.json=/absolute/path/to/identity.json \
  --labels="app.kubernetes.io/part-of=bluemap-web-performance,bluemap.guenter.cloud/experiment-id=$run_id"
kubectl -n minecraft create secret generic bluemap-perf-runpod-ssh \
  --from-file=id_ed25519=/absolute/path/to/id_ed25519 \
  --labels="app.kubernetes.io/part-of=bluemap-web-performance,bluemap.guenter.cloud/experiment-id=$run_id"
```

The nonroot init container copies the projected key to an isolated `emptyDir`
with mode `0600`; the main container never receives a RunPod API token. The
identity `runId`, runtime ConfigMap run ID, and controller image revision must
agree. The Job refuses an existing run directory, so a failed or abandoned
formal run is never resumed automatically. The preflight root is also refused
if it already exists, including an empty directory or dangling symlink.

At startup the controller verifies both its completed init container and its
running main container image IDs against the configured registry digest. It
also requires the init and main image references in the live Pod manifest to
be identical and digest-pinned.

The Job's 2,100-second termination grace covers the orchestrator's worst-case
cleanup envelope: up to 40 seconds to stop a running case, six sequential
300-second candidate-convergence checks, and 260 seconds of API and artifact
write margin. This lets the signal-driven `finally` cleanup quiesce every
allowlisted candidate before Kubernetes sends `SIGKILL`.

`formal-controller.yaml` creates only its dedicated ServiceAccount, exact
namespaced Role/RoleBinding, artifact PVC, runtime ConfigMap, and Job. The
Role can read only the named public Ingress in addition to the already
allowlisted benchmark resources, so the controller can prove the fixed
Traefik route without broader ingress access. The
public Service/Ingress are separate in `kubernetes/public-ingress.yaml`.
Neither manifest mounts or grants access to the Minecraft data PVC.

Validate the package without contacting the cluster:

```shell
python benchmarks/web-performance/controller/test_packaging.py
python -m unittest discover \
  -s benchmarks/web-performance/controller/tests \
  -p 'test_*.py'
```
