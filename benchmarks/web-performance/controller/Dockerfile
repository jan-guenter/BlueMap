FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

ARG SOURCE_REPOSITORY="https://github.com/jan-guenter/BlueMap.git"
ARG REVISION
ARG VERSION="formal"
ARG CREATED
ARG KUBECTL_VERSION="v1.36.2"
ARG KUBECTL_SHA256="1e9045ec32bea85da43de85f0065358529ea7c7a152eca78154fba5b58c27d82"

LABEL org.opencontainers.image.title="BlueMap formal benchmark controller" \
      org.opencontainers.image.description="Durable controller for the frozen RunPod-backed BlueMap benchmark" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

RUN set -eux; \
    case "${REVISION}" in \
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]* ) ;; \
        * ) echo "REVISION must be a full lowercase Git SHA" >&2; exit 1 ;; \
    esac; \
    test "${#REVISION}" -eq 40; \
    case "${SOURCE_REPOSITORY}" in \
        https://github.com/*/*.git ) ;; \
        * ) echo "SOURCE_REPOSITORY must be a credential-free GitHub HTTPS URL ending in .git" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        curl \
        diffutils \
        git \
        jq \
        openssh-client \
        tini; \
    curl --fail --location --silent --show-error \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        --output /usr/local/bin/kubectl; \
    echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum --check --strict; \
    chmod 0755 /usr/local/bin/kubectl; \
    python -m pip install --no-cache-dir "zstandard==0.25.0"; \
    groupadd --gid 12345 controller; \
    useradd \
        --uid 12345 \
        --gid 12345 \
        --no-create-home \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        controller; \
    install -d /opt/bluemap; \
    install -d -o controller -g controller \
        /opt/bluemap-runtime \
        /opt/bluemap-runtime/credentials \
        /tmp

RUN set -eux; \
    git init /opt/bluemap; \
    git -C /opt/bluemap remote add origin "${SOURCE_REPOSITORY}"; \
    git -C /opt/bluemap fetch --depth=1 origin "${REVISION}"; \
    git -C /opt/bluemap checkout --detach FETCH_HEAD; \
    test "$(git -C /opt/bluemap rev-parse HEAD)" = "${REVISION}"; \
    git -C /opt/bluemap status --porcelain --untracked-files=no | test ! -s /dev/stdin; \
    chown -R controller:controller /opt/bluemap

COPY --chown=12345:12345 \
    benchmarks/web-performance/controller/frozen/ \
    /opt/bluemap/benchmarks/web-performance/controller/frozen/

COPY --chown=12345:12345 \
    benchmarks/web-performance/controller/entrypoint.sh \
    /usr/local/bin/bluemap-formal-controller
COPY --chown=12345:12345 \
    benchmarks/web-performance/controller/kubeconfig.yaml \
    /etc/bluemap-controller/kubeconfig
COPY --chown=12345:12345 \
    benchmarks/web-performance/controller/validate_frozen_bundle.py \
    /usr/local/lib/bluemap-controller/validate_frozen_bundle.py

RUN set -eux; \
    chmod 0555 /usr/local/bin/bluemap-formal-controller; \
    chmod 0444 /etc/bluemap-controller/kubeconfig; \
    chmod 0444 /usr/local/lib/bluemap-controller/validate_frozen_bundle.py; \
    bash -n /usr/local/bin/bluemap-formal-controller; \
    test -d \
        /opt/bluemap/benchmarks/web-performance/controller/formal; \
    python -m py_compile \
        /opt/bluemap/benchmarks/web-performance/controller/formal/analyze.py \
        /opt/bluemap/benchmarks/web-performance/controller/formal/freeze.py \
        /opt/bluemap/benchmarks/web-performance/controller/formal/orchestrate.py; \
    install -d -o controller -g controller \
        /opt/bluemap/benchmarks/web-performance/artifacts/formal-runs; \
    export GIT_CONFIG_COUNT=1; \
    export GIT_CONFIG_KEY_0=safe.directory; \
    export GIT_CONFIG_VALUE_0=/opt/bluemap; \
    GIT_OPTIONAL_LOCKS=0 \
        python /usr/local/lib/bluemap-controller/validate_frozen_bundle.py \
        --repository /opt/bluemap \
        --revision "${REVISION}"; \
    GIT_OPTIONAL_LOCKS=0 \
        python \
        /opt/bluemap/benchmarks/web-performance/controller/formal/orchestrate.py \
        validate

ENV BENCHMARK_PYTHON="/usr/local/bin/python" \
    GIT_OPTIONAL_LOCKS="0" \
    HOME="/nonexistent" \
    KUBECONFIG="/etc/bluemap-controller/kubeconfig" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

WORKDIR /opt/bluemap
USER 12345:12345

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bluemap-formal-controller"]
