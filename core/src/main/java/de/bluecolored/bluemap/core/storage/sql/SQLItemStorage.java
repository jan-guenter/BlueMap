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

import de.bluecolored.bluemap.core.storage.CacheMetadata;
import de.bluecolored.bluemap.core.storage.StoredDataMetadata;
import de.bluecolored.bluemap.core.storage.compression.CompressedInputStream;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.storage.ItemStorage;
import de.bluecolored.bluemap.core.storage.sql.commandset.CommandSet;
import de.bluecolored.bluemap.core.util.Key;
import de.bluecolored.bluemap.core.util.stream.OnCloseOutputStream;
import org.jetbrains.annotations.Nullable;

import java.io.*;

public class SQLItemStorage implements ItemStorage {

    private final CommandSet sql;
    private final String map;
    private final Key storage;
    private final Compression compression;
    private final boolean readOnly;

    public SQLItemStorage(
            CommandSet sql,
            String map,
            Key storage,
            Compression compression
    ) {
        this(sql, map, storage, compression, false);
    }

    public SQLItemStorage(
            CommandSet sql,
            String map,
            Key storage,
            Compression compression,
            boolean readOnly
    ) {
        this.sql = sql;
        this.map = map;
        this.storage = storage;
        this.compression = compression;
        this.readOnly = readOnly;
    }

    @Override
    public OutputStream write() throws IOException {
        requireWritable();
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        return new OnCloseOutputStream(compression.compress(bytes),
                () -> sql.writeItem(map, storage, compression, bytes.toByteArray())
        );
    }

    @Override
    public @Nullable CompressedInputStream read() throws IOException {
        return asStream(sql.readItemData(map, storage, compression));
    }

    @Override
    public @Nullable CompressedInputStream read(long expectedContentLength)
            throws IOException {
        return asStream(sql.readItemData(
                map, storage, compression, expectedContentLength
        ));
    }

    @Override
    public boolean supportsAtomicLengthRead() {
        return sql.supportsAtomicLengthRead();
    }

    private @Nullable CompressedInputStream asStream(
            @Nullable CommandSet.StoredData stored
    ) {
        if (stored == null) return null;
        byte[] data = stored.data();
        return new CompressedInputStream(
                new ByteArrayInputStream(data),
                compression,
                new CacheMetadata(stored.contentHash(), stored.updatedAt()),
                data.length
        );
    }

    @Override
    public @Nullable StoredDataMetadata readMetadata() throws IOException {
        CommandSet.StoredMetadata stored =
                sql.readItemMetadata(map, storage, compression);
        if (stored == null) return null;
        return new StoredDataMetadata(
                compression,
                new CacheMetadata(stored.contentHash(), stored.updatedAt()),
                stored.contentLength()
        );
    }

    @Override
    public Compression compression() {
        return compression;
    }

    @Override
    public void delete() throws IOException {
        requireWritable();
        sql.deleteItem(map, storage);
    }

    @Override
    public boolean exists() throws IOException {
        return sql.hasItem(map, storage, compression);
    }

    @Override
    public boolean isClosed() {
        return sql.isClosed();
    }

    private void requireWritable() throws IOException {
        if (readOnly) {
            throw new IOException("SQL storage is configured read-only");
        }
    }

}
