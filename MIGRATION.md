# SQL storage migration

This release adds `content_hash` and `updated_at` columns to BlueMap's SQL item
and grid storage tables. BlueMap uses these columns for HTTP cache validators.

## Upgrade order

Do not run an older PostgreSQL writer after the new columns have been added.
Older PostgreSQL upserts can change stored bytes without clearing the cache
metadata. A standalone web server could then return `304 Not Modified` for
content that has changed.

Use this order for an SQL-backed installation:

1. Back up the BlueMap database.
2. Stop every BlueMap process that can write map data. This includes server
   plugins, mods, render workers, and CLI render jobs whose SQL storage has
   `read-only: false`.
3. Upgrade one writer and start it with `read-only: false`. Its normal storage
   initialization adds the cache-metadata columns if they are missing.
4. Check that the writer completed storage initialization without an SQL
   migration error.
5. Upgrade every remaining writer before starting it. Do not restart an older
   writer against the migrated PostgreSQL database.
6. Start standalone web-server replicas with `read-only: true`. Read-only
   replicas validate the schema at startup and refuse to start if the migration
   has not completed.

For PostgreSQL, this query should return four rows after step 3:

```sql
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND table_name IN (
    'bluemap_item_storage_data',
    'bluemap_grid_storage_data'
  )
  AND column_name IN ('content_hash', 'updated_at')
ORDER BY table_name, column_name;
```

## If an older PostgreSQL writer ran after migration

Stop all BlueMap writers and standalone web servers before repairing the
metadata. Readers must remain stopped until the transaction commits; otherwise
they can continue returning stale `304 Not Modified` responses during the
repair.

The updates touch every stored row and can generate substantial PostgreSQL WAL.
Check free database and WAL/archive space before starting. Then invalidate the
validators that may be stale:

```sql
BEGIN;
UPDATE bluemap_item_storage_data
SET content_hash = NULL, updated_at = NULL;
UPDATE bluemap_grid_storage_data
SET content_hash = NULL, updated_at = NULL;
COMMIT;
```

This does not delete map data. Responses for rows with cleared metadata omit
`ETag` and `Last-Modified` until an upgraded writer rewrites those rows. A
forced/full render that rewrites the affected rows repopulates the metadata;
an incremental render may leave unchanged rows without validators.

## Rollback

Stop the new read-only web servers before rolling writers back. The added
columns do not prevent an older writer from using the tables, but PostgreSQL
cache metadata becomes unsafe once that writer changes a row. Clear the
metadata with the repair transaction above before deploying this release
again.
