FROM php:8.4-fpm-alpine3.22@sha256:e0f1ffe8bd43a137facdccdcac41cabdfc23e8d0fdd0665916a062f16e2f4058

ARG CREATED
ARG REVISION
ARG SOURCE
ARG VERSION

LABEL org.opencontainers.image.title="BlueMap PHP RunPod benchmark wrapper" \
      org.opencontainers.image.description="Exact upstream sql.php behind nginx, PHP-FPM, and a CA-verifying MariaDB TLS tunnel" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="docker.io/library/php:8.4-fpm-alpine3.22" \
      org.opencontainers.image.base.digest="sha256:e0f1ffe8bd43a137facdccdcac41cabdfc23e8d0fdd0665916a062f16e2f4058" \
      io.bluemap.benchmark.application-revision="e664c1abdf697c64703401dca1d7e1956f755f65" \
      io.bluemap.benchmark.sql-php-sha256="e160a9ecbd996b5c701f172a7b22bf73eec96670cf6508034e18251067bebb6b" \
      io.bluemap.benchmark.wrapper.packages="openssh-server,curl,iproute2,procps,nginx,stunnel"

RUN apk add --no-cache \
        curl \
        iproute2 \
        nginx \
        openssh-server \
        procps \
        stunnel \
    && apk add --no-cache --virtual .build-deps $PHPIZE_DEPS \
    && docker-php-ext-install -j"$(getconf _NPROCESSORS_ONLN)" pdo_mysql \
    && apk del .build-deps \
    && install -d -m 0755 /bootstrap /opt/bluemap /srv/bluemap /run/nginx /run/sshd /run/stunnel \
    && rm -f /etc/nginx/http.d/default.conf /etc/ssh/ssh_host_*

COPY common/webapp/public/sql.php /opt/bluemap/sql.php.upstream
RUN echo "e160a9ecbd996b5c701f172a7b22bf73eec96670cf6508034e18251067bebb6b  /opt/bluemap/sql.php.upstream" \
      | sha256sum -c -

COPY benchmarks/web-throughput/images/common/bootstrap.sh /usr/local/libexec/bluemap-bootstrap.sh
COPY benchmarks/web-throughput/images/common/sshd_config /etc/ssh/sshd_config
COPY benchmarks/web-throughput/images/php/nginx.conf /etc/nginx/nginx.conf
COPY benchmarks/web-throughput/images/php/php-fpm-www.conf /usr/local/etc/php-fpm.d/zz-benchmark.conf
COPY benchmarks/web-throughput/images/php/php.ini /usr/local/etc/php/conf.d/zz-benchmark.ini
COPY --chmod=0755 benchmarks/web-throughput/images/php/entrypoint.sh /usr/local/bin/bluemap-php-entrypoint

EXPOSE 22

STOPSIGNAL SIGQUIT

ENTRYPOINT ["/usr/local/bin/bluemap-php-entrypoint"]
