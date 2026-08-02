/*
 * This file is part of BlueMap, licensed under the MIT License (MIT).
 *
 * Copyright (c) Blue (Lukas Rieger) <https://bluecolored.de>
 * Copyright (c) contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */
package de.bluecolored.bluemap.core.storage.sql.commandset;

import com.github.benmanes.caffeine.cache.LoadingCache;
import de.bluecolored.bluemap.core.logger.Logger;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.storage.sql.Database;
import de.bluecolored.bluemap.core.util.Caches;
import de.bluecolored.bluemap.core.util.Key;
import lombok.RequiredArgsConstructor;
import org.intellij.lang.annotations.Language;
import org.jetbrains.annotations.Nullable;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.*;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.TimeUnit;

@SuppressWarnings("SqlSourceToSinkFlow")
@RequiredArgsConstructor
public abstract class AbstractCommandSet implements CommandSet {

    private static final Set<String> REQUIRED_TABLES = Set.of(
            "bluemap_map",
            "bluemap_compression",
            "bluemap_item_storage",
            "bluemap_item_storage_data",
            "bluemap_grid_storage",
            "bluemap_grid_storage_data"
    );

    protected final Database db;
    private volatile @Nullable Boolean cacheMetadataEnabled;

    // Map rows can be purged and recreated by another process. Expiring this
    // identity after write prevents a busy read replica from retaining the old
    // numeric key indefinitely while still avoiding one lookup per request.
    protected final LoadingCache<String, Integer> mapKeys =
            Caches.buildExpiringAfterWrite(
                    1, TimeUnit.SECONDS, this::findOrCreateMapKey
            );
    protected final LoadingCache<Compression, Integer> compressionKeys = Caches.build(this::findOrCreateCompressionKey);
    protected final LoadingCache<Key, Integer> itemStorageKeys = Caches.build(this::findOrCreateItemStorageKey);
    protected final LoadingCache<Key, Integer> gridStorageKeys = Caches.build(this::findOrCreateGridStorageKey);

    @Override
    public @Nullable ReadPermit tryAcquireReadPermit() {
        Database.ReadPermit permit = db.tryAcquireReadPermit();
        return permit == null ? null : permit::close;
    }

    @Language("sql")
    public abstract String listExistingTablesStatement();

    @Language("sql")
    public abstract String createMapTableStatement();

    @Language("sql")
    public abstract String createCompressionTableStatement();

    @Language("sql")
    public abstract String createItemStorageTableStatement();

    @Language("sql")
    public abstract String createItemStorageDataTableStatement();

    @Language("sql")
    public abstract String createGridStorageTableStatement();

    @Language("sql")
    public abstract String createGridStorageDataTableStatement();

    @Language("sql")
    public String addItemContentHashColumnStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Language("sql")
    public String addItemUpdatedAtColumnStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Language("sql")
    public String addGridContentHashColumnStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Language("sql")
    public String addGridUpdatedAtColumnStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    /**
     * Opts a dialect into the cache-metadata schema and extended statements.
     *
     * <p>The default preserves the statement shapes used by existing custom
     * {@code AbstractCommandSet} subclasses.</p>
     */
    protected boolean supportsCacheMetadata() {
        return false;
    }

    private boolean cacheMetadataEnabled() {
        Boolean enabled = cacheMetadataEnabled;
        if (enabled != null) return enabled;

        enabled = supportsCacheMetadata();
        cacheMetadataEnabled = enabled;
        return enabled;
    }

