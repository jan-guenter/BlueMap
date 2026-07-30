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
package de.bluecolored.bluemap.core.storage.sql;

import de.bluecolored.bluemap.core.storage.GridStorage;
import de.bluecolored.bluemap.core.storage.ItemStorage;
import de.bluecolored.bluemap.core.storage.MapStorage;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.storage.sql.commandset.CommandSet;
import de.bluecolored.bluemap.core.storage.sql.commandset.SqliteCommandSet;
import de.bluecolored.bluemap.core.util.Key;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.sqlite.SQLiteDataSource;

import javax.sql.DataSource;
import java.io.OutputStream;
import java.io.PrintWriter;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Map;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.logging.Logger;

import static org.junit.jupiter.api.Assertions.*;

class SqliteCacheMetadataIntegrationTest {

    @Test
    void migratesLegacySchemaAndPersistsValidators(@TempDir Path tempDir) throws Exception {
        String url = "jdbc:sqlite:" + tempDir.resolve("bluemap.db");
        createLegacySchema(url);

        try (Database database = new Database(url, Map.of(), 1);
             CommandSet commands = new SqliteCommandSet(database)) {
            commands.initializeTables();
            commands.initializeTables();

            byte[] item = "settings".getBytes();
            commands.writeItem("map", Key.bluemap("settings"), Compression.NONE, item);
            CommandSet.StoredData storedItem =
                    commands.readItem("map", Key.bluemap("settings"), Compression.NONE);
            assertNotNull(storedItem);
            assertArrayEquals(item, storedItem.data());
            assertArrayEquals(MessageDigest.getInstance("SHA-256").digest(item), storedItem.contentHash());
            assertTrue(storedItem.updatedAt() > 0);
            CommandSet.StoredMetadata itemMetadata =
                    commands.readItemMetadata(
                            "map", Key.bluemap("settings"), Compression.NONE
                    );
            assertNotNull(itemMetadata);
            assertEquals(item.length, itemMetadata.contentLength());
            assertArrayEquals(storedItem.contentHash(), itemMetadata.contentHash());
            assertEquals(storedItem.updatedAt(), itemMetadata.updatedAt());

            byte[] tile = "tile".getBytes();
            commands.writeGridItem("map", Key.bluemap("hires"), 1, -2, Compression.NONE, tile);
            CommandSet.StoredData storedTile =
                    commands.readGridItem("map", Key.bluemap("hires"), 1, -2, Compression.NONE);
            assertNotNull(storedTile);
            assertArrayEquals(tile, storedTile.data());
            assertArrayEquals(MessageDigest.getInstance("SHA-256").digest(tile), storedTile.contentHash());
            assertTrue(storedTile.updatedAt() > 0);
            CommandSet.StoredMetadata tileMetadata =
                    commands.readGridItemMetadata(
                            "map", Key.bluemap("hires"), 1, -2, Compression.NONE
                    );
            assertNotNull(tileMetadata);
            assertEquals(tile.length, tileMetadata.contentLength());
            assertArrayEquals(storedTile.contentHash(), tileMetadata.contentHash());
            assertEquals(storedTile.updatedAt(), tileMetadata.updatedAt());
        }
    }

    @Test
    void missingReadsNeverCreateAttackerControlledStorageKeys(
            @TempDir Path tempDir
    ) throws Exception {
        String url = "jdbc:sqlite:" + tempDir.resolve("lookup-only.db");

        try (Database database = new Database(url, Map.of(), 2);
             CommandSet commands = new SqliteCommandSet(database)) {
            commands.initializeTables();
            SQLMapStorage storage =
                    new SQLMapStorage("map", commands, Compression.NONE);

            try (OutputStream output = storage.settings().write()) {
                output.write("settings".getBytes());
            }
            try (OutputStream output = storage.hiresTiles().write(0, 0)) {
                output.write("tile".getBytes());
            }

            int itemKeys = countRows(url, "bluemap_item_storage");
            int gridKeys = countRows(url, "bluemap_grid_storage");

            ItemStorage missingAsset =
                    storage.asset("missing-" + "x".repeat(1024) + ".bin");
            assertNull(missingAsset.read());
            assertNull(missingAsset.readMetadata());
            assertFalse(missingAsset.exists());
            missingAsset.delete();

            GridStorage arbitraryLod = storage.lowresTiles(987_654_321);
            assertNull(arbitraryLod.read(1, 2));
            assertNull(arbitraryLod.readMetadata(1, 2));
            assertFalse(arbitraryLod.exists(1, 2));
            arbitraryLod.delete(1, 2);

            assertEquals(itemKeys, countRows(url, "bluemap_item_storage"));
            assertEquals(gridKeys, countRows(url, "bluemap_grid_storage"));
        }
    }

