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

import de.bluecolored.bluemap.core.storage.compression.Compression;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.*;

class FileStorageTest {

    @Test
    void defaultProbeRecognizesAnExistingDirectory(@TempDir Path tempDir)
            throws Exception {
        try (FileStorage storage =
                     new FileStorage(tempDir, Compression.NONE, true)) {
            storage.initialize();
            awaitHealth(storage, true);
        }
    }

    @Test
    void healthReadsOnlyCachedStateAndExpires(@TempDir Path tempDir)
            throws Exception {
        CountDownLatch probeStarted = new CountDownLatch(1);
        CountDownLatch releaseProbe = new CountDownLatch(1);
        AtomicInteger probeCount = new AtomicInteger();
        AtomicLong nanoTime = new AtomicLong();

        try (FileStorage storage = new FileStorage(
                tempDir,
                Compression.NONE,
                true,
                nanoTime::get,
                Duration.ofSeconds(10),
                Duration.ofDays(1),
                path -> {
                    probeCount.incrementAndGet();
                    probeStarted.countDown();
                    awaitIgnoringInterrupts(releaseProbe);
                    return true;
                }
        )) {
            assertFalse(storage.isHealthy());
            assertEquals(0, probeCount.get());

            storage.initialize();
            assertTrue(probeStarted.await(2, TimeUnit.SECONDS));
            assertFalse(storage.isHealthy());
            assertEquals(1, probeCount.get());

            releaseProbe.countDown();
            awaitHealth(storage, true);
            assertEquals(1, probeCount.get());

            for (int i = 0; i < 100; i++) assertTrue(storage.isHealthy());
            assertEquals(1, probeCount.get());

            nanoTime.set(Duration.ofSeconds(10).toNanos() + 1);
            assertFalse(storage.isHealthy());
            assertEquals(1, probeCount.get());
        } finally {
            releaseProbe.countDown();
        }
    }

    @Test
    void staleProbeCannotKeepReadinessHealthyAndCloseDoesNotWait(
            @TempDir Path tempDir
    ) throws Exception {
        CountDownLatch firstProbeComplete = new CountDownLatch(1);
        CountDownLatch blockedProbeStarted = new CountDownLatch(1);
        CountDownLatch releaseBlockedProbe = new CountDownLatch(1);
        AtomicInteger probeCount = new AtomicInteger();
        AtomicLong nanoTime = new AtomicLong();

        FileStorage storage = new FileStorage(
                tempDir,
                Compression.NONE,
                true,
                nanoTime::get,
                Duration.ofSeconds(10),
                Duration.ofMillis(1),
                path -> {
                    if (probeCount.incrementAndGet() == 1) {
                        firstProbeComplete.countDown();
                        return true;
                    }

                    blockedProbeStarted.countDown();
                    awaitIgnoringInterrupts(releaseBlockedProbe);
                    return true;
                }
        );

        try {
            storage.initialize();
            assertTrue(firstProbeComplete.await(2, TimeUnit.SECONDS));
            awaitHealth(storage, true);
            assertTrue(blockedProbeStarted.await(2, TimeUnit.SECONDS));

            nanoTime.set(Duration.ofSeconds(10).toNanos() + 1);
            assertFalse(storage.isHealthy());

            assertTimeout(Duration.ofSeconds(1), storage::close);
            assertTrue(storage.isClosed());
            assertFalse(storage.isHealthy());
        } finally {
            storage.close();
            releaseBlockedProbe.countDown();
        }
    }

    @Test
    void probeDoesNotCreateAMissingRoot(@TempDir Path tempDir)
            throws Exception {
        Path missingRoot = tempDir.resolve("maps");
        CountDownLatch firstProbe = new CountDownLatch(1);

        try (FileStorage storage = new FileStorage(
                missingRoot,
                Compression.NONE,
                true,
                System::nanoTime,
                Duration.ofSeconds(10),
                Duration.ofMillis(5),
                path -> {
                    firstProbe.countDown();
                    return Files.isDirectory(path);
                }
        )) {
            storage.initialize();
            assertTrue(firstProbe.await(2, TimeUnit.SECONDS));
            assertFalse(storage.isHealthy());
            assertFalse(Files.exists(missingRoot));

            Files.createDirectory(missingRoot);
            awaitHealth(storage, true);
        }
    }

    private static void awaitHealth(FileStorage storage, boolean expected)
            throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        while (storage.isHealthy() != expected && System.nanoTime() < deadline) {
            Thread.sleep(5);
        }
        assertEquals(expected, storage.isHealthy());
    }

    private static void awaitIgnoringInterrupts(CountDownLatch latch) {
        boolean interrupted = false;
        while (true) {
            try {
                latch.await();
                break;
            } catch (InterruptedException ignored) {
                interrupted = true;
            }
        }
        if (interrupted) Thread.currentThread().interrupt();
    }

}
