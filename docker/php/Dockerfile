FROM php:8.4-fpm-alpine

ARG VERSION
ARG REVISION
ARG CREATED
ARG SOURCE="https://github.com/BlueMap-Minecraft/BlueMap"

LABEL org.opencontainers.image.title="BlueMap SQL PHP-FPM" \
      org.opencontainers.image.description="BlueMap SQL map-data endpoint for PHP-FPM" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

RUN apk add --no-cache libpq \
    && apk add --no-cache --virtual .build-deps postgresql-dev \
    && docker-php-ext-install -j"$(nproc)" opcache pdo_mysql pdo_pgsql \
    && apk del .build-deps \
    && mv "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini"

COPY common/webapp/public/sql.php /var/www/html/sql.php
COPY docker/php/health.php /var/www/html/health.php
COPY docker/php/opcache.ini /usr/local/etc/php/conf.d/opcache.ini

RUN chown -R www-data:www-data /var/www/html

USER www-data

EXPOSE 9000

CMD ["php-fpm", "-F"]
