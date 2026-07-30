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
import org.jetbrains.annotations.Nullable;

import java.io.Closeable;
import java.io.IOException;

public interface CommandSet extends Closeable {

    /**
     * Tries to reserve capacity for one SQL-backed response read. The permit
     * remains held until the returned response stream is closed.
     */
    default @Nullable ReadPermit tryAcquireReadPermit() {
        return () -> {};
    }

    void initializeTables() throws IOException;

    void writeItem(String mapId, Key key, Compression compression, byte[] bytes) throws IOException;

    byte @Nullable [] readItem(String mapId, Key key, Compression compression) throws IOException;

    /**
     * Reads an item together with cache metadata when available.
     *
     * <p>The default preserves compatibility with command sets implementing
     * the original byte-array read contract.</p>
     */
    default @Nullable StoredData readItemData(
            String mapId, Key key, Compression compression
    ) throws IOException {
        byte[] data = readItem(mapId, key, compression);
        return data == null ? null : new StoredData(data, null, 0);
    }

    default @Nullable StoredMetadata readItemMetadata(
            String mapId, Key key, Compression compression
    ) throws IOException {
        return null;
    }

    void deleteItem(String mapId, Key key) throws IOException;

    boolean hasItem(String mapId, Key key, Compression compression) throws IOException;

    void writeGridItem(
            String mapId, Key key, int x, int z, Compression compression,
            byte[] bytes
    ) throws IOException;

    byte @Nullable [] readGridItem(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException;

    /**
     * Reads a grid item together with cache metadata when available.
     *
     * <p>The default preserves compatibility with command sets implementing
     * the original byte-array read contract.</p>
     */
    default @Nullable StoredData readGridItemData(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException {
        byte[] data = readGridItem(mapId, key, x, z, compression);
        return data == null ? null : new StoredData(data, null, 0);
    }

    default @Nullable StoredMetadata readGridItemMetadata(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException {
        return null;
    }

    void deleteGridItem(
            String mapId, Key key, int x, int z
    ) throws IOException;

    boolean hasGridItem(
            String mapId, Key key, int x, int z, Compression compression
    ) throws IOException;

    TilePosition[] listGridItems(
            String mapId, Key key, Compression compression,
            int start, int count
    ) throws IOException;

    int countMapGridsItems(String mapId) throws IOException;

    int purgeMapGrids(String mapId, int limit) throws IOException;

    void purgeMap(String mapId) throws IOException;

    boolean hasMap(String mapId) throws IOException;

    String[] listMapIds(int start, int count) throws IOException;

    boolean isClosed();

    default boolean isHealthy() {
        return !isClosed();
    }

    record TilePosition (int x, int z) {}

    record StoredData(byte[] data, byte @Nullable [] contentHash, long updatedAt) {

        public StoredData {
            contentHash = contentHash == null ? null : contentHash.clone();
        }

        @Override
        public byte @Nullable [] contentHash() {
            return contentHash == null ? null : contentHash.clone();
        }

    }

    record StoredMetadata(
            long contentLength,
            byte @Nullable [] contentHash,
            long updatedAt
    ) {

        public StoredMetadata {
            if (contentLength < 0) {
                throw new IllegalArgumentException("contentLength must not be negative");
            }
            contentHash = contentHash == null ? null : contentHash.clone();
        }

        @Override
        public byte @Nullable [] contentHash() {
            return contentHash == null ? null : contentHash.clone();
        }

    }

    @FunctionalInterface
    interface ReadPermit extends AutoCloseable {

        @Override
        void close();

    }

}
