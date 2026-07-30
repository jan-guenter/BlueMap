# BlueMap Web container

This image runs BlueMap's web UI and map-data HTTP server without a Minecraft
server, world reader, renderer command, or platform plugin entry point. It uses
the same Java storage and HTTP implementations as BlueMap itself.

The image expects a BlueMap configuration tree at `/config`, stores generated
web-app files under `/data`, listens on port `8100`, and runs as UID/GID
`10001`. For example:

```shell
docker run --rm \
  --publish 8100:8100 \
  --volume "$PWD/config:/config:ro" \
  --volume bluemap-web-data:/data \
  ghcr.io/bluemap-minecraft/bluemap-web:latest
```

The configuration needs `core.conf`, `webapp.conf`, `webserver.conf`, at least
one storage under `storages/`, and at least one web-only map under `maps/`.
Omit `world` from each map configuration so this process registers the map
without trying to render it.

The image intentionally contains no JDBC drivers. For SQL storage, mount the
required driver JAR and set BlueMap's `driver-jar` and `driver-class` storage
settings. The Helm chart supports loading one driver from an existing
ConfigMap or a checksum-verified download URL.

File storage also works, provided the web container and renderer share its
storage directory. Do not put database passwords in an image layer or a
ConfigMap; mount the SQL storage configuration from a secret.

## Live data

The standalone process does not receive in-process events from the Minecraft
server, so `sse-enabled` should be `false`. The web app automatically polls
storage for updates. To publish players and markers from the Minecraft server,
set BlueMap's `write-players-interval` and `write-markers-interval` to positive
values in its plugin configuration.

The server exposes `/health/live` and `/health/ready`. Readiness means that the
generated webroot exists and all referenced storage backends are healthy.
Readiness never runs a synchronous storage dependency check. SQL storage uses
a cached background `SELECT 1`; file storage uses a daemon background probe
that verifies the configured root is a directory without creating or changing
it. Both cached states expire after ten seconds without a successful check, so
a stalled database or network filesystem cannot leave readiness healthy
indefinitely. File storage starts unready until its first successful root
probe.

`webserver.conf` also controls `max-active-connections`,
`connection-idle-timeout-seconds`, `max-request-line-bytes`,
`max-header-count`, `max-header-bytes`, and `max-body-bytes`. The defaults are
intended to provide predictable benchmark and overload behavior; tune the
connection limit together with the SQL pool limit.

For larger public deployments, the Helm chart can route `/maps` to an
independently scalable SQL data tier built from the
[BlueMap SQL PHP-FPM image](../php/README.md). The Java container continues to
serve the generated web UI while PHP retrieves map data directly from the
database.
