# BlueMap Web Helm chart

This chart deploys the standalone BlueMap web UI without a Minecraft server or
renderer. The existing Java webserver remains the default; an experimental
read-only Rust implementation is opt-in. The chart supports file and SQL
storage and can optionally add a horizontally scalable PHP-FPM data tier to
the Java deployment for busier public maps.

```shell
helm install bluemap \
  oci://ghcr.io/bluemap-minecraft/charts/bluemap-web \
  --namespace bluemap \
  --create-namespace
```

The web pod runs without root, Linux capabilities, or a service-account token
and supports a read-only root filesystem. It exposes a ClusterIP Service on
port `8100`; ingress is opt-in.

For non-release workflow runs, all three images receive the immutable
`sha-<full-40-character-commit>` tag and the OCI chart version is
`0.1.0-dev.sha.<full-40-character-commit>`. Branch tags remain convenience
aliases; pin GitOps deployments to the full-SHA image and chart versions.

## Experimental Rust webserver

Select the Rust implementation explicitly:

```yaml
webserver:
  implementation: rust
```

The chart then deploys `webserver.rust.image`, generates a dedicated
`config.toml`, and does not render the Java configuration, generated storage
HOCON, JDBC driver init container, or inline SQL Secret. Configure the maps
served by the Rust process under `webserver.rust.maps`; these entries also
control the generated webapp map order.

Complete file, MariaDB, and PostgreSQL examples are available in:

- `examples/rust-file-values.yaml`
- `examples/rust-mariadb-values.yaml`
- `examples/rust-postgresql-values.yaml`

For example:

```shell
helm upgrade --install bluemap ./charts/bluemap-web \
  --namespace bluemap \
  --values charts/bluemap-web/examples/rust-postgresql-values.yaml
```

Rust supports only BlueMap file storage, MariaDB, and PostgreSQL. Helm rejects
MySQL, SQLite, custom storage, PHP-FPM, JDBC URLs/properties/drivers, inline
database credentials, and external Java ConfigMap/Secret sources while Rust is
selected. The built-in `config.files` defaults remain present for Java
backward compatibility but are not mounted into the Rust pod; use only
`webserver.rust` for application settings. PostgreSQL ambient variables such
as `PGOPTIONS` and `PGSSLROOTCERT` are rejected from `extraEnv`.

SQL credentials must come from an existing Secret:

```yaml
storage:
  type: sql
  sql:
    databaseType: postgresql
    host: postgresql.database.svc
    port: 5432
    database: bluemap
    maxConnections: 10
    connectTimeoutSeconds: 10
    credentials:
      existingSecret: bluemap-database
      usernameKey: username
      passwordKey: password
```

The keys are exposed only as `BLUEMAP_DATABASE_USERNAME` and
`BLUEMAP_DATABASE_PASSWORD`. Optional database CA and mutual-TLS material are
mounted read-only from existing Secrets. Helm rejects CA or client material
when TLS mode is `disable`:

```yaml
webserver:
  rust:
    databaseTls:
      mode: verify-full
      ca:
        existingSecret: bluemap-database-ca
        key: ca.crt
      clientCertificate:
        existingSecret: bluemap-database-client
        certKey: tls.crt
        keyKey: tls.key
```

The shared `replicaCount`, `strategy`, probes, `resources`, scheduling, and
pod-security values apply to Rust. SQL replicas are stateless; size
`replicaCount * storage.sql.maxConnections` within the database connection
budget. Keep each pod's `webserver.rust.config.maxInFlightRequests` near
`storage.sql.maxConnections`, so the aggregate in-flight limit remains near
the aggregate pool capacity instead of retaining many SQL BLOBs above it. File
deployments with multiple replicas require a shared ReadWriteMany (RWX) volume
with reliable read-after-rename behavior, as shown by the file example. The
Rust mount is read-only. Custom `extraVolumeMounts` must live outside `/data`,
`/etc/bluemap-web`, and the database TLS mount roots so they cannot shadow
chart-managed content. The Rust image is linux/amd64 only. The chart merges
`kubernetes.io/arch: amd64` into `nodeSelector` and rejects a conflicting
selector while preserving other user selectors.

The default in-flight limit is eight. The largest object in the reference test
data was a 20.2 MiB texture, so eight materialized responses alone can retain
at least 161.6 MiB. The examples request 128 MiB and cap each pod at 512 MiB to
leave room for database, TLS, allocator, and runtime overhead. Recalculate both
the in-flight limit and memory limit for the largest object in your own map.
File-backed syscalls use at most the smaller of the in-flight limit and eight
blocking workers.

