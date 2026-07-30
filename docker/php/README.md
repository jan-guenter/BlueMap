# BlueMap SQL PHP-FPM container

This image packages BlueMap's official `sql.php` map-data endpoint with
PHP-FPM and the PDO MySQL/MariaDB and PostgreSQL extensions. It is intended for
the optional SQL data tier in the BlueMap Web Helm chart.

The packaged script is the unchanged upstream compatibility baseline. It does
not consume BlueMap's newer SQL cache metadata.

The script reads these environment variables:

- `BLUEMAP_SQL_PDO_DRIVER` (`mysql` or `pgsql`)
- `BLUEMAP_SQL_HOST`
- `BLUEMAP_SQL_PORT`
- `BLUEMAP_SQL_DATABASE`
- `BLUEMAP_SQL_USERNAME`
- `BLUEMAP_SQL_PASSWORD`

PHP-FPM listens on port `9000`. It speaks FastCGI rather than HTTP, so the Helm
chart runs an unprivileged NGINX sidecar in front of it and exposes that
sidecar through a Kubernetes Service.
