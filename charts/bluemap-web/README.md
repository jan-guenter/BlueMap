# BlueMap Web Helm chart

This chart runs BlueMap's standalone Java web server without a Minecraft
server, world reader, or renderer. It serves the generated web application and
map data from file or SQL storage.

```shell
helm install bluemap \
  oci://ghcr.io/bluemap-minecraft/charts/bluemap-web \
  --namespace bluemap \
  --create-namespace
```

The pod runs as UID/GID `10001`, drops Linux capabilities, uses the runtime
default seccomp profile, does not mount a service-account token, and supports a
read-only root filesystem. A ClusterIP Service exposes port `8100`; ingress is
opt-in.

## Stateless horizontal scaling

Set `replicaCount` above one only with an external MariaDB, MySQL, or
PostgreSQL database. The chart rejects file storage, SQLite, and persistence in
that mode. It also configures the SQL storage as read-only, so a web replica
validates an existing schema but cannot initialize or modify it.

Every pod receives projected configuration and credentials, a pod-local
runtime directory, and a pod-local `/data/web` `emptyDir` for generated web
files and `settings.json`. Requests therefore require neither session affinity
nor a shared writable webroot. The default strategy is a zero-unavailable
rolling update for multiple replicas and `Recreate` for one replica.

`storage.sql.maxConnections` is a per-pod database and in-flight response
limit. Allow for at most `replicaCount * maxConnections` connections from this
deployment, in addition to renderers and other database users. When the limit
is reached, map-data reads fail quickly with `503 Service Unavailable` instead
of building an unbounded queue.

## Connection metrics and autoscaling

The standalone server can expose connection-capacity metrics on a separate
OpenMetrics listener. The listener is disabled by default and is never added to
the public Service or Ingress. Enable it without autoscaling when a collector
should scrape the metrics for observation:

```yaml
metrics:
  enabled: true
  bindAddress: 0.0.0.0
  port: 9090
```

The rendered `<release>-bluemap-web-metrics` ClusterIP Service selects every
web pod and serves `/metrics`. The endpoint is unauthenticated and is reachable
cluster-wide unless a NetworkPolicy narrows access. Its default Prometheus
scrape annotations can be replaced or extended through
`metrics.service.annotations`. An OpenTelemetry Collector can scrape the
endpoint with its Prometheus receiver.

The endpoint publishes these label-free gauges:

- `bluemap_web_http_connections`
- `bluemap_web_http_connections_limit`
- `bluemap_web_http_connections_average_1m`
- `bluemap_web_http_connections_average_5m`
- `bluemap_web_http_connection_utilization_ratio`
- `bluemap_web_http_connection_utilization_average_1m_ratio`
- `bluemap_web_http_connection_utilization_average_5m_ratio`

The current count comes from the same connection semaphore governed by
`max-active-connections`. It includes active requests and idle keep-alive
connections, so it measures connection-slot pressure rather than CPU usage or
request throughput. The one-minute and five-minute values are elapsed-time
weighted from one-second samples. During process startup they cover the history
available so far instead of assuming zero load before startup.

Enable the optional HPA only with external MariaDB, MySQL, or PostgreSQL
storage:

```yaml
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10
  utilizationMetricName: bluemap_web_http_connection_utilization_average_1m_ratio
  targetAverageConnectionUtilizationPercentage: 70

webserver:
  maxActiveConnections: 64

storage:
  type: sql
  sql:
    databaseType: mariadb
    # host, credentials, and JDBC driver omitted
```

Autoscaling enables the metrics listener automatically. The
`autoscaling/v2` HPA uses a per-pod custom metric and an `AverageValue` target;
the default 70 percent target is emitted as the Kubernetes quantity `700m`.
The one-minute ratio smooths brief connection spikes while retaining 30
percent headroom. `utilizationMetricName` is configurable because a metrics
adapter may rename the OpenMetrics series. It must resolve to a 0..1 ratio; the
absolute connection-count gauges are not compatible with the percentage
target.

An application endpoint does not implement Kubernetes' `metrics.k8s.io` API.
Before enabling the HPA, install and configure a metrics pipeline that scrapes
every pod and publishes the selected gauge through
`custom.metrics.k8s.io`. Metrics Server supplies CPU and memory resource
metrics only and is not sufficient. The chart intentionally does not install a
cluster-wide collector or adapter.

For example, a Prometheus Adapter rule can retain pod identity and collapse
duplicate scrape targets as follows. The collector must add the `namespace`
and `pod` labels through Kubernetes service discovery:

```yaml
rules:
  - seriesQuery: 'bluemap_web_http_connection_utilization_average_1m_ratio{namespace!="",pod!=""}'
    resources:
      overrides:
        namespace: {resource: namespace}
        pod: {resource: pod}
    name:
      matches: '^bluemap_web_http_connection_utilization_average_1m_ratio$'
      as: bluemap_web_http_connection_utilization_average_1m_ratio
    metricsQuery: 'max by (namespace, pod) (<<.Series>>{<<.LabelMatchers>>})'
```

Confirm that the custom API returns one value for every Ready web pod before
relying on autoscaling:

First discover the API version served by the installed adapter:

```shell
kubectl api-versions | grep '^custom.metrics.k8s.io/'
```

Then substitute that version below:

```shell
kubectl get --raw \
  "/apis/custom.metrics.k8s.io/SERVED_VERSION/namespaces/BLUEMAP_NAMESPACE/pods/*/bluemap_web_http_connection_utilization_average_1m_ratio"
```

The adapter must retain Kubernetes namespace and pod identity; scraping one
load-balanced ClusterIP target is not enough. If the metric disappears, the
HPA keeps the current replica count and reports the fetch failure in its
conditions. Persistent client connections do not move to new pods after a
scale-up, so the ingress or load balancer must distribute new connections
across Ready replicas.

