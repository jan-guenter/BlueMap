# BlueMap Rust webserver experiment

This component is a read-only, standalone BlueMap web UI and map-data server.
It does not load Minecraft, render maps, mutate BlueMap's schema, or implement
the BlueMap API. It supports BlueMap file storage, MariaDB through its MySQL
wire protocol, and PostgreSQL.

MySQL and SQLite are intentionally outside this MVP. BlueMap's LZ4 storage uses
Java's block-stream format rather than the standard LZ4 frame format. The
server still supports it as an opaque pass-through representation: normal
browsers do not advertise `lz4` and receive the structured HTTP 406 response,
while an explicit client can request the unchanged bytes.

## HTTP behavior

The service provides the upstream web UI, a generated `/settings.json`, map
objects below `/maps/<map-id>/`, and:

- `/health/live`, which reports that the HTTP process is running;
- `/health/ready`, which reports the latest storage dependency check;
- `/maps/<map-id>/live/sse`, which deliberately returns 404 so the webapp uses
  polling.

Startup validates the schema and settings object for every configured map.
Subsequent readiness polling is deliberately cheap: one `SELECT 1` for SQL, or
a reopen/stat check through the pre-opened file-storage root handle. It does
not repeat per-map queries or filesystem stats every interval.

The Minecraft BlueMap plugin must persist live data for polling:

```hocon
write-players-interval: 3
write-markers-interval: 10
```

Stored objects are never decompressed or transcoded. Gzip, deflate, zstd, and
BlueMap block-stream LZ4 objects are returned only if `Accept-Encoding` permits
that exact encoding. Otherwise
the service returns an `application/problem+json` HTTP 406 with
`Cache-Control: no-store,no-transform`, `Vary: Accept-Encoding`, and
`X-BlueMap-Required-Content-Encoding`. The body identifies
`bluemap_required_content_encoding` and the required coding. `identity` remains
implicitly acceptable unless `identity;q=0` or `*;q=0` explicitly rejects it;
an omitted `Accept-Encoding` header permits every content coding.

Tile URLs are accepted only in BlueMap's canonical character-sharded form:
hires tiles end exactly in `.prbm`, low-resolution tiles end exactly in
`.png`, and storage-compression suffixes never appear in the HTTP URL. This
keeps multiple aliases from referring to the same cache object.

Tiles use `public,max-age=60,must-revalidate,no-transform` by default; set
`tile_cache_max_age_seconds` to change the age. Settings, textures, assets, and
markers use `public,no-cache,no-transform`. Players use
`private,no-store,no-transform`. Static webapp files are revalidated unless
their `/assets/` filename contains a build fingerprint, in which case they use
a one-year immutable policy.

Current BlueMap SQL schemas store validator hashes as `BINARY(32)` on MariaDB
and `BYTEA` on PostgreSQL, with update times as epoch-millisecond `BIGINT`
values. The service projects the hashes to lowercase hexadecimal text and
converts the millisecond timestamps to HTTP dates. It also remains compatible
with an older schema where those nullable columns are absent.

## Configuration and security

TOML is the only application configuration format. See the three
`config.*.example.toml` files. Passwords are read from the configured
environment variable. Database usernames can be literal (`username`) or read
from `username_env`; configure exactly one. The database examples use
`BLUEMAP_DATABASE_USERNAME` so Kubernetes Secrets can supply both credentials.
Credential values are redacted from debug output and configuration errors.

Map IDs use BlueMap's canonical letters/digits/underscore form. File storage
opens every path relative to a pre-opened root directory and rejects symlink
components. This keeps a writer with access to the map tree from redirecting
the read-only server to files outside that tree. A normal GET uses one securely
opened descriptor for its headers and body. The body is read in bounded chunks,
never beyond the initially advertised length, and the descriptor is checked
again before the final chunk is released. Atomic path replacement keeps serving
the opened version; in-place changes truncate the fixed-length response instead
of completing with mixed data. File opens, metadata calls, and individual
chunks run on explicit blocking workers. Their concurrency is the smaller of
`max_in_flight_requests` and eight, and a worker keeps its permit after an HTTP
timeout or cancellation until the underlying filesystem syscall really
returns. This prevents a stalled NFS mount from filling Tokio's blocking pool.

