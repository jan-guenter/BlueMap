# BlueMap Web Helm chart

This chart deploys the standalone BlueMap web UI without a Minecraft server or
renderer. It supports file storage and SQL storage and can optionally add a
horizontally scalable PHP-FPM data tier for busier public maps.

```shell
helm install bluemap \
  oci://ghcr.io/bluemap-minecraft/charts/bluemap-web \
  --namespace bluemap \
  --create-namespace
```

The Java pod runs without root, Linux capabilities, or a service-account token
and supports a read-only root filesystem. It exposes a ClusterIP Service on
port `8100`; ingress is opt-in.

## Structured storage configuration

The chart generates exactly one `storages/<id>.conf` file from the `storage`
object. Do not add that same path to `config.files` or `secretConfig.files`.
Map configurations should reference `storage.id`.

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

## Scalable PHP-FPM data tier

BlueMap's external SQL webserver design uses its `sql.php` script to translate
normal `/maps/...` requests into SQL queries. Enable that tier with:

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

## Configuration sources

- `config.files` creates non-sensitive BlueMap configuration files in a
  ConfigMap. Keys are paths relative to `/config` and their contents support
  Helm `tpl` expressions.
- `config.existingConfigMap` and `config.items` mount an existing ConfigMap.
- `secretConfig.existingSecret` and `secretConfig.items` mount additional
  sensitive BlueMap configuration.
- `secretConfig.files` is useful for local testing, but values containing
  secrets should not be committed or passed through command-line history.

The generated storage file is always projected alongside these sources.

Keep the Java `replicaCount` at `1` unless every Java replica has an independent
writable webroot and concurrent web-app synchronization has been tested. The
PHP data tier is stateless and is independently scalable through
`phpFpm.replicaCount`.

## Live data

The standalone Java process does not receive the Minecraft server's in-memory
events, so SSE is disabled. Set BlueMap's `write-players-interval` and
`write-markers-interval` to positive values on the Minecraft server. Both the
Java SQL handler and `sql.php` can then read the persisted player and marker
data.