    @Test
    void boundsInFlightSqlBodiesToTheConnectionPoolSize(
            @TempDir Path tempDir
    ) throws Exception {
        String url = "jdbc:sqlite:" + tempDir.resolve("read-gate.db");

        try (Database database = new Database(url, Map.of(), 1);
             CommandSet commands = new SqliteCommandSet(database)) {
            commands.initializeTables();
            SQLMapStorage storage =
                    new SQLMapStorage("map", commands, Compression.NONE);
            try (OutputStream output = storage.settings().write()) {
                output.write("settings".getBytes());
            }

            MapStorage.ReadPermit first = storage.tryAcquireReadPermit();
            assertNotNull(first);
            assertNull(storage.tryAcquireReadPermit());

            first.close();
            first.close();

            try (MapStorage.ReadPermit second =
                         storage.tryAcquireReadPermit()) {
                assertNotNull(second);
                assertNull(storage.tryAcquireReadPermit());
            }
            try (MapStorage.ReadPermit recovered =
                         storage.tryAcquireReadPermit()) {
                assertNotNull(recovered);
            }
        }
    }

    @Test
    void preservesNegativeUnlimitedPoolSemantics(@TempDir Path tempDir)
            throws Exception {
        String url = "jdbc:sqlite:" + tempDir.resolve("unlimited.db");

        try (Database database = new Database(url, Map.of(), -1)) {
            assertEquals(-1, database.getMaxPoolSize());
            assertEquals(-1, database.getMaxConcurrentReads());
            try (Database.ReadPermit first = database.tryAcquireReadPermit();
                 Database.ReadPermit second = database.tryAcquireReadPermit()) {
                assertNotNull(first);
                assertNotNull(second);
            }
        }
    }

    @Test
    void cachesDatabaseDependencyHealthAndRecovers() throws Exception {
        ToggleDataSource dataSource = new ToggleDataSource();

        try (Database database = new Database(dataSource, -1)) {
            database.refreshHealth();
            assertTrue(database.isHealthy());

            dataSource.available.set(false);
            database.refreshHealth();
            assertFalse(database.isHealthy());

            dataSource.available.set(true);
            database.refreshHealth();
            assertTrue(database.isHealthy());
        }
    }

    @Test
    void cachedHealthExpiresWhileAJdbcProbeIsBlocked() throws Exception {
        CountDownLatch probeStarted = new CountDownLatch(1);
        CountDownLatch releaseProbe = new CountDownLatch(1);
        AtomicLong nanoTime = new AtomicLong();
        DataSource dataSource = new BlockingDataSource(
                probeStarted,
                releaseProbe
        );

        try (Database database = new Database(
                dataSource,
                -1,
                nanoTime::get,
                Duration.ofSeconds(5)
        )) {
            CompletableFuture<Void> probe =
                    CompletableFuture.runAsync(database::refreshHealth);
            assertTrue(probeStarted.await(2, TimeUnit.SECONDS));
            assertTrue(database.isHealthy());

            nanoTime.set(Duration.ofSeconds(5).toNanos() + 1);
            assertFalse(database.isHealthy());

            releaseProbe.countDown();
            probe.get(2, TimeUnit.SECONDS);
            assertTrue(database.isHealthy());
        } finally {
            releaseProbe.countDown();
        }
    }

    private static int countRows(String url, String table) throws Exception {
        try (var connection = DriverManager.getConnection(url);
             var statement = connection.createStatement();
             var result = statement.executeQuery("SELECT COUNT(*) FROM " + table)) {
            assertTrue(result.next());
            return result.getInt(1);
        }
    }

