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

import com.github.benmanes.caffeine.cache.LoadingCache;
import de.bluecolored.bluemap.core.storage.Storage;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.util.Caches;
import lombok.Getter;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.Duration;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.LongSupplier;
import java.util.stream.Stream;

public class FileStorage implements Storage {

    private static final Duration DEFAULT_HEALTH_MAX_AGE =
            Duration.ofSeconds(10);
    private static final Duration DEFAULT_HEALTH_INTERVAL =
            Duration.ofSeconds(1);
    private static final long NEVER_PROBED = Long.MIN_VALUE;

    @Getter private final Path root;
    private final LoadingCache<String, FileMapStorage> mapStorages;
    private final LongSupplier nanoTime;
    private final long healthMaxAgeNanos;
    private final long healthIntervalNanos;
    private final RootProbe rootProbe;
    private final AtomicBoolean closed = new AtomicBoolean();

    private volatile long lastSuccessfulProbe = NEVER_PROBED;
    private volatile boolean latestProbeHealthy;
    private ScheduledExecutorService healthExecutor;

    public FileStorage(Path root, Compression compression, boolean atomic) {
        this(
                root,
                compression,
                atomic,
                System::nanoTime,
                DEFAULT_HEALTH_MAX_AGE,
                DEFAULT_HEALTH_INTERVAL,
                FileStorage::isDirectory
        );
    }

    FileStorage(
            Path root,
            Compression compression,
            boolean atomic,
            LongSupplier nanoTime,
            Duration healthMaxAge,
            Duration healthInterval,
            RootProbe rootProbe
    ) {
        this.root = root;
        this.nanoTime = nanoTime;
        this.healthMaxAgeNanos = requirePositive(
                healthMaxAge,
                "healthMaxAge"
        );
        this.healthIntervalNanos = requirePositive(
                healthInterval,
                "healthInterval"
        );
        this.rootProbe = rootProbe;
        mapStorages = Caches.build(id -> new FileMapStorage(root.resolve(id), compression, atomic));
    }

    @Override
    public synchronized void initialize() throws IOException {
        if (closed.get()) {
            throw new IOException("File storage is closed");
        }
        if (healthExecutor != null) return;

        healthExecutor = Executors.newSingleThreadScheduledExecutor(task -> {
            Thread thread = new Thread(
                    task,
                    "bluemap-file-storage-health"
            );
            thread.setDaemon(true);
            return thread;
        });
        healthExecutor.scheduleWithFixedDelay(
                this::refreshHealth,
                0,
                healthIntervalNanos,
                TimeUnit.NANOSECONDS
        );
    }

    @Override
    public FileMapStorage map(String mapId) {
        return mapStorages.get(mapId);
    }

    @SuppressWarnings("resource")
    @Override
    public Stream<String> mapIds() throws IOException {
        if (!Files.exists(root)) return Stream.empty();
        return Files.list(root)
                .filter(Files::isDirectory)
                .map(Path::getFileName)
                .map(Path::toString);
    }

    @Override
    public boolean isClosed() {
        return closed.get();
    }

    @Override
    public boolean isHealthy() {
        if (closed.get() || !latestProbeHealthy) return false;

        long lastSuccess = lastSuccessfulProbe;
        if (lastSuccess == NEVER_PROBED) return false;

        long age = nanoTime.getAsLong() - lastSuccess;
        return age >= 0 && age <= healthMaxAgeNanos;
    }

    @Override
    public synchronized void close() {
        if (!closed.compareAndSet(false, true)) return;

        latestProbeHealthy = false;
        lastSuccessfulProbe = NEVER_PROBED;
        if (healthExecutor != null) healthExecutor.shutdownNow();
    }

    private void refreshHealth() {
        if (closed.get()) return;

        boolean healthy;
        try {
            healthy = rootProbe.isDirectory(root);
        } catch (IOException | RuntimeException ignored) {
            healthy = false;
        }

        if (closed.get()) return;
        if (healthy) lastSuccessfulProbe = nanoTime.getAsLong();
        latestProbeHealthy = healthy;
    }

    private static boolean isDirectory(Path path) throws IOException {
        return Files.readAttributes(path, BasicFileAttributes.class)
                .isDirectory();
    }

    private static long requirePositive(Duration duration, String name) {
        if (duration.isZero() || duration.isNegative()) {
            throw new IllegalArgumentException(name + " must be positive");
        }
        try {
            return duration.toNanos();
        } catch (ArithmeticException e) {
            throw new IllegalArgumentException(name + " is too large", e);
        }
    }

    @FunctionalInterface
    interface RootProbe {

        boolean isDirectory(Path path) throws IOException;

    }

}
