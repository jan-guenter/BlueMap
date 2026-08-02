# RunPod role-image contract

These five images are disposable benchmark wrappers. No credentials, private
keys, TLS private material, database contents, or generated SSH host keys are
stored in an image layer.

Every role starts only `sshd` at first. A single public key is supplied through
`BENCHMARK_SSH_PUBLIC_KEY` (or uploaded as `/bootstrap/authorized_keys`).
Password, keyboard-interactive, agent-forwarding, X11, and TUN-device forwarding
are disabled. TCP forwarding remains enabled because the isolated benchmark
control path needs it. Host keys are deleted during the build and generated uniquely at
container start.

The role process remains stopped until its required files and the final regular
file `/bootstrap/start` exist. Upload the marker last. In particular:

Before uploading that marker, the controller must install and capture a
source-IP firewall. Candidate port 8100 accepts only the frozen load-generator
IPv4 and loopback. MariaDB port 3306 accepts only the frozen benchmark-client
IPv4s and loopback. The run fails before application startup if `iptables`
cannot enforce those rules.

- `upstream` and `java` require `/bootstrap/config`, the MariaDB JDBC driver
  at `/bootstrap/jdbc/mariadb-java-client.jar`, and the MariaDB CA at
  `/bootstrap/tls/ca.crt`. The uploaded JDBC URL uses
  `sslMode=verify-full`, that CA as `serverSslCert`, and disables fallback to
  the system trust store. Both wrappers install the same
  `openssh-server`, `curl`, `iproute2`, `iptables`, `procps`, and `util-linux`
  helper set and run
  the application as UID/GID 10001. Both reject an active webserver log-file
  setting and require exactly one active `port: 8100`; the upstream CLI is not
  run in verbose mode. No process binds 8100 before authorization.
- `php` requires `/bootstrap/php/database.json` and
  `/bootstrap/tls/ca.crt`. The JSON contains `host`, `port`, `tlsServerName`,
  `username`, `password`, `database`, and optional `localPort`. It configures
  only the six connection assignments above the immutable marker in upstream
  `sql.php`. The request and SQL logic after that marker is byte-for-byte
  upstream. nginx access logging, nginx/FastCGI caching, buffering, gzip, and
  output compression are disabled. A local stunnel verifies the MariaDB CA and checks a DNS SAN with
  `checkHost` or an IP SAN with `checkIP`, matching the uploaded identity.
- `mariadb` requires an empty `/var/lib/mysql`, CA/server TLS material, the
  zstd SQL snapshot, a root-password file, and benchmark-user initialization
  SQL under `/bootstrap/database`. It verifies the certificate/key/snapshot,
  then delegates to the exact official entrypoint with
  `require_secure_transport=ON`. Port 3306 is not bound before authorization.
- `loadgen` contains k6, Python, OpenSSH client/server, HAProxy, MariaDB client,
  age, rclone, and diagnostic networking tools. After authorization it executes
  its command (an idle `sleep infinity` by default), allowing the controller to
  upload and execute the frozen benchmark over SSH.

The idle SSH daemon and diagnostic packages are wrapper overhead. Resource
capture must identify the application processes separately; whole-container
measurements retain the helper overhead. The upstream and Java candidates use
the same helper package set so their wrapper overhead is structurally matched.

The workflow publishes each push with a unique run/attempt/source tag. Java and
PHP use the existing public `bluemap-web` and `bluemap-web-php` packages. The
load-generator, upstream wrapper, and MariaDB wrapper use role-prefixed tags in
the existing public `bluemap-perf-loadgen` package. The build-lock artifact maps
each role to its exact tag, digest, immutable reference, Dockerfile hash, and
deletion identity.