Tiles default to
`public,max-age=60,must-revalidate,no-transform`; tune the age with
`webserver.rust.config.tileCacheMaxAgeSeconds`. Settings, textures, assets, and
markers revalidate, while player data is private and never stored. Stored
compression is passed through without transcoding. BlueMap block-stream LZ4 is
therefore allowed, but normal browsers do not advertise `lz4` and receive the
structured HTTP 406 response that identifies the required coding.
`mapDataRoot` and `liveDataRoot` must both remain `maps`, matching the route
served by this implementation.

Rust shutdown has two bounded phases:
`webserver.rust.config.shutdownGraceSeconds` for health-monitor shutdown, HTTP
draining, and storage cleanup, followed by
`webserver.rust.config.runtimeShutdownSeconds` for any uncancellable blocking
filesystem workers. The generated `terminationGracePeriodSeconds` includes
both values plus five seconds of Kubernetes margin.

## Structured storage configuration

For Java, the chart generates exactly one `storages/<id>.conf` file from the
`storage` object. Do not add that same path to `config.files` or
`secretConfig.files`. Java map configurations should reference `storage.id`.
Rust translates the same structured storage values directly into its TOML.

The default is disposable file storage:

```yaml
storage:
  id: file
  type: file
  compression: gzip
  file:
    root: /data/maps

config:
  files:
    maps/world.conf: |
      name: "World"
      storage: {{ .Values.storage.id | quote }}
```

Enable `persistence`, select an existing claim, or mount shared storage with
`extraVolumes` and `extraVolumeMounts` for a durable file-storage deployment.

## SQL storage

The Java image intentionally contains no JDBC drivers. Supply the one required
by your database either from an existing ConfigMap or through a download URL.

Create a Secret containing the database credentials:

```shell
kubectl -n bluemap create secret generic bluemap-database \
  --from-literal=username=bluemap \
  --from-literal=password=replace-me
```

Then configure MariaDB and a checksum-pinned Connector/J download:

```yaml
storage:
  id: sql
  type: sql
  compression: gzip
  sql:
    databaseType: mariadb
    host: mariadb.database.svc
    port: 3306
    database: bluemap
    maxConnections: 10
    credentials:
      existingSecret: bluemap-database
      usernameKey: username
      passwordKey: password
    driver:
      className: org.mariadb.jdbc.Driver
      download:
        url: https://dlm.mariadb.com/4765352/Connectors/java/connector-java-3.5.9/mariadb-java-client-3.5.9.jar
        sha256: 11e3bb5bbf8ef0e806ae4d6c5d5033fedf7262cc777f0190bde8a2f3c8e6bd8d

config:
  files:
    maps/world.conf: |
      name: "World"
      storage: {{ .Values.storage.id | quote }}
```

The generated BlueMap configuration contains the JDBC URL, credential
environment substitutions, connection limit, compression, and:

```hocon
driver-jar: "/drivers/jdbc-driver.jar"
driver-class: "org.mariadb.jdbc.Driver"
```

`connectionUrl` can override the generated JDBC URL when driver-specific query
parameters are required. The PHP tier still uses the separately structured
`host`, `port`, and `database` values.

To provide the JAR from a ConfigMap instead:

```shell
kubectl -n bluemap create configmap bluemap-jdbc-driver \
  --from-file=driver.jar=mariadb-java-client-3.5.9.jar
```

```yaml
storage:
  type: sql
  sql:
    driver:
      className: org.mariadb.jdbc.Driver
      existingConfigMap:
        name: bluemap-jdbc-driver
        key: driver.jar
```

The ConfigMap and download sources are mutually exclusive. Kubernetes limits
ConfigMap objects to 1 MiB, so a download is usually preferable for larger
drivers. The optional download checksum must contain exactly 64 hexadecimal
characters. Set `driver.className` when using either external JAR source;
BlueMap needs the class name to instantiate a driver from `driver-jar`.

The structured SQL configuration supports `mariadb`, `mysql`, `postgresql`,
and `sqlite`. SQLite uses `storage.sql.database` as the database file path and
cannot use the PHP-FPM tier.

If `credentials.existingSecret` is empty, the chart creates a Secret from
`credentials.username` and `credentials.password`. Those values remain in the
Helm release, so an existing externally managed Secret is recommended for
production.

### SQL cache-metadata upgrade

BlueMap adds nullable cache-validator columns to existing SQL storage tables at
startup. Readers remain compatible while those columns are being added.

For PostgreSQL, stop every older BlueMap writer before starting the first
version that writes cache metadata, and never let an older writer write to that
database afterward. A legacy PostgreSQL upsert can update map data while
preserving the previous hash and timestamp, which would expose stale HTTP cache
validators. Multiple read-only webserver replicas may run during the upgrade;
only the writer versions must not overlap.