    /**
     * Returns whether this instance still inherits all four legacy statement
     * shapes from the supplied built-in dialect.
     *
     * <p>Those methods were public extension points before cache metadata was
     * introduced. A subclass overriding any one of them must keep using all
     * legacy arities, while a no-op subclass can safely inherit the built-in
     * metadata-aware statements.</p>
     */
    protected final boolean usesBuiltInStatementShapes(
            Class<? extends AbstractCommandSet> dialect
    ) {
        try {
            return getClass()
                    .getMethod("itemStorageWriteStatement")
                    .getDeclaringClass() == dialect
                    && getClass()
                    .getMethod("itemStorageReadStatement")
                    .getDeclaringClass() == dialect
                    && getClass()
                    .getMethod("gridStorageWriteStatement")
                    .getDeclaringClass() == dialect
                    && getClass()
                    .getMethod("gridStorageReadStatement")
                    .getDeclaringClass() == dialect;
        } catch (NoSuchMethodException ex) {
            throw new IllegalStateException(
                    "Required SQL statement method is missing",
                    ex
            );
        }
    }

    @Override
    public void initializeTables() throws IOException {
        db.run(connection -> {
            boolean tablesComplete = false;
            try {
                Set<String> tables = new HashSet<>(6);
                ResultSet result = executeQuery(connection, listExistingTablesStatement());
                while (result.next()) {
                    tables.add(result.getString(1));
                }

                tablesComplete = tables.containsAll(REQUIRED_TABLES);
            } catch (SQLException ex) {
                Logger.global.logWarning("Failed to check for existing tables, will try to create them...");
                Logger.global.logDebug(ex.toString());
            }

            if (!tablesComplete) {
                // create tables (if not exists)
                executeUpdate(connection, createMapTableStatement());
                executeUpdate(connection, createCompressionTableStatement());
                executeUpdate(connection, createItemStorageTableStatement());
                executeUpdate(connection, createItemStorageDataTableStatement());
                executeUpdate(connection, createGridStorageTableStatement());
                executeUpdate(connection, createGridStorageDataTableStatement());
            }
        });

        if (cacheMetadataEnabled()) {
            addColumnIfMissing("bluemap_item_storage_data", "content_hash",
                    addItemContentHashColumnStatement());
            addColumnIfMissing("bluemap_item_storage_data", "updated_at",
                    addItemUpdatedAtColumnStatement());
            addColumnIfMissing("bluemap_grid_storage_data", "content_hash",
                    addGridContentHashColumnStatement());
            addColumnIfMissing("bluemap_grid_storage_data", "updated_at",
                    addGridUpdatedAtColumnStatement());
        }
    }

    @Override
    public void validateTables() throws IOException {
        db.run(connection -> {
            Set<String> tables = new HashSet<>(REQUIRED_TABLES.size());
            try (ResultSet result = executeQuery(
                    connection, listExistingTablesStatement()
            )) {
                while (result.next()) tables.add(result.getString(1));
            }

            Set<String> missingTables = new HashSet<>(REQUIRED_TABLES);
            missingTables.removeAll(tables);
            if (!missingTables.isEmpty()) {
                throw new IOException(
                        "SQL storage schema is not initialized; missing tables: "
                                + String.join(", ", missingTables)
                );
            }

            if (!cacheMetadataEnabled()) return;

            List<String> missingColumns = new ArrayList<>(4);
            addMissingColumn(
                    connection, missingColumns,
                    "bluemap_item_storage_data", "content_hash"
            );
            addMissingColumn(
                    connection, missingColumns,
                    "bluemap_item_storage_data", "updated_at"
            );
            addMissingColumn(
                    connection, missingColumns,
                    "bluemap_grid_storage_data", "content_hash"
            );
            addMissingColumn(
                    connection, missingColumns,
                    "bluemap_grid_storage_data", "updated_at"
            );
            if (!missingColumns.isEmpty()) {
                throw new IOException(
                        "SQL storage schema requires migration; missing columns: "
                                + String.join(", ", missingColumns)
                );
            }
        });
    }

    private static void addMissingColumn(
            Connection connection,
            List<String> missing,
            String table,
            String column
    ) throws SQLException {
        if (!columnExists(connection, table, column)) {
            missing.add(table + "." + column);
        }
    }

    @Language("sql")
    public abstract String itemStorageWriteStatement();

