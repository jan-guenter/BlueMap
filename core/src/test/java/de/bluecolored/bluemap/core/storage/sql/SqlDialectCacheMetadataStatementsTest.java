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

import de.bluecolored.bluemap.core.storage.sql.commandset.AbstractCommandSet;
import de.bluecolored.bluemap.core.storage.sql.commandset.MySQLCommandSet;
import de.bluecolored.bluemap.core.storage.sql.commandset.PostgreSQLCommandSet;
import de.bluecolored.bluemap.core.storage.sql.commandset.SqliteCommandSet;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SqlDialectCacheMetadataStatementsTest {

    @Test
    void allDialectsWriteAndReadCacheMetadata() {
        assertMetadataStatements(new MySQLCommandSet(null));
        assertMetadataStatements(new PostgreSQLCommandSet(null));
        assertMetadataStatements(new SqliteCommandSet(null));
    }

    private static void assertMetadataStatements(AbstractCommandSet commands) {
        assertTrue(commands.supportsAtomicLengthRead());
        assertTrue(commands.createItemStorageDataTableStatement().contains("content_hash"));
        assertTrue(commands.createItemStorageDataTableStatement().contains("updated_at"));
        assertTrue(commands.createGridStorageDataTableStatement().contains("content_hash"));
        assertTrue(commands.createGridStorageDataTableStatement().contains("updated_at"));

        assertEquals(4, placeholders(commands.itemStorageWriteStatement()));
        assertEquals(6, placeholders(commands.gridStorageWriteStatement()));
        assertFalse(commands.itemStorageReadStatement().contains("content_hash"));
        assertFalse(commands.gridStorageReadStatement().contains("content_hash"));
        assertEquals(
                6,
                placeholders(commands.itemStorageWriteWithMetadataStatement())
        );
        assertEquals(
                8,
                placeholders(commands.gridStorageWriteWithMetadataStatement())
        );
        assertTrue(
                commands.itemStorageReadWithMetadataStatement()
                        .contains("content_hash")
        );
        assertTrue(
                commands.itemStorageReadWithMetadataStatement()
                        .contains("updated_at")
        );
        assertTrue(
                commands.gridStorageReadWithMetadataStatement()
                        .contains("content_hash")
        );
        assertTrue(
                commands.gridStorageReadWithMetadataStatement()
                        .contains("updated_at")
        );
        assertEquals(
                4,
                placeholders(
                        commands.itemStorageReadWithMetadataAndLengthStatement()
                )
        );
        assertEquals(
                6,
                placeholders(
                        commands.gridStorageReadWithMetadataAndLengthStatement()
                )
        );
        assertTrue(
                commands.itemStorageReadWithMetadataAndLengthStatement()
                        .toUpperCase()
                        .contains("LENGTH(")
        );
        assertTrue(
                commands.gridStorageReadWithMetadataAndLengthStatement()
                        .toUpperCase()
                        .contains("LENGTH(")
        );

        assertEquals(3, placeholders(commands.itemStorageReadMetadataStatement()));
        assertEquals(5, placeholders(commands.gridStorageReadMetadataStatement()));
        assertTrue(commands.itemStorageReadMetadataStatement().toUpperCase().contains("LENGTH("));
        assertTrue(commands.gridStorageReadMetadataStatement().toUpperCase().contains("LENGTH("));
        assertTrue(commands.itemStorageReadMetadataStatement().contains("content_hash"));
        assertTrue(commands.gridStorageReadMetadataStatement().contains("content_hash"));
    }

    private static long placeholders(String statement) {
        return statement.chars().filter(character -> character == '?').count();
    }

}
