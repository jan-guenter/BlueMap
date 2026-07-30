# BlueMap Rust web image

The Dockerfile builds the unmodified upstream webapp and an x86_64 static-musl
Rust binary, then copies both into a non-root `scratch` image. The result has no
shell, package manager, Java runtime, PHP runtime, or required writable path.

Mount TOML configuration, storage, and TLS material read-only. Kubernetes
probes should use `/health/live` and `/health/ready`.
