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

import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.storage.sql.commandset.CommandSet;
import de.bluecolored.bluemap.core.storage.sql.commandset.SqliteCommandSet;
import de.bluecolored.bluemap.core.util.Key;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.security.MessageDigest;
import java.sql.DriverManager;
import java.util.Map;

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

            byte[] tile = "tile".getBytes();
            commands.writeGridItem("map", Key.bluemap("hires"), 1, -2, Compression.NONE, tile);
            CommandSet.StoredData storedTile =
                    commands.readGridItem("map", Key.bluemap("hires"), 1, -2, Compression.NONE);
            assertNotNull(storedTile);
            assertArrayEquals(tile, storedTile.data());
            assertArrayEquals(MessageDigest.getInstance("SHA-256").digest(tile), storedTile.contentHash());
            assertTrue(storedTile.updatedAt() > 0);
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

}
