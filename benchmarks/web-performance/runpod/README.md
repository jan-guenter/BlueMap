# RunPod load generator

This image is the external request source for formal BlueMap webserver
benchmarks. It runs a pinned k6 binary and exposes only an SSH endpoint for a
single unprivileged `loadgen` user.

The provisioner supplies an ephemeral Ed25519 public key through
`BLUEMAP_RUNPOD_SSH_PUBLIC_KEY`. Password authentication, root login,
local TCP forwarding, Unix-socket forwarding, agent forwarding, X11
forwarding, tunnelling, and user-controlled SSH environment variables are
disabled. HAProxy owns a loopback-only frontend at `127.0.0.1:18080` and uses
fixed `static-rr` TCP fanout to exactly eight loopback lanes on ports `18081`
through `18088`. The controller creates one independent remote SSH forward
for each lane. `GatewayPorts no` and an exact `PermitListen` allowlist for
`127.0.0.1:18081` through `127.0.0.1:18088` prevent the lanes from becoming a
general-purpose or publicly reachable proxy. SSH is not permitted to claim
the HAProxy frontend. The private key is never sent to RunPod, and the RunPod
API still exposes only SSH port 22.

The eight-lane fanout is deliberately fixed and fail-closed. HAProxy performs
no health checks, retries, redispatch, or adaptive backend removal. If any
independent SSH lane stops carrying traffic, requests fail instead of silently
continuing with a different transport topology. The image entrypoint
supervises HAProxy and sshd together and terminates the container if either
service exits.

Every k6 command session is coupled to an stdin lease whose only write-capable
descriptor is owned by the controller helper itself. A nonce-bound startup
handshake is consumed before launch, and a liveness probe arms the EOF watcher
before the workload barrier is released. The wrapper supervises that watcher
for the entire workload and fails closed if it exits other than for a recorded
lease event or the wrapper's deliberate stop. Every SSH child also has a checked
parent-death KILL signal, so helper loss cannot leave lane processes holding the
controller output pipe open. A verified process-group launch barrier closes the
fork-to-`setsid` race, and both the remote timeout and a separate helper
wall-clock deadline are bounded. The remote wrapper sends TERM,
escalates to KILL, confirms that the process group is empty, reaps its watcher
and resource sampler, and atomically publishes a nonce-bound session receipt.
A global active-phase lock is released only after that terminal proof exists;
confirmation also requires the lock to be absent. Existing output paths or a
stale lock block later work instead of allowing replay or overlapping traffic.

The HAProxy frontend is reserved for the direct L4 benchmark route. It lets k6
retain `bluemap-test.guenter.cloud` as the URL host while the controller
carries opaque HTTP bytes over all eight SSH lanes to the cluster's internal
Traefik service. Consequently both `Host: bluemap-test.guenter.cloud` and
`Accept-Encoding: zstd` reach Traefik without Cloudflare HTTP processing.

The formal run freezes and records:

- the image digest;
- the full source revision baked into that image at build time;
- RunPod Pod, machine, data-center, CPU-flavor, and vCPU identities;
- the SSH host-key fingerprint;
- the output of `bluemap-runpod-identity`;
- a throughput calibration and per-phase k6 artifacts.

Use one fixed Secure CPU Pod for the complete matrix. Add another source only
if the calibration proves that the first generator is a bottleneck.

## Image and immutable launch reference

The `runpod-loadgen-image` job in `.github/workflows/web.yml` publishes:

```text
ghcr.io/<lowercase-repository-owner>/bluemap-perf-loadgen:sha-<git-commit>
```

The workflow passes its full lowercase Git SHA as a required Docker build
argument. The image stores that revision in a root-owned, read-only build
record. It is never supplied through the RunPod runtime environment.

Provisioning must use the manifest digest, never the mutable tag:

```text
ghcr.io/<lowercase-repository-owner>/bluemap-perf-loadgen@sha256:<digest>
```

The formal lifecycle helper accepts only
`ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:<digest>`. It rejects every
other repository, a tag-only reference, Community/interruptible compute,
non-EU placement, CPU flavors other than `cpu5c`, and allocations other than
8 vCPUs.

## Secret-safe provisioning

Generate a dedicated Ed25519 key. The private key stays on the controller and
must have mode `0600`; only its public half is sent to RunPod.

First inspect the exact, non-secret v1 request without making an API call:

