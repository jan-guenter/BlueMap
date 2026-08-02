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

import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.util.Key;
import org.junit.jupiter.api.Test;

import java.io.IOException;

import static org.junit.jupiter.api.Assertions.*;

class CommandSetCompatibilityTest {

    @Test
    void metadataAwareReadsFallBackToLegacyByteArrayMethods()
            throws Exception {
        byte[] item = "item".getBytes();
        byte[] gridItem = "grid".getBytes();
        CommandSet commands = new LegacyCommandSet(item, gridItem);

        CommandSet.StoredData storedItem = commands.readItemData(
                "map",
                Key.bluemap("settings"),
                Compression.NONE
        );
        assertNotNull(storedItem);
        assertArrayEquals(item, storedItem.data());
        assertNull(storedItem.contentHash());
        assertEquals(0, storedItem.updatedAt());

        CommandSet.StoredData storedGridItem = commands.readGridItemData(
                "map",
                Key.bluemap("hires"),
                1,
                2,
                Compression.NONE
        );
        assertNotNull(storedGridItem);
        assertArrayEquals(gridItem, storedGridItem.data());
        assertNull(storedGridItem.contentHash());
        assertEquals(0, storedGridItem.updatedAt());
    }

    /**
     * Deliberately implements only the original read signatures. Compilation
     * of this fake is the source-compatibility regression check.
     */
    private static final class LegacyCommandSet implements CommandSet {

        private final byte[] item;
        private final byte[] gridItem;

        private LegacyCommandSet(byte[] item, byte[] gridItem) {
            this.item = item;
            this.gridItem = gridItem;
        }

        @Override
        public void initializeTables() {}

        @Override
        public void writeItem(
                String mapId,
                Key key,
                Compression compression,
                byte[] bytes
        ) {}

        @Override
        public byte[] readItem(
                String mapId,
                Key key,
                Compression compression
        ) {
            return item;
        }

        @Override
        public void deleteItem(String mapId, Key key) {}

        @Override
        public boolean hasItem(
                String mapId,
                Key key,
                Compression compression
        ) {
            return true;
        }

        @Override
        public void writeGridItem(
                String mapId,
                Key key,
                int x,
                int z,
                Compression compression,
                byte[] bytes
        ) {}

        @Override
        public byte[] readGridItem(
                String mapId,
                Key key,
                int x,
                int z,
                Compression compression
        ) {
            return gridItem;
        }

        @Override
        public void deleteGridItem(
                String mapId,
                Key key,
                int x,
                int z
        ) {}

        @Override
        public boolean hasGridItem(
                String mapId,
                Key key,
                int x,
                int z,
                Compression compression
        ) {
            return true;
        }

        @Override
        public TilePosition[] listGridItems(
                String mapId,
                Key key,
                Compression compression,
                int start,
                int count
        ) {
            return new TilePosition[0];
        }

        @Override
        public int countMapGridsItems(String mapId) {
            return 0;
        }

        @Override
        public int purgeMapGrids(String mapId, int limit) {
            return 0;
        }

        @Override
        public void purgeMap(String mapId) {}

        @Override
        public boolean hasMap(String mapId) {
            return false;
        }

        @Override
        public String[] listMapIds(int start, int count) {
            return new String[0];
        }

        @Override
        public boolean isClosed() {
            return false;
        }

        @Override
        public void close() throws IOException {}

    }

}