Database TLS modes are `disable`, `required`, `verify-ca`, and `verify-full`
(the default). A custom CA and optional client certificate/key can be mounted
read-only. Certificate and key must be configured together, and TLS material
cannot be configured when the mode is `disable`.
PostgreSQL connection options are TOML-only: `PGSSLROOTCERT`, `PGSSLCERT`,
`PGSSLKEY`, `PGOPTIONS`, and `PGAPPNAME` must be unset so ambient process
configuration cannot silently alter TLS or session behavior.
`webapp.map_data_root` and `webapp.live_data_root` must remain `maps`; this
matches the `/maps` route implemented by this focused server.

SQL users need only `SELECT` on the existing six `bluemap_*` tables. The
Minecraft BlueMap instance owns schema creation and updates. Each replica has a
bounded pool; keep `replicas * max_connections` within the database budget.
Serving-time SQL connections are detached and discarded if their configured
storage deadline is cancelled. This prevents a silent database or network
failure from stranding a pool permit in SQLx 0.8.6's unbounded on-release ping
([upstream issue #4349](https://github.com/transact-rs/sqlx/issues/4349));
successful return-to-pool checks remain inside the same deadline. SQLx's
implicit pre-acquire ping is disabled so I/O on an idle pooled connection
cannot begin before that guard owns it; the guarded query detects and discards
a stale connection instead.
Keep the per-replica `max_in_flight_requests` near `max_connections`, so the
aggregate in-flight limit stays near the aggregate pool capacity instead of
retaining many SQL BLOBs above it. File deployments with multiple replicas
require a shared RWX filesystem with reliable read-after-rename semantics.
`storage_timeout_seconds` bounds startup discovery, HTTP storage queries, and
readiness checks, as well as each blocking file open and body-chunk operation.
File status and headers are sent after the open, before the complete body is
read. A later chunk timeout or I/O failure aborts the fixed-length body; it
cannot be converted into a late HTTP error response. The shutdown grace is a
total budget shared by stopping the health monitor, HTTP draining, and
database-pool cleanup. After that, `runtime_shutdown_seconds` bounds how long
process exit waits for uncancellable blocking filesystem workers. Kubernetes
must allow both budgets plus a small termination margin; the Helm chart
calculates that total automatically.

`max_in_flight_requests` is a non-queuing map-response limit (default `8`,
valid range `1..=1024`). `max_object_bytes` rejects any individual stored
object above its default 32 MiB limit. File storage checks the securely opened
descriptor before creating the response and streams at most that advertised
length. SQL checks metadata before its body query and uses a size-bounded
projection so an object that grows between queries is represented by metadata
rather than materialized as a BLOB. Oversize responses are HTTP 500 problem
documents with
`Cache-Control: no-store,no-transform`; logs contain only the observed and
configured byte counts.
Excess map requests receive HTTP 503 with `Retry-After: 1`. A permit remains
attached for the response-body lifetime. File responses retain only a bounded
chunk and one open descriptor per active request; keep a raised in-flight limit
within the container's file-descriptor headroom. SQL responses remain
materialized BLOBs after their database connections return to the pool. The
largest texture object in the reference data was 20.2 MiB. Eight simultaneous
SQL objects can therefore account for at least 161.6 MiB before database, TLS,
allocator, runtime, and webapp overhead. SQL decoding can transiently retain
both the database row and response copy, so budget SQL deployments for roughly
twice
`max_in_flight_requests * max_object_bytes`, plus database, TLS, allocator,
runtime, and webapp overhead. The Helm examples use a 1 GiB limit; lower the
object or in-flight limit when using a smaller SQL pod memory budget.

## Build and test

```sh
cargo fmt --check
cargo clippy --target x86_64-unknown-linux-gnu --all-targets -- -D warnings
cargo test --target x86_64-unknown-linux-gnu
cargo build --release --locked --target x86_64-unknown-linux-musl
```

Build the static, non-root image (which also builds the upstream webapp):

```sh
docker build -f docker/rust-web/Dockerfile \
  --build-arg BLUEMAP_VERSION=development \
  -t bluemap-rust-web:development .
```

`docker/rust-web/smoke-test.sh` builds the image, starts it read-only, and
verifies that the language settings and a public image asset are served.

Run with an immutable root:

```sh
docker run --rm --read-only --user 10001:10001 -p 8100:8100 \
  -v "$PWD/config.toml:/etc/bluemap-web/config.toml:ro" \
  -v "$PWD/maps:/data/maps:ro" \
  bluemap-rust-web:development
```

Use `RUST_LOG=debug` for diagnostics. Credentials are not logged.