```shell
benchmarks/web-performance/tools/manage_runpod_loadgen.py plan \
  --run-id formal-25db-r3 \
  --image ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:<digest> \
  --source-revision <40-character-source-S-git-sha> \
  --data-center EU-NL-1 \
  --ssh-public-key /secure/runpod-formal/id_ed25519.pub \
  --ssh-private-key /secure/runpod-formal/id_ed25519
```

Then create exactly one Pod. `--confirm-create` must repeat the run ID:

```shell
(
  read -rsp 'RunPod API key: ' RUNPOD_API_KEY
  export RUNPOD_API_KEY
  trap 'unset RUNPOD_API_KEY' EXIT
  benchmarks/web-performance/tools/manage_runpod_loadgen.py create \
    --run-id formal-25db-r3 \
    --confirm-create formal-25db-r3 \
    --image ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:<digest> \
    --source-revision <40-character-source-S-git-sha> \
    --data-center EU-NL-1 \
    --ssh-public-key /secure/runpod-formal/id_ed25519.pub \
    --ssh-private-key /secure/runpod-formal/id_ed25519 \
    --output-dir /secure/runpod-formal/formal-25db-r3
)
```

The helper consumes `RUNPOD_API_KEY` only from its environment, removes it
from child-process environments, never puts it in `argv`, and never prints or
writes API response bodies on failure. Do not put the key in a shell command,
argument, config file, benchmark artifact, or Git repository.

Creation uses `POST https://rest.runpod.io/v1/pods` with one fixed data center,
Secure on-demand CPU compute, `cpu5c`, 8 vCPUs, 500 Mbps minimum download, and
100 Mbps minimum upload. It never silently selects another region or flavor.

As soon as the API returns a Pod ID, `pod-state.json` is written. This is the
recovery handle if image startup, SSH, or identity capture later fails. The
helper deliberately does not auto-delete a partially initialized Pod because
deletion must always identify and confirm the exact target.

## Identity capture and verification

After the Pod is ready, the helper:

1. verifies the API-reported Pod ID, run marker, immutable image, machine ID,
   Secure placement, exact data center, CPU allocation, and network floors;
2. reads the Ed25519 SSH host key three times and requires an identical key;
3. freezes that key and its SHA-256 fingerprint in `frozen-identity.json`;
4. connects with strict host-key checking, runs `bluemap-runpod-identity`, and
   requires its baked source revision to equal `--source-revision`;
5. writes the independently observed result to
   `live-identity-before.json`.

RunPod v1 omits the nested `machine` object unless requested. Readiness and
later identity verification therefore use
`GET /v1/pods/<id>?includeMachine=true`; an unexpanded response fails closed.

The frozen file follows `identity.schema.json`. It is non-secret, but the
output directory defaults to private permissions because it is paired with a
private controller key elsewhere. `run_origin_case.sh` receives the frozen
identity and the private-key path separately.

Recheck API placement, machine identity, port mapping, pinned host key, image,
and live runtime before starting or resuming orchestration:

```shell
(
  read -rsp 'RunPod API key: ' RUNPOD_API_KEY
  export RUNPOD_API_KEY
  trap 'unset RUNPOD_API_KEY' EXIT
  benchmarks/web-performance/tools/manage_runpod_loadgen.py verify \
    --output-dir /secure/runpod-formal/formal-25db-r3 \
    --ssh-private-key /secure/runpod-formal/id_ed25519
)
```

Any changed machine ID, public IP, SSH mapping/key, region, CPU allocation, or
image invalidates the formal run. Never recapture a changed key into an
existing run identity.

## Explicit deletion

Deletion reads `pod-state.json`, addresses only that exact Pod ID, and requires
the same ID as an explicit confirmation. It also verifies the Pod name, run-ID
environment marker, and image before issuing `DELETE /v1/pods/<id>`.

```shell
pod_id="$(
  jq -r '.podId' \
    /secure/runpod-formal/formal-25db-r3/pod-state.json
)"
(
  read -rsp 'RunPod API key: ' RUNPOD_API_KEY
  export RUNPOD_API_KEY
  trap 'unset RUNPOD_API_KEY' EXIT
  benchmarks/web-performance/tools/manage_runpod_loadgen.py delete \
    --output-dir /secure/runpod-formal/formal-25db-r3 \
    --confirm-delete "$pod_id"
)
```

The helper waits until the API confirms absence and writes `deletion.json`.
It preserves the frozen identity and live evidence. Stopping is intentionally
not offered: this disposable Pod has no persistent data and must be deleted to
end all associated billing.