## Scalable PHP-FPM data tier

BlueMap's external SQL webserver design uses its `sql.php` script to translate
normal `/maps/...` requests into SQL queries. The PHP image intentionally keeps
the existing upstream script unchanged. It is retained as a compatibility and
performance baseline; it does not consume the new SQL cache metadata or provide
the Java server's enhanced validator and unsupported-encoding responses.

Enable that tier with:

```yaml
phpFpm:
  enabled: true
  replicaCount: 4

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: map.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - hosts:
        - map.example.com
      secretName: map-tls
```

This creates a second Deployment with the requested number of replicas. Each
pod contains:

- BlueMap's PHP script running in PHP-FPM with PDO MySQL/MariaDB and PostgreSQL
  support.
- An unprivileged NGINX sidecar converting HTTP requests to FastCGI.

The PHP Service load-balances all replicas. For every configured ingress host,
the chart adds `/maps` as a `Prefix` route to the PHP Service; the existing `/`
route continues to use the Java web Service. `phpFpm.ingress.path` can add a
base path when the web app is hosted in a subfolder, but it must end in
`/maps`, for example `/bluemap/maps`.

PHP-FPM can only be enabled with `storage.type: sql`, and BlueMap's script
supports MySQL, MariaDB, and PostgreSQL. Helm rendering fails for file storage
or SQLite.

The PHP image receives database credentials from the same Secret as the Java
pod. The script is always executed behind FastCGI; it is never exposed as a
downloadable static file.

## Java configuration sources

- `config.files` creates non-sensitive BlueMap configuration files in a
  ConfigMap. Keys are paths relative to `/config` and their contents support
  Helm `tpl` expressions.
- `config.existingConfigMap` and `config.items` mount an existing ConfigMap.
- `secretConfig.existingSecret` and `secretConfig.items` mount additional
  sensitive BlueMap configuration.
- `secretConfig.files` is useful for local testing, but values containing
  secrets should not be committed or passed through command-line history.

The generated Java storage file is always projected alongside these sources.
They are not mounted when Rust is selected.

With one Java replica, `/data/web` remains part of the `data` volume for
compatibility with existing persistent webroots. With multiple replicas, the
chart overlays `/data/web` with a pod-local `emptyDir` so each replica can
safely synchronize its own web app and `settings.json`. In that mode,
`extraVolumes` cannot use the reserved `webroot` name and
`extraVolumeMounts` cannot use the reserved `/data/web` path.

The default deployment strategy is `RollingUpdate` for multiple replicas, and
`Recreate` for a single replica or when enabled persistence declares
`ReadWriteOnce`. Set `strategy` explicitly to override this selection. File
storage replicas may share the map-data volume with a single BlueMap writer
when atomic file writes remain enabled. SQLite is restricted to one Java
replica. Size `storage.sql.maxConnections` as a per-pod limit, accounting for
all Java replicas and the Minecraft writer. A positive value also caps
in-flight SQL response bodies per Java replica; excess reads fail fast with
`503 Service Unavailable` and `Retry-After: 1` instead of accumulating behind
the connection pool. The PHP data tier is independently scalable through
`phpFpm.replicaCount`.

The default liveness probe is a TCP check. It verifies that the listener still
exists without competing for one of the bounded HTTP connection slots.
Readiness remains an HTTP request to `/health/ready`; a saturated or
dependency-unhealthy replica can therefore be removed from Service traffic
without Kubernetes restarting an otherwise live process. Java SQL readiness
uses a cached background dependency probe, so the endpoint changes to `503`
during a database outage without blocking on the JDBC pool and recovers after
connectivity returns. The cached state also expires after ten seconds without
any successful SQL operation, so a stalled JDBC call cannot leave readiness
healthy indefinitely. Java connections accepted after
`max-active-connections` is reached are closed immediately.

Rust validates all configured maps at startup, then keeps recurring readiness
checks cheap with one SQL `SELECT 1` or a file-root handle check per interval
instead of repeating per-map queries. It rejects excess map-data requests with
`503 Service Unavailable`.

On termination, the Java server stops accepting traffic and marks readiness
false, then drains active responses for `shutdownGracePeriodSeconds` (20
seconds by default). The pod's `terminationGracePeriodSeconds` defaults to 30,
leaving time to force-close a stalled response and finish process cleanup.

## Live data

The standalone Java process does not receive the Minecraft server's in-memory
events, so SSE is disabled. Set BlueMap's `write-players-interval` and
`write-markers-interval` to positive values on the Minecraft server. Java,
Rust, and `sql.php` can then read the persisted player and marker data.