    @Language("sql")
    public String itemStorageWriteWithMetadataStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Override
    public void writeItem(String mapId, Key key, Compression compression, byte[] bytes) throws IOException {
        int mapKey = mapKey(mapId);
        int storageKey = itemStorageKey(key);
        int compressionKey = compressionKey(compression);
        if (cacheMetadataEnabled()) {
            byte[] contentHash = sha256(bytes);
            long updatedAt = System.currentTimeMillis();
            db.run(connection -> executeUpdate(connection,
                    itemStorageWriteWithMetadataStatement(),
                    mapKey, storageKey, compressionKey,
                    bytes, contentHash, updatedAt
            ));
        } else {
            db.run(connection -> executeUpdate(connection,
                    itemStorageWriteStatement(),
                    mapKey, storageKey, compressionKey, bytes
            ));
        }
    }

    @Language("sql")
    public abstract String itemStorageReadStatement();

    @Language("sql")
    public String itemStorageReadWithMetadataStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Override
    public byte @Nullable [] readItem(
            String mapId, Key key, Compression compression
    ) throws IOException {
        StoredData stored = readItemData(mapId, key, compression);
        return stored == null ? null : stored.data();
    }

    @Override
    public @Nullable StoredData readItemData(
            String mapId, Key key, Compression compression
    ) throws IOException {
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findItemStorageKey(key);
        Integer compressionKey = findCompressionKey(compression);
        if (mapKey == null || storageKey == null || compressionKey == null) return null;
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    cacheMetadataEnabled()
                            ? itemStorageReadWithMetadataStatement()
                            : itemStorageReadStatement(),
                    mapKey, storageKey, compressionKey
            );
            if (!result.next()) return null;
            if (!cacheMetadataEnabled()) {
                return new StoredData(result.getBytes(1), null, 0);
            }
            return new StoredData(
                    result.getBytes(1), result.getBytes(2), result.getLong(3)
            );
        });
    }

    @Language("sql")
    public String itemStorageReadMetadataStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Override
    public @Nullable StoredMetadata readItemMetadata(
            String mapId, Key key, Compression compression
    ) throws IOException {
        if (!cacheMetadataEnabled()) return null;
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findItemStorageKey(key);
        Integer compressionKey = findCompressionKey(compression);
        if (mapKey == null || storageKey == null || compressionKey == null) return null;
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    itemStorageReadMetadataStatement(),
                    mapKey, storageKey, compressionKey
            );
            if (!result.next()) return null;
            return new StoredMetadata(
                    result.getLong(1), result.getBytes(2), result.getLong(3)
            );
        });
    }

    @Language("sql")
    public abstract String itemStorageDeleteStatement();

    @Override
    public void deleteItem(String mapId, Key key) throws IOException {
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findItemStorageKey(key);
        if (mapKey == null || storageKey == null) return;
        db.run(connection -> executeUpdate(connection,
                itemStorageDeleteStatement(),
                mapKey, storageKey
        ));
    }

    @Language("sql")
    public abstract String itemStorageHasStatement();

    @Override
    public boolean hasItem(String mapId, Key key, Compression compression) throws IOException {
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findItemStorageKey(key);
        Integer compressionKey = findCompressionKey(compression);
        if (mapKey == null || storageKey == null || compressionKey == null) return false;
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    itemStorageHasStatement(),
                    mapKey, storageKey, compressionKey
            );
            if (!result.next()) throw new IllegalStateException("Counting query returned empty result!");
            return result.getBoolean(1);
        });
    }

    @Language("sql")
    public abstract String gridStorageWriteStatement();

    @Language("sql")
    public String gridStorageWriteWithMetadataStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Override
    public void writeGridItem(
            String mapId, Key key, int x, int z, Compression compression,
            byte[] bytes
    ) throws IOException {
        int mapKey = mapKey(mapId);
        int storageKey = gridStorageKey(key);
        int compressionKey = compressionKey(compression);
        if (cacheMetadataEnabled()) {
            byte[] contentHash = sha256(bytes);
            long updatedAt = System.currentTimeMillis();
            db.run(connection -> executeUpdate(connection,
                    gridStorageWriteWithMetadataStatement(),
                    mapKey, storageKey, x, z, compressionKey,
                    bytes, contentHash, updatedAt
            ));
        } else {
            db.run(connection -> executeUpdate(connection,
                    gridStorageWriteStatement(),
                    mapKey, storageKey, x, z, compressionKey, bytes
            ));
        }
    }

    @Language("sql")
    public abstract String gridStorageReadStatement();

    @Language("sql")
    public String gridStorageReadWithMetadataStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Override
    public byte @Nullable [] readGridItem(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException {
        StoredData stored =
                readGridItemData(mapId, key, x, z, compression);
        return stored == null ? null : stored.data();
    }

    @Override
    public @Nullable StoredData readGridItemData(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException {
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findGridStorageKey(key);
        Integer compressionKey = findCompressionKey(compression);
        if (mapKey == null || storageKey == null || compressionKey == null) return null;
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    cacheMetadataEnabled()
                            ? gridStorageReadWithMetadataStatement()
                            : gridStorageReadStatement(),
                    mapKey, storageKey, x, z, compressionKey
            );
            if (!result.next()) return null;
            if (!cacheMetadataEnabled()) {
                return new StoredData(result.getBytes(1), null, 0);
            }
            return new StoredData(
                    result.getBytes(1), result.getBytes(2), result.getLong(3)
            );
        });
    }

    @Language("sql")
    public String gridStorageReadMetadataStatement() {
        throw new UnsupportedOperationException("Cache metadata is not supported");
    }

    @Override
    public @Nullable StoredMetadata readGridItemMetadata(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException {
        if (!cacheMetadataEnabled()) return null;
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findGridStorageKey(key);
        Integer compressionKey = findCompressionKey(compression);
        if (mapKey == null || storageKey == null || compressionKey == null) return null;
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    gridStorageReadMetadataStatement(),
                    mapKey, storageKey, x, z, compressionKey
            );
            if (!result.next()) return null;
            return new StoredMetadata(
                    result.getLong(1), result.getBytes(2), result.getLong(3)
            );
        });
    }

    @Language("sql")
    public abstract String gridStorageDeleteStatement();

    @Override
    public void deleteGridItem(
            String mapId, Key key, int x, int z
    ) throws IOException {
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findGridStorageKey(key);
        if (mapKey == null || storageKey == null) return;
        db.run(connection -> executeUpdate(connection,
                gridStorageDeleteStatement(),
                mapKey, storageKey, x, z
        ));
    }

    @Language("sql")
    public abstract String gridStorageHasStatement();

    @Override
    public boolean hasGridItem(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException {
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findGridStorageKey(key);
        Integer compressionKey = findCompressionKey(compression);
        if (mapKey == null || storageKey == null || compressionKey == null) return false;
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    gridStorageHasStatement(),
                    mapKey, storageKey, x, z, compressionKey
            );
            if (!result.next()) throw new IllegalStateException("Counting query returned empty result!");
            return result.getBoolean(1);
        });
    }

    @Language("sql")
    public abstract String gridStorageListStatement();

    @Override
    public TilePosition[] listGridItems(
            String mapId, Key key, Compression compression,
            int start, int count
    ) throws IOException {
        Integer mapKey = findMapKey(mapId);
        Integer storageKey = findGridStorageKey(key);
        Integer compressionKey = findCompressionKey(compression);
        if (mapKey == null || storageKey == null || compressionKey == null) {
            return new TilePosition[0];
        }
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    gridStorageListStatement(),
                    mapKey, storageKey, compressionKey,
                    count, start
            );

            TilePosition[] tiles = new TilePosition[count];
            int i = 0;
            while (result.next()) {
                tiles[i++] = new TilePosition(
                        result.getInt(1),
                        result.getInt(2)
                );
            }

            if (i < count) {
                TilePosition[] trimmed = new TilePosition[i];
                System.arraycopy(tiles, 0, trimmed, 0, i);
                tiles = trimmed;
            }

            return tiles;
        });
    }

    @Language("sql")
    public abstract String gridStorageCountMapItemsStatement();

    @Override
    public int countMapGridsItems(String mapId) throws IOException {
        Integer mapKey = findMapKey(mapId);
        if (mapKey == null) return 0;
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    gridStorageCountMapItemsStatement(),
                    mapKey
            );
            if (!result.next()) throw new IllegalStateException("Counting query returned empty result!");
            return result.getInt(1);
        });
    }

    @Language("sql")
    public abstract String gridStoragePurgeMapStatement();

    @Override
    public int purgeMapGrids(String mapId, int limit) throws IOException {
        Integer mapKey = findMapKey(mapId);
        if (mapKey == null) return 0;
        return db.run(connection -> {
            return executeUpdate(connection,
                    gridStoragePurgeMapStatement(),
                    mapKey, limit
            );
        });
    }

    @Language("sql")
    public abstract String purgeMapStatement();

    @Override
    public void purgeMap(String mapId) throws IOException {
        synchronized (mapKeys) {
            Integer mapKey = findMapKey(mapId);
            if (mapKey == null) return;
            db.run(connection -> executeUpdate(connection,
                    purgeMapStatement(),
                    mapKey
            ));
            mapKeys.invalidate(mapId);
        }
    }

    @Language("sql")
    public abstract String hasMapStatement();

    @Override
    public boolean hasMap(String mapId) throws IOException {
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    hasMapStatement(),
                    mapId
            );
            if (!result.next()) throw new IllegalStateException("Counting query returned empty result!");
            return result.getBoolean(1);
        });
    }

    @Language("sql")
    public abstract String listMapIdsStatement();

    @Override
    public String[] listMapIds(int start, int count) throws IOException {
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    listMapIdsStatement(),
                    count, start
            );
            List<String> mapIds = new ArrayList<>();
            while (result.next()) {
                mapIds.add(result.getString(1));
            }
            return mapIds.toArray(String[]::new);
        });
    }

    @Language("sql")
    public abstract String findMapKeyStatement();

    @Language("sql")
    public abstract String createMapKeyStatement();

    public int mapKey(String mapId) {
        return mapKeys.get(mapId);
    }

    protected final @Nullable Integer findMapKey(String mapId) throws IOException {
        try {
            // Cache.get(key, mappingFunction) computes atomically per key. An
            // expired lookup therefore coalesces requests for one map without
            // making unrelated maps wait for its JDBC round trip.
            return mapKeys.get(mapId, key -> {
                try {
                    return loadMapKey(key);
                } catch (IOException ex) {
                    throw new UncheckedIOException(ex);
                }
            });
        } catch (UncheckedIOException ex) {
            throw ex.getCause();
        }
    }

    protected @Nullable Integer loadMapKey(String mapId) throws IOException {
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    findMapKeyStatement(),
                    mapId
            );
            return result.next() ? result.getInt(1) : null;
        });
    }

    public int findOrCreateMapKey(String mapId) throws IOException {
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    findMapKeyStatement(),
                    mapId
            );

            if (result.next())
                return result.getInt(1);

            PreparedStatement statement = connection.prepareStatement(
                    createMapKeyStatement(),
                    Statement.RETURN_GENERATED_KEYS
            );
            statement.setString(1, mapId);
            statement.executeUpdate();

            ResultSet keys = statement.getGeneratedKeys();
            if (!keys.next()) throw new IllegalStateException("No generated key returned!");
            return keys.getInt(1);
        });
    }

    @Language("sql")
    public abstract String findCompressionKeyStatement();

    @Language("sql")
    public abstract String createCompressionKeyStatement();

    public int compressionKey(Compression compression) {
        synchronized (compressionKeys) {
            return compressionKeys.get(compression);
        }
    }

    private @Nullable Integer findCompressionKey(Compression compression)
            throws IOException {
        synchronized (compressionKeys) {
            Integer cached = compressionKeys.getIfPresent(compression);
            if (cached != null) return cached;

            Integer found = db.run(connection -> {
                ResultSet result = executeQuery(connection,
                        findCompressionKeyStatement(),
                        compression.getKey().getFormatted()
                );
                return result.next() ? result.getInt(1) : null;
            });
            if (found != null) compressionKeys.put(compression, found);
            return found;
        }
    }

    public int findOrCreateCompressionKey(Compression compression) throws IOException {
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    findCompressionKeyStatement(),
                    compression.getKey().getFormatted()
            );

            if (result.next())
                return result.getInt(1);

            PreparedStatement statement = connection.prepareStatement(
                    createCompressionKeyStatement(),
                    Statement.RETURN_GENERATED_KEYS
            );
            statement.setString(1, compression.getKey().getFormatted());
            statement.executeUpdate();

            ResultSet keys = statement.getGeneratedKeys();
            if (!keys.next()) throw new IllegalStateException("No generated key returned!");
            return keys.getInt(1);
        });
    }

    @Language("sql")
    public abstract String findItemStorageKeyStatement();

    @Language("sql")
    public abstract String createItemStorageKeyStatement();

    public int itemStorageKey(Key key) {
        synchronized (itemStorageKeys) {
            return itemStorageKeys.get(key);
        }
    }

    private @Nullable Integer findItemStorageKey(Key key) throws IOException {
        synchronized (itemStorageKeys) {
            Integer cached = itemStorageKeys.getIfPresent(key);
            if (cached != null) return cached;

            Integer found = db.run(connection -> {
                ResultSet result = executeQuery(connection,
                        findItemStorageKeyStatement(),
                        key.getFormatted()
                );
                return result.next() ? result.getInt(1) : null;
            });
            if (found != null) itemStorageKeys.put(key, found);
            return found;
        }
    }

    public int findOrCreateItemStorageKey(Key key) throws IOException {
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    findItemStorageKeyStatement(),
                    key.getFormatted()
            );

            if (result.next())
                return result.getInt(1);

            PreparedStatement statement = connection.prepareStatement(
                    createItemStorageKeyStatement(),
                    Statement.RETURN_GENERATED_KEYS
            );
            statement.setString(1, key.getFormatted());
            statement.executeUpdate();

            ResultSet keys = statement.getGeneratedKeys();
            if (!keys.next()) throw new IllegalStateException("No generated key returned!");
            return keys.getInt(1);
        });
    }

    @Language("sql")
    public abstract String findGridStorageKeyStatement();

    @Language("sql")
    public abstract String createGridStorageKeyStatement();

    public int gridStorageKey(Key key) {
        synchronized (gridStorageKeys) {
            return gridStorageKeys.get(key);
        }
    }

    private @Nullable Integer findGridStorageKey(Key key) throws IOException {
        synchronized (gridStorageKeys) {
            Integer cached = gridStorageKeys.getIfPresent(key);
            if (cached != null) return cached;

            Integer found = db.run(connection -> {
                ResultSet result = executeQuery(connection,
                        findGridStorageKeyStatement(),
                        key.getFormatted()
                );
                return result.next() ? result.getInt(1) : null;
            });
            if (found != null) gridStorageKeys.put(key, found);
            return found;
        }
    }

    public int findOrCreateGridStorageKey(Key key) throws IOException {
        return db.run(connection -> {
            ResultSet result = executeQuery(connection,
                    findGridStorageKeyStatement(),
                    key.getFormatted()
            );

            if (result.next())
                return result.getInt(1);

            PreparedStatement statement = connection.prepareStatement(
                    createGridStorageKeyStatement(),
                    Statement.RETURN_GENERATED_KEYS
            );
            statement.setString(1, key.getFormatted());
            statement.executeUpdate();

            ResultSet keys = statement.getGeneratedKeys();
            if (!keys.next()) throw new IllegalStateException("No generated key returned!");
            return keys.getInt(1);
        });
    }

    @Override
    public boolean isClosed() {
        return db.isClosed();
    }

    @Override
    public boolean isHealthy() {
        return db.isHealthy();
    }

    @Override
    public void close() throws IOException {
        db.close();
    }

    protected static ResultSet executeQuery(Connection connection, @Language("sql") String sql, Object... parameters) throws SQLException {
        return prepareStatement(connection, sql, parameters).executeQuery();
    }

    @SuppressWarnings("UnusedReturnValue")
    protected static int executeUpdate(Connection connection, @Language("sql") String sql, Object... parameters) throws SQLException {
        return prepareStatement(connection, sql, parameters).executeUpdate();
    }

    private static PreparedStatement prepareStatement(Connection connection, @Language("sql") String sql, Object... parameters) throws SQLException {
        // we only use this prepared statement once, but the DB-Driver caches those and reuses them
        PreparedStatement statement = connection.prepareStatement(sql);
        for (int i = 0; i < parameters.length; i++) {
            statement.setObject(i + 1, parameters[i]);
        }
        return statement;
    }

    private void addColumnIfMissing(
            String table, String column, @Language("sql") String statement
    ) throws IOException {
        db.run(connection -> {
            if (columnExists(connection, table, column)) return;

            try {
                executeUpdate(connection, statement);
            } catch (SQLException migrationFailure) {
                // PostgreSQL leaves the transaction aborted after a concurrent
                // duplicate-column error. This migration gets its own
                // transaction, so resetting it here cannot discard other work.
                try {
                    if (!connection.getAutoCommit()) connection.rollback();
                } catch (SQLException rollbackFailure) {
                    migrationFailure.addSuppressed(rollbackFailure);
                }

                try {
                    if (columnExists(connection, table, column)) return;
                } catch (SQLException recheckFailure) {
                    migrationFailure.addSuppressed(recheckFailure);
                }
                throw migrationFailure;
            }
        });
    }

    static boolean columnExists(Connection connection, String table, String column)
            throws SQLException {
        DatabaseMetaData metadata = connection.getMetaData();
        String catalog = connection.getCatalog();
        String schema = connection.getSchema();
        String tablePattern = exactMetadataPattern(metadata, table);
        String columnPattern = exactMetadataPattern(metadata, column);

        try (ResultSet columns = metadata.getColumns(
                catalog, schema, tablePattern, columnPattern
        )) {
            while (columns.next()) {
                if (!table.equalsIgnoreCase(columns.getString("TABLE_NAME"))) continue;
                if (!column.equalsIgnoreCase(columns.getString("COLUMN_NAME"))) continue;

                String resultCatalog = columns.getString("TABLE_CAT");
                if (catalog != null && resultCatalog != null
                        && !catalog.equalsIgnoreCase(resultCatalog)) continue;

                String resultSchema = columns.getString("TABLE_SCHEM");
                if (schema != null && resultSchema != null
                        && !schema.equalsIgnoreCase(resultSchema)) continue;

                return true;
            }
        }
        return false;
    }

    private static String exactMetadataPattern(DatabaseMetaData metadata, String value)
            throws SQLException {
        String escape = metadata.getSearchStringEscape();
        if (escape == null || escape.isEmpty()) return value;
        return value
                .replace(escape, escape + escape)
                .replace("_", escape + "_")
                .replace("%", escape + "%");
    }

    private static byte[] sha256(byte[] bytes) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(bytes);
        } catch (NoSuchAlgorithmException ex) {
            throw new AssertionError("SHA-256 is required by the Java platform", ex);
        }
    }

}
