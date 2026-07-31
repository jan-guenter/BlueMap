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

Finish and commit the benchmark/harness changes, provision the fixed RunPod
CPU machine, and initialize the controller binding for that exact clean
commit:

```shell
python benchmarks/web-performance/controller/prepare_frozen_bundle.py init-lock
```

The confirmation printed by the freezer binds the current revision and
controller hashes. Run it against only the six disposable candidates, then
publish the result into the tracked `controller/frozen` directory:

```shell
confirmation="$(
  python benchmarks/web-performance/controller/formal/freeze.py validate |
  jq -r .requiredConfirmation
)"
revision="$(git rev-parse --short=12 HEAD)"
fresh_freeze="benchmarks/web-performance/artifacts/snapshot/formal-inputs-$revision-runpod-staging"
python benchmarks/web-performance/controller/formal/freeze.py run \
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
addition of exactly the six generated JSON files listed in `frozen/README.md`.

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
workstation at runtime.

## Runtime model and inputs

The Kubernetes Job is only the durable control plane. It validates the public
route, scales the exact allowlisted candidates, records Kubernetes and
Prometheus evidence, and sends every k6 phase over SSH to the frozen RunPod
CPU machine. Neither the Job nor the workstation is a benchmark request
source.

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
formal run is never resumed automatically.

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
Cloudflare/Traefik route without broader ingress access. The
public Service/Ingress are separate in `kubernetes/public-ingress.yaml`.
Neither manifest mounts or grants access to the Minecraft data PVC.

Validate the package without contacting the cluster:

```shell
python benchmarks/web-performance/controller/test_packaging.py
python -m unittest discover \
  -s benchmarks/web-performance/controller/tests \
  -p 'test_*.py'
```
