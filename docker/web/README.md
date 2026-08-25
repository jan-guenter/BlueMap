# BlueMap Web container

This image runs BlueMap's Java web UI and map-data server without a Minecraft
server, world reader, renderer command, or platform plugin. It uses the same
storage and HTTP implementation as BlueMap itself.

The image expects a BlueMap configuration tree at `/config`, writes generated
web files below `/data/web`, listens on port `8100`, and runs as UID/GID
`10001`:

```shell
docker run --rm \
  --publish 8100:8100 \
  --volume "$PWD/config:/config:ro" \
  --volume bluemap-web-data:/data \
  ghcr.io/bluemap-minecraft/bluemap-web:latest
```

The configuration needs `core.conf`, `webapp.conf`, `webserver.conf`, one or
more storage definitions under `storages/`, and at least one web-only map under
`maps/`. Omit `world` from each map configuration so the process registers the
map without trying to render it.

The image contains no JDBC drivers. For SQL storage, mount the required driver
JAR and configure `driver-jar` and `driver-class`. Put database credentials in
a mounted Secret rather than an image layer or ConfigMap. SQL storage can be
set to `read-only` so standalone replicas validate an initialized schema but
cannot create tables or modify map data.

File storage works when the web container and renderer share the map-storage
directory. Keep generated `/data/web` files local to each web process. For
horizontal scaling, use external MariaDB, MySQL, or PostgreSQL storage, give
each replica its own writable `/data` runtime directory, and distribute
requests without session affinity. The Helm chart applies these constraints
automatically.

## HTTP caching and compression

The server derives validators from file or SQL storage metadata, allowing
conditional requests to return `304 Not Modified` consistently across
replicas. Static files and map metadata are revalidated, tile freshness is
configurable, and private live-player data is not stored by caches.

Stored map-data compression is passed through without transcoding. If a client
does not advertise the required coding, the server returns `406 Not Acceptable`
and identifies the required encoding instead of returning an undecodable body.
Configure the reader with the same storage compression as the writer. The
client-decompression `.gz` URLs are the narrow exception: they always return a
raw gzip file, transcoding only when the stored representation is not gzip.

## Health and resource bounds

The server exposes `/health/live` and `/health/ready`. Readiness is updated by
background storage checks, so a probe does not wait on a JDBC pool or network
filesystem. Stale successful checks expire, allowing an unhealthy replica to
leave service without triggering a liveness restart.

`webserver.conf` controls `max-active-connections`,
`connection-idle-timeout-seconds`, `shutdown-grace-period-seconds`,
`max-request-line-bytes`, `max-header-count`, `max-header-bytes`, and
`max-body-bytes`. Tune the connection limit together with the per-process SQL
pool limit. During shutdown the server becomes unready, stops accepting new
connections, and drains active responses up to the configured grace period.

## Live data

The standalone process does not receive in-process events from the Minecraft
server, so `sse-enabled` should remain `false`. Set BlueMap's
`write-players-interval` and `write-markers-interval` to positive values on the
Minecraft server so the web process can poll persisted player and marker
updates.
