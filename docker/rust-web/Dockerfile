# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS webapp
WORKDIR /src/common/webapp
COPY common/webapp/package.json common/webapp/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY common/webapp/ ./
RUN npm run build \
    && test -s dist/lang/settings.conf \
    && test -s dist/assets/logo.png

FROM rust:1.97-alpine AS rust-builder
RUN apk add --no-cache binutils build-base file musl-dev perl
WORKDIR /src/implementations/rust-webserver
COPY implementations/rust-webserver/Cargo.toml implementations/rust-webserver/Cargo.lock ./
COPY implementations/rust-webserver/.cargo ./.cargo
COPY implementations/rust-webserver/src ./src
ARG BLUEMAP_VERSION=development
ENV BLUEMAP_VERSION=${BLUEMAP_VERSION}
ENV CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=gcc
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/implementations/rust-webserver/target \
    cargo build --release --locked --target x86_64-unknown-linux-musl \
    && cp target/x86_64-unknown-linux-musl/release/bluemap-rust-webserver /bluemap-rust-webserver \
    && file /bluemap-rust-webserver \
    && ! readelf -lW /bluemap-rust-webserver | grep -q 'INTERP' \
    && ! readelf -dW /bluemap-rust-webserver | grep -q '(NEEDED)'

FROM alpine:3.22 AS certificates
RUN apk add --no-cache ca-certificates

FROM scratch
ARG BLUEMAP_VERSION=development
ARG REVISION
ARG CREATED
ARG SOURCE="https://github.com/BlueMap-Minecraft/BlueMap"
LABEL org.opencontainers.image.title="BlueMap Rust Web" \
      org.opencontainers.image.description="Static standalone BlueMap web UI and storage reader" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${BLUEMAP_VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

COPY --from=certificates /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=rust-builder /bluemap-rust-webserver /usr/local/bin/bluemap-rust-webserver
COPY --from=webapp /src/common/webapp/dist /usr/share/bluemap-web

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
USER 10001:10001
EXPOSE 8100
ENTRYPOINT ["/usr/local/bin/bluemap-rust-webserver"]
CMD ["--config", "/etc/bluemap-web/config.toml"]
