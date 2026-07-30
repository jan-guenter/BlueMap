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
package de.bluecolored.bluemap.core.storage.file;

import de.bluecolored.bluemap.core.storage.StoredDataMetadata;
import de.bluecolored.bluemap.core.storage.compression.CompressedInputStream;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.OutputStream;
import java.nio.channels.Channels;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.FileTime;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.*;

class FileItemStorageTest {

    @Test
    void exposesLengthAndLastModifiedWithoutHashingOrSidecars(@TempDir Path tempDir)
            throws Exception {
        byte[] content = "stored-file-data".getBytes();
        FileItemStorage storage = new FileItemStorage(
                tempDir.resolve("item.bin"),
                Compression.NONE,
                false
        );
        try (OutputStream output = storage.write()) {
            output.write(content);
        }

        StoredDataMetadata metadata = storage.readMetadata();
        assertNotNull(metadata);
        assertEquals(Compression.NONE, metadata.compression());
        assertEquals(content.length, metadata.contentLength());
        assertNull(metadata.cacheMetadata().contentHash());
        assertTrue(metadata.cacheMetadata().updatedAt() > 0);

        try (CompressedInputStream input = storage.read()) {
            assertNotNull(input);
            assertNotNull(input.getCacheMetadata());
            assertEquals(
                    metadata.cacheMetadata().updatedAt(),
                    input.getCacheMetadata().updatedAt()
            );
            assertArrayEquals(content, input.readAllBytes());
        }
    }

    @Test
    void retriesWhenAFileIsAtomicallyReplacedBetweenStatAndOpen(
            @TempDir Path tempDir
    ) throws Exception {
        Path file = tempDir.resolve("item.bin");
        Path replacement = tempDir.resolve("replacement.bin");
        byte[] oldContent = "old".getBytes();
        byte[] newContent = "replacement-data".getBytes();
        Files.write(file, oldContent);
        Files.write(replacement, newContent);
        Files.setLastModifiedTime(file, FileTime.fromMillis(1_000_000));
        Files.setLastModifiedTime(replacement, FileTime.fromMillis(2_000_000));

        AtomicBoolean replace = new AtomicBoolean(true);
        try (FileItemStorage.OpenedFile opened =
                     FileItemStorage.openStableFile(file, () -> {
                         if (!replace.compareAndSet(true, false)) return;
                         try {
                             Files.move(
                                     replacement,
                                     file,
                                     StandardCopyOption.ATOMIC_MOVE,
                                     StandardCopyOption.REPLACE_EXISTING
                             );
                         } catch (AtomicMoveNotSupportedException e) {
                             Files.move(
                                     replacement,
                                     file,
                                     StandardCopyOption.REPLACE_EXISTING
                             );
                         }
                     })) {
            assertEquals(newContent.length, opened.attributes().size());
            assertEquals(
                    Files.getLastModifiedTime(file),
                    opened.attributes().lastModifiedTime()
            );
            assertArrayEquals(
                    newContent,
                    Channels.newInputStream(opened.channel()).readAllBytes()
            );
        }
    }

}
