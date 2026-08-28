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
package de.bluecolored.bluemap.core.map.hires;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.flowpowered.math.vector.Vector2i;
import de.bluecolored.bluemap.core.storage.GridStorage;
import de.bluecolored.bluemap.core.storage.ItemStorage;
import de.bluecolored.bluemap.core.storage.StoredDataMetadata;
import de.bluecolored.bluemap.core.storage.compression.CompressedInputStream;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

class HiresModelManagerTest {
    @Test
    void failedTileWriteDeletesOutputAndPropagates() {
        FailingGridStorage storage = new FailingGridStorage();

        assertThrows(
                UncheckedIOException.class,
                () -> HiresModelManager.writeModel(
                        storage, new ArrayTileModel(0), Vector2i.ZERO
                )
        );
        assertTrue(storage.deleted);
    }

    private static final class FailingGridStorage implements GridStorage {
        boolean deleted;

        @Override
        public OutputStream write(int x, int z) {
            return new OutputStream() {
                @Override
                public void write(int value) throws IOException {
                    throw new IOException("expected write failure");
                }
            };
        }

        @Override
        public CompressedInputStream read(int x, int z) {
            return null;
        }

        @Override
        public StoredDataMetadata readMetadata(int x, int z) {
            return null;
        }

        @Override
        public Compression compression() {
            return null;
        }

        @Override
        public void delete(int x, int z) {
            deleted = true;
        }

        @Override
        public boolean exists(int x, int z) {
            return false;
        }

        @Override
        public ItemStorage cell(int x, int z) {
            throw new UnsupportedOperationException();
        }

        @Override
        public Stream<Cell> stream() {
            return Stream.empty();
        }

        @Override
        public boolean isClosed() {
            return false;
        }
    }
}