When autoscaling is enabled, `minReplicas` and `maxReplicas` replace
`replicaCount`, and the Deployment defaults to a zero-unavailable rolling
update unless `strategy` overrides it. The chart rejects file storage, SQLite,
and persistence because any configured maximum can create multiple replicas.
Size database capacity for at most
`autoscaling.maxReplicas * storage.sql.maxConnections` connections from web
pods, plus writers and other readers.

The HPA measures connection-slot pressure only. Set
`webserver.maxActiveConnections` so sustained pressure on the first relevant
per-pod bottleneck is visible in the ratio. If the SQL pool, CPU, or another
resource saturates while connection utilization remains low, this metric will
not scale early enough; use a metric for that bottleneck or lower the HTTP
connection limit after observing the workload. Idle keep-alive connections are
included, so the HTTP and SQL limits do not have a universal one-to-one ratio.
`webserver.maxActiveConnections` affects the chart-generated
`config.files.webserver.conf`; a replacement file or `config.existingConfigMap`
must set `max-active-connections` itself. The exported limit and ratios always
reflect the configuration loaded by the server.

## Structured storage configuration

The chart generates one `storages/<id>.conf` file from `storage`. Do not add
that path to `config.files` or `secretConfig.files`. Map configurations should
reference the same `storage.id`.

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

For a durable single-replica file deployment, enable `persistence`, select an
existing claim, or mount the renderer's storage with `extraVolumes` and
`extraVolumeMounts`. Generated web files remain pod-local even when map storage
is persistent. The chart reserves the `webroot` volume name and `/data/web`
mount path.

## SQL storage

The image intentionally contains no JDBC drivers. Supply the required driver
from an existing ConfigMap or with a checksum-verified download. The SQL schema
must already have been initialized by a writer.

Create a Secret containing database credentials:

```shell
kubectl -n bluemap create secret generic bluemap-database \
  --from-literal=username=bluemap \
  --from-literal=password=replace-me
```

Then configure MariaDB and a pinned Connector/J download:

```yaml
replicaCount: 3

storage:
  id: sql
  type: sql
  compression: zstd
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
```

The generated configuration includes the JDBC URL, credential environment
substitutions, connection limit, storage compression, read-only mode, and:

```hocon
driver-jar: "/drivers/jdbc-driver.jar"
driver-class: "org.mariadb.jdbc.Driver"
```

`connectionUrl` can replace the generated JDBC URL when driver-specific
parameters are required. To provide the driver from a ConfigMap instead:

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

The ConfigMap and download sources are mutually exclusive. A download requires
a 64-character SHA-256 checksum. Kubernetes limits ConfigMap objects to 1 MiB,
so a verified download is often preferable for larger drivers.

MariaDB, MySQL, PostgreSQL, and SQLite are supported for one replica. SQLite
uses `storage.sql.database` as its file path and cannot be scaled horizontally.
If `credentials.existingSecret` is empty, the chart creates a Secret from the
inline username and password; those values remain in the Helm release, so an
externally managed Secret is recommended.

### Cache-metadata upgrade

BlueMap adds nullable cache-validator columns to existing SQL storage tables.
Readers remain compatible while a current writer adds those columns.

For PostgreSQL, stop all older writers before starting the first version that
writes cache metadata, and do not let an older writer use that database again.
An older PostgreSQL upsert can replace map data while preserving its previous
hash and timestamp, which would expose stale HTTP validators. Read-only web
replicas may remain online during the upgrade; only writer versions must not
overlap.

## HTTP caching and stored compression

The server emits storage-backed validators and cache policy for web assets,
map settings, tiles, textures, markers, and live data. Conditional requests can
return `304 Not Modified` without loading an unchanged body. The validator
metadata lives with the map data rather than in a web-replica cache, so all
replicas make consistent decisions.

Map data remains in its configured storage encoding and is served without
transcoding. Set `storage.compression` to `none`, `gzip`, `deflate`, `zstd`, or
`lz4` to match the writer. Clients that do not advertise the required encoding
receive `406 Not Acceptable` with the required coding identified, rather than a
mislabelled or undecodable response. Choose an encoding supported by the
browsers and intermediaries that access the map. Client-decompression `.gz`
URLs are the narrow exception: they return raw gzip files and transcode only
when the stored representation is not gzip.

`tile-cache-max-age` in `webserver.conf` controls the tile freshness lifetime.
Settings and mutable data revalidate; player data remains private and is not
stored by shared caches.

## Configuration sources

- `config.files` creates non-sensitive configuration in a ConfigMap. Keys are
  paths relative to `/config`, and values support Helm `tpl` expressions.
- `config.existingConfigMap` and `config.items` mount an existing ConfigMap.
- `secretConfig.existingSecret` and `secretConfig.items` mount sensitive
  configuration.
- `secretConfig.files` is convenient for local use, but secrets should not be
  committed or placed in command-line history.

The generated storage file is projected alongside these sources.

## Health checks and shutdown

The TCP liveness probe verifies that the listener exists without consuming a
bounded HTTP connection slot. `/health/ready` reflects webroot and storage
health from background checks, so a stalled dependency does not block the
probe itself or remain healthy indefinitely.

On termination the server stops accepting traffic, becomes unready, and drains
active responses for `shutdownGracePeriodSeconds`. The chart requires
`terminationGracePeriodSeconds` to be larger so Kubernetes leaves time for
forced connection closure and process cleanup.

## Live data

The standalone process does not receive the Minecraft server's in-memory
events, so SSE is disabled. Set BlueMap's `write-players-interval` and
`write-markers-interval` to positive values on the Minecraft server so web
replicas can read persisted player and marker updates.
