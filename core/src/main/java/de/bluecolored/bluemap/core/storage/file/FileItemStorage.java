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

import de.bluecolored.bluemap.core.storage.CacheMetadata;
import de.bluecolored.bluemap.core.storage.ItemStorage;
import de.bluecolored.bluemap.core.storage.StoredDataMetadata;
import de.bluecolored.bluemap.core.storage.compression.CompressedInputStream;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.util.FileHelper;
import lombok.RequiredArgsConstructor;
import org.jetbrains.annotations.Nullable;

import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.channels.Channels;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.Objects;

@RequiredArgsConstructor
public class FileItemStorage implements ItemStorage {

    private final Path file;
    private final Compression compression;
    private final boolean atomic;

    @Override
    public OutputStream write() throws IOException {
        if (atomic)
            return compression.compress(FileHelper.createFilepartOutputStream(file));

        Path folder = file.toAbsolutePath().normalize().getParent();
        FileHelper.createDirectories(folder);
        return compression.compress(Files.newOutputStream(file,
                StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.CREATE));
    }

    @Override
    public @Nullable CompressedInputStream read() throws IOException {
        OpenedFile openedFile;
        try {
            openedFile = openStableFile(file);
        } catch (FileNotFoundException | NoSuchFileException ex) {
            return null;
        }

        BasicFileAttributes attributes = openedFile.attributes();
        if (!attributes.isRegularFile()) {
            openedFile.close();
            return null;
        }

        try {
            return new CompressedInputStream(
                    Channels.newInputStream(openedFile.channel()),
                    compression,
                    cacheMetadata(attributes)
            );
        } catch (RuntimeException e) {
            openedFile.close();
            throw e;
        }
    }

    @Override
    public @Nullable StoredDataMetadata readMetadata() throws IOException {
        try {
            BasicFileAttributes attributes =
                    Files.readAttributes(file, BasicFileAttributes.class);
            if (!attributes.isRegularFile()) return null;
            return new StoredDataMetadata(
                    compression,
                    cacheMetadata(attributes),
                    attributes.size()
            );
        } catch (FileNotFoundException | NoSuchFileException ex) {
            return null;
        }
    }

    @Override
    public Compression compression() {
        return compression;
    }

    static OpenedFile openStableFile(Path path) throws IOException {
        return openStableFile(path, () -> {});
    }

    static OpenedFile openStableFile(
            Path path,
            OpenHook afterInitialMetadata
    ) throws IOException {
        IOException lastFailure = null;

        for (int attempt = 0; attempt < 3; attempt++) {
            BasicFileAttributes before =
                    Files.readAttributes(path, BasicFileAttributes.class);
            afterInitialMetadata.run();
            FileChannel channel = null;

            try {
                channel = FileChannel.open(path, StandardOpenOption.READ);
                BasicFileAttributes after =
                        Files.readAttributes(path, BasicFileAttributes.class);

                if (sameFileVersion(before, after)
                        && channel.size() == after.size()) {
                    return new OpenedFile(channel, after);
                }

                lastFailure = new IOException(
                        "File changed while it was being opened: " + path
                );
            } catch (IOException e) {
                lastFailure = e;
            }

            if (channel != null) channel.close();
        }

        throw Objects.requireNonNullElseGet(
                lastFailure,
                () -> new IOException("Failed to open file: " + path)
        );
    }

    private static boolean sameFileVersion(
            BasicFileAttributes before,
            BasicFileAttributes after
    ) {
        Object beforeKey = before.fileKey();
        Object afterKey = after.fileKey();
        if ((beforeKey != null || afterKey != null)
                && !Objects.equals(beforeKey, afterKey)) {
            return false;
        }

        return before.isRegularFile() == after.isRegularFile()
                && before.size() == after.size()
                && before.lastModifiedTime().equals(after.lastModifiedTime());
    }

    /**
     * Creates a representation validator from file metadata only. It is weak
     * because an in-place writer can change content while preserving every
     * available metadata field.
     */
    private static CacheMetadata cacheMetadata(BasicFileAttributes attributes) {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is unavailable", e);
        }

        updateDigest(digest, "bluemap-file-validator-v1");
        Object fileKey = attributes.fileKey();
        if (fileKey == null) {
            digest.update((byte) 0);
        } else {
            digest.update((byte) 1);
            updateDigest(digest, fileKey.getClass().getName());
            updateDigest(digest, fileKey.toString());
        }

        Instant modified = attributes.lastModifiedTime().toInstant();
        updateDigest(digest, Long.toString(modified.getEpochSecond()));
        updateDigest(digest, Integer.toString(modified.getNano()));
        updateDigest(digest, Long.toString(attributes.size()));

        return new CacheMetadata(
                digest.digest(),
                attributes.lastModifiedTime().toMillis(),
                true
        );
    }

    private static void updateDigest(MessageDigest digest, String value) {
        byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
        digest.update((byte) (bytes.length >>> 24));
        digest.update((byte) (bytes.length >>> 16));
        digest.update((byte) (bytes.length >>> 8));
        digest.update((byte) bytes.length);
        digest.update(bytes);
    }

    @Override
    public void delete() throws IOException {
        if (Files.exists(file)) Files.delete(file);
    }

    @Override
    public boolean exists() {
        return Files.exists(file);
    }

    @Override
    public boolean isClosed() {
        return false;
    }

    @FunctionalInterface
    interface OpenHook {

        void run() throws IOException;

    }

    record OpenedFile(
            FileChannel channel,
            BasicFileAttributes attributes
    ) implements AutoCloseable {

        @Override
        public void close() throws IOException {
            channel.close();
        }

    }

}
