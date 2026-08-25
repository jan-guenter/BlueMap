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
package de.bluecolored.bluemap.webserver.metrics;

import de.bluecolored.bluemap.common.web.http.HttpRequest;
import de.bluecolored.bluemap.common.web.http.HttpResponse;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;
import org.junit.jupiter.api.Test;

import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.LongSupplier;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ConnectionMetricsTest {

    @Test
    void computesElapsedTimeWeightedRollingAverages() {
        AtomicInteger active = new AtomicInteger(10);
        AtomicLong time = new AtomicLong();
        try (ConnectionMetrics metrics = metrics(active, time, 100)) {
            advance(time, Duration.ofSeconds(10));
            active.set(30);
            metrics.sample();

            advance(time, Duration.ofSeconds(30));
            active.set(50);
            metrics.sample();

            advance(time, Duration.ofSeconds(30));
            active.set(70);
            ConnectionMetrics.Snapshot snapshot = metrics.snapshot();

            assertEquals(70, snapshot.active());
            assertEquals(100, snapshot.limit());
            assertEquals(40, snapshot.average1m(), 0.000_001);
            assertEquals(2500d / 70d, snapshot.average5m(), 0.000_001);
            assertEquals(0.7, snapshot.utilization(), 0.000_001);
            assertEquals(0.4, snapshot.utilization1m(), 0.000_001);
            assertEquals(25d / 70d, snapshot.utilization5m(), 0.000_001);
        }
    }

    @Test
    void trimsHistoryAtFiveMinutesAndUsesAvailableStartupHistory() {
        AtomicInteger active = new AtomicInteger(0);
        AtomicLong time = new AtomicLong();
        try (ConnectionMetrics metrics = metrics(active, time, 100)) {
            advance(time, Duration.ofMinutes(1));
            active.set(10);
            metrics.sample();

            advance(time, Duration.ofMinutes(4));
            active.set(20);
            metrics.sample();

            advance(time, Duration.ofMinutes(1));
            active.set(30);
            ConnectionMetrics.Snapshot snapshot = metrics.snapshot();

            assertEquals(20, snapshot.average1m(), 0.000_001);
            assertEquals(12, snapshot.average5m(), 0.000_001);
        }
    }

    @Test
    void exportsOpenMetricsWithoutRequestLabels() throws Exception {
        AtomicInteger active = new AtomicInteger(2);
        AtomicLong time = new AtomicLong();
        try (ConnectionMetrics metrics = metrics(active, time, 10)) {
            advance(time, Duration.ofMinutes(1));
            active.set(4);
            metrics.sample();
            advance(time, Duration.ofMinutes(4));
            active.set(6);
            metrics.sample();
            advance(time, Duration.ofMinutes(1));
            active.set(8);

            try (HttpResponse response = metrics.handle(request("GET", "/metrics"))) {
                assertEquals(HttpStatusCode.OK, response.getStatusCode());
                assertEquals(
                        "application/openmetrics-text; version=1.0.0; charset=utf-8",
                        response.getHeader("Content-Type").getValue()
                );
                assertEquals(
                        "no-store,no-transform",
                        response.getHeader("Cache-Control").getValue()
                );

                String body = new String(
                        response.getBody().readAllBytes(),
                        StandardCharsets.UTF_8
                );
                Map<String, Double> gauges = gauges(body);
                assertEquals(7, gauges.size());
                assertEquals(8d, gauges.get("bluemap_web_http_connections"));
                assertEquals(10d, gauges.get("bluemap_web_http_connections_limit"));
                assertEquals(6d, gauges.get("bluemap_web_http_connections_average_1m"));
                assertEquals(4.4, gauges.get("bluemap_web_http_connections_average_5m"));
                assertEquals(0.8, gauges.get("bluemap_web_http_connection_utilization_ratio"));
                assertEquals(
                        0.6,
                        gauges.get(
                                "bluemap_web_http_connection_utilization_average_1m_ratio"
                        )
                );
                assertEquals(
                        0.44,
                        gauges.get(
                                "bluemap_web_http_connection_utilization_average_5m_ratio"
                        ),
                        0.000_001
                );
                assertTrue(body.endsWith("# EOF\n"));
                assertTrue(!body.contains("{"));
            }
        }
    }

    @Test
    void schedulesSamplesAtOneSecondAndStopsTheSampler() {
        AtomicInteger active = new AtomicInteger(1);
        AtomicLong time = new AtomicLong();
        RecordingScheduler sampler = new RecordingScheduler();
        try (ConnectionMetrics metrics = new ConnectionMetrics(
                active::get,
                10,
                time::get,
                sampler
        )) {
            assertEquals(
                    ConnectionMetrics.SAMPLE_INTERVAL.toNanos(),
                    sampler.initialDelay
            );
            assertEquals(
                    ConnectionMetrics.SAMPLE_INTERVAL.toNanos(),
                    sampler.delay
            );
            assertEquals(TimeUnit.NANOSECONDS, sampler.unit);

            active.set(2);
            advance(time, ConnectionMetrics.SAMPLE_INTERVAL);
            sampler.runScheduled();
            assertEquals(2, metrics.snapshot().active());
        }
        assertTrue(sampler.isShutdown());
    }

    @Test
    void failsScrapesClosedAfterSamplingFailureOrStaleness() throws Exception {
        AtomicInteger active = new AtomicInteger(1);
        AtomicLong time = new AtomicLong();
        RecordingScheduler failedSampler = new RecordingScheduler();
        try (ConnectionMetrics metrics = new ConnectionMetrics(
                active::get,
                10,
                time::get,
                failedSampler
        )) {
            active.set(11);
            failedSampler.runScheduled();
            active.set(1);

            assertThrows(IllegalStateException.class, metrics::snapshot);
            try (HttpResponse response = metrics.handle(request("GET", "/metrics"))) {
                assertEquals(
                        HttpStatusCode.SERVICE_UNAVAILABLE,
                        response.getStatusCode()
                );
            }
        }

        RecordingScheduler staleSampler = new RecordingScheduler();
        try (ConnectionMetrics metrics = new ConnectionMetrics(
                active::get,
                10,
                time::get,
                staleSampler
        )) {
            advance(time, ConnectionMetrics.MAX_SAMPLE_AGE.plusNanos(1));
            assertThrows(IllegalStateException.class, metrics::snapshot);
        }
    }

    @Test
    void closesSamplerWhenInitialSampleFails() {
        RecordingScheduler sampler = new RecordingScheduler();
        assertThrows(
                IllegalStateException.class,
                () -> new ConnectionMetrics(
                        () -> 11,
                        10,
                        System::nanoTime,
                        sampler
                )
        );
        assertTrue(sampler.isShutdown());
    }

    @Test
    void serializesSnapshotTimeWithPeriodicSamples() throws Exception {
        AtomicInteger clockReads = new AtomicInteger();
        AtomicLong time = new AtomicLong();
        CountDownLatch snapshotClockRead = new CountDownLatch(1);
        CountDownLatch releaseSnapshotClock = new CountDownLatch(1);
        CountDownLatch sampleAttempted = new CountDownLatch(1);
        LongSupplier clock = () -> {
            long value = time.get();
            if (clockReads.incrementAndGet() == 2) {
                snapshotClockRead.countDown();
                try {
                    if (!releaseSnapshotClock.await(2, TimeUnit.SECONDS)) {
                        throw new AssertionError("Snapshot clock was not released");
                    }
                } catch (InterruptedException ex) {
                    Thread.currentThread().interrupt();
                    throw new AssertionError(ex);
                }
            }
            return value;
        };

        try (ConnectionMetrics metrics = new ConnectionMetrics(
                () -> 1,
                10,
                clock,
                null
        ); ExecutorService executor = Executors.newFixedThreadPool(2)) {
            Future<ConnectionMetrics.Snapshot> snapshot =
                    executor.submit(metrics::snapshot);
            try {
                assertTrue(snapshotClockRead.await(2, TimeUnit.SECONDS));
                time.set(1);
                Future<?> sample = executor.submit(() -> {
                    sampleAttempted.countDown();
                    metrics.sample();
                });
                assertTrue(sampleAttempted.await(2, TimeUnit.SECONDS));
                assertFalse(sample.isDone());

                releaseSnapshotClock.countDown();
                assertEquals(1, snapshot.get(2, TimeUnit.SECONDS).active());
                sample.get(2, TimeUnit.SECONDS);
            } finally {
                releaseSnapshotClock.countDown();
            }
        }
    }

    @Test
    void rejectsUnsupportedPathsAndMethods() throws Exception {
        AtomicInteger active = new AtomicInteger();
        AtomicLong time = new AtomicLong();
        try (ConnectionMetrics metrics = metrics(active, time, 10);
             HttpResponse missing = metrics.handle(request("GET", "/missing"));
             HttpResponse post = metrics.handle(request("POST", "/metrics"))) {
            assertEquals(HttpStatusCode.NOT_FOUND, missing.getStatusCode());
            assertEquals(HttpStatusCode.METHOD_NOT_ALLOWED, post.getStatusCode());
            assertEquals("GET, HEAD", post.getHeader("Allow").getValue());
        }
    }

    @Test
    void failsOnInvalidConnectionCountsAndClockRegression() {
        AtomicInteger active = new AtomicInteger(11);
        AtomicLong time = new AtomicLong();
        assertThrows(
                IllegalStateException.class,
                () -> metrics(active, time, 10)
        );

        active.set(1);
        try (ConnectionMetrics metrics = metrics(active, time, 10)) {
            time.set(-1);
            assertThrows(IllegalStateException.class, metrics::sample);
        }
    }

    private static ConnectionMetrics metrics(
            AtomicInteger active,
            AtomicLong time,
            int limit
    ) {
        return new ConnectionMetrics(
                active::get,
                limit,
                time::get,
                null
        );
    }

    private static HttpRequest request(String method, String path)
            throws Exception {
        return new HttpRequest(
                InetAddress.getLoopbackAddress(),
                method,
                path
        );
    }

    private static void advance(AtomicLong time, Duration duration) {
        time.addAndGet(duration.toNanos());
    }

    private static Map<String, Double> gauges(String body) {
        Map<String, Double> gauges = new LinkedHashMap<>();
        body.lines()
                .filter(line -> !line.isBlank() && !line.startsWith("#"))
                .forEach(line -> {
                    String[] parts = line.split(" ", 2);
                    assertEquals(2, parts.length);
                    assertNull(gauges.put(parts[0], Double.parseDouble(parts[1])));
                });
        return gauges;
    }

    private static final class RecordingScheduler
            extends ScheduledThreadPoolExecutor {

        private Runnable command;
        private long initialDelay;
        private long delay;
        private TimeUnit unit;

        private RecordingScheduler() {
            super(1);
        }

        @Override
        public ScheduledFuture<?> scheduleWithFixedDelay(
                Runnable command,
                long initialDelay,
                long delay,
                TimeUnit unit
        ) {
            this.command = command;
            this.initialDelay = initialDelay;
            this.delay = delay;
            this.unit = unit;
            return super.scheduleWithFixedDelay(
                    command,
                    1,
                    1,
                    TimeUnit.DAYS
            );
        }

        private void runScheduled() {
            command.run();
        }
    }
}