    private static void createLegacySchema(String url) throws Exception {
        try (var connection = DriverManager.getConnection(url);
             var statement = connection.createStatement()) {
            // JDBC metadata names are patterns. These deliberately match the
            // unescaped underscores in the real table name and must not make
            // the migration mistake a different table for the target.
            statement.execute("""
                    CREATE TABLE bluemapXitemXstorageXdata (
                     content_hash BLOB NULL, updated_at INTEGER NULL
                    )
                    """);
            statement.execute("""
                    CREATE TABLE bluemapXgridXstorageXdata (
                     content_hash BLOB NULL, updated_at INTEGER NULL
                    )
                    """);
            statement.execute("CREATE TABLE bluemap_map (id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT UNIQUE NOT NULL)");
            statement.execute("CREATE TABLE bluemap_compression (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL)");
            statement.execute("CREATE TABLE bluemap_item_storage (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL)");
            statement.execute("CREATE TABLE bluemap_grid_storage (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL)");
            statement.execute("""
                    CREATE TABLE bluemap_item_storage_data (
                     map INTEGER NOT NULL, storage INTEGER NOT NULL, compression INTEGER NOT NULL,
                     data BLOB NOT NULL, PRIMARY KEY (map, storage)
                    )
                    """);
            statement.execute("""
                    CREATE TABLE bluemap_grid_storage_data (
                     map INTEGER NOT NULL, storage INTEGER NOT NULL, x INTEGER NOT NULL, z INTEGER NOT NULL,
                     compression INTEGER NOT NULL, data BLOB NOT NULL,
                     PRIMARY KEY (map, storage, x, z)
                    )
                    """);
        }
    }

    private static final class ToggleDataSource implements DataSource {

        private final SQLiteDataSource delegate = new SQLiteDataSource();
        private final AtomicBoolean available = new AtomicBoolean(true);

        private ToggleDataSource() {
            delegate.setUrl("jdbc:sqlite::memory:");
        }

        @Override
        public Connection getConnection() throws SQLException {
            if (!available.get()) {
                throw new SQLException("database unavailable");
            }
            Connection connection = delegate.getConnection();
            connection.setAutoCommit(false);
            return connection;
        }

        @Override
        public Connection getConnection(String username, String password)
                throws SQLException {
            return getConnection();
        }

        @Override
        public PrintWriter getLogWriter() throws SQLException {
            return delegate.getLogWriter();
        }

        @Override
        public void setLogWriter(PrintWriter out) throws SQLException {
            delegate.setLogWriter(out);
        }

        @Override
        public void setLoginTimeout(int seconds) throws SQLException {
            delegate.setLoginTimeout(seconds);
        }

        @Override
        public int getLoginTimeout() throws SQLException {
            return delegate.getLoginTimeout();
        }

        @Override
        public Logger getParentLogger() {
            return Logger.getGlobal();
        }

        @Override
        public <T> T unwrap(Class<T> iface) throws SQLException {
            if (iface.isInstance(this)) return iface.cast(this);
            throw new SQLException("Not a wrapper for " + iface);
        }

        @Override
        public boolean isWrapperFor(Class<?> iface) {
            return iface.isInstance(this);
        }

    }

    private static final class BlockingDataSource implements DataSource {

        private final SQLiteDataSource delegate = new SQLiteDataSource();
        private final CountDownLatch started;
        private final CountDownLatch release;

        private BlockingDataSource(
                CountDownLatch started,
                CountDownLatch release
        ) {
            this.started = started;
            this.release = release;
            delegate.setUrl("jdbc:sqlite::memory:");
        }

        @Override
        public Connection getConnection() throws SQLException {
            started.countDown();
            try {
                if (!release.await(2, TimeUnit.SECONDS)) {
                    throw new SQLException("timed out waiting to release probe");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new SQLException("probe interrupted", e);
            }
            Connection connection = delegate.getConnection();
            connection.setAutoCommit(false);
            return connection;
        }

        @Override
        public Connection getConnection(String username, String password)
                throws SQLException {
            return getConnection();
        }

        @Override
        public PrintWriter getLogWriter() throws SQLException {
            return delegate.getLogWriter();
        }

        @Override
        public void setLogWriter(PrintWriter out) throws SQLException {
            delegate.setLogWriter(out);
        }

        @Override
        public void setLoginTimeout(int seconds) throws SQLException {
            delegate.setLoginTimeout(seconds);
        }

        @Override
        public int getLoginTimeout() throws SQLException {
            return delegate.getLoginTimeout();
        }

        @Override
        public Logger getParentLogger() {
            return Logger.getGlobal();
        }

        @Override
        public <T> T unwrap(Class<T> iface) throws SQLException {
            if (iface.isInstance(this)) return iface.cast(this);
            throw new SQLException("Not a wrapper for " + iface);
        }

        @Override
        public boolean isWrapperFor(Class<?> iface) {
            return iface.isInstance(this);
        }

    }

}
