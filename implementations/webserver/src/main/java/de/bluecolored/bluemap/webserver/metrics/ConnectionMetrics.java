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
import de.bluecolored.bluemap.common.web.http.HttpRequestHandler;
import de.bluecolored.bluemap.common.web.http.HttpResponse;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;

import java.time.Duration;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Iterator;
import java.util.Locale;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.IntSupplier;
import java.util.function.LongSupplier;

public final class ConnectionMetrics implements HttpRequestHandler, AutoCloseable {

    static final Duration ONE_MINUTE = Duration.ofMinutes(1);
    static final Duration FIVE_MINUTES = Duration.ofMinutes(5);
    static final Duration SAMPLE_INTERVAL = Duration.ofSeconds(1);
    static final Duration MAX_SAMPLE_AGE = SAMPLE_INTERVAL.multipliedBy(3);

    private final IntSupplier activeConnections;
    private final int connectionLimit;
    private final LongSupplier nanoTime;
    private final ScheduledExecutorService sampler;
    private final History history = new History();
    private volatile long lastSuccessfulSampleTime;
    private volatile RuntimeException samplingFailure;

    public ConnectionMetrics(
            IntSupplier activeConnections,
            int connectionLimit
    ) {
        this(
                activeConnections,
                connectionLimit,
                System::nanoTime,
                Executors.newSingleThreadScheduledExecutor(runnable -> {
                    Thread thread = new Thread(
                            runnable,
                            "BlueMap-Webserver-Connection-Metrics"
                    );
                    thread.setDaemon(true);
                    return thread;
                })
        );
    }

    ConnectionMetrics(
            IntSupplier activeConnections,
            int connectionLimit,
            LongSupplier nanoTime,
            ScheduledExecutorService sampler
    ) {
        if (connectionLimit < 1) {
            throw new IllegalArgumentException(
                    "connectionLimit must be positive"
            );
        }
        this.activeConnections = activeConnections;
        this.connectionLimit = connectionLimit;
        this.nanoTime = nanoTime;
        this.sampler = sampler;

        try {
            sample();
            if (sampler != null) {
                sampler.scheduleWithFixedDelay(
                        this::samplePeriodically,
                        SAMPLE_INTERVAL.toNanos(),
                        SAMPLE_INTERVAL.toNanos(),
                        TimeUnit.NANOSECONDS
                );
            }
        } catch (RuntimeException ex) {
            if (sampler != null) sampler.shutdownNow();
            throw ex;
        }
    }

    @Override
    public HttpResponse handle(HttpRequest request) {
        String path = request.getPath();
        if (!"metrics".equals(path) && !"/metrics".equals(path)) {
            return response(HttpStatusCode.NOT_FOUND);
        }
        if (!request.getMethod().equalsIgnoreCase("GET")
                && !request.getMethod().equalsIgnoreCase("HEAD")) {
            HttpResponse response = response(HttpStatusCode.METHOD_NOT_ALLOWED);
            response.addHeader("Allow", "GET, HEAD");
            return response;
        }

        Snapshot snapshot;
        try {
            snapshot = snapshot();
        } catch (IllegalStateException ex) {
            return response(HttpStatusCode.SERVICE_UNAVAILABLE);
        }
        HttpResponse response = response(HttpStatusCode.OK);
        response.addHeader(
                "Content-Type",
                "application/openmetrics-text; version=1.0.0; charset=utf-8"
        );
        response.setBody(render(snapshot));
        return response;
    }

    private static HttpResponse response(HttpStatusCode status) {
        HttpResponse response = new HttpResponse(status);
        response.addHeader("Cache-Control", "no-store,no-transform");
        return response;
    }

    synchronized void sample() {
        long time = nanoTime.getAsLong();
        history.record(time, activeConnectionCount());
        lastSuccessfulSampleTime = time;
    }

    synchronized void samplePeriodically() {
        if (samplingFailure != null) return;
        try {
            sample();
        } catch (RuntimeException ex) {
            samplingFailure = ex;
        }
    }

    synchronized Snapshot snapshot() {
        RuntimeException failure = samplingFailure;
        if (failure != null) {
            throw new IllegalStateException(
                    "Connection metric sampling failed",
                    failure
            );
        }

        long time = nanoTime.getAsLong();
        if (sampler != null
                && time - lastSuccessfulSampleTime
                > MAX_SAMPLE_AGE.toNanos()) {
            throw new IllegalStateException(
                    "Connection metric samples are stale"
            );
        }

        Snapshot snapshot = history.snapshot(
                time,
                activeConnectionCount(),
                connectionLimit,
                ONE_MINUTE,
                FIVE_MINUTES
        );
        failure = samplingFailure;
        if (failure != null) {
            throw new IllegalStateException(
                    "Connection metric sampling failed",
                    failure
            );
        }
        return snapshot;
    }

    private int activeConnectionCount() {
        int active = activeConnections.getAsInt();
        if (active < 0 || active > connectionLimit) {
            throw new IllegalStateException(
                    "Active connection count is outside the configured limit: "
                            + active
            );
        }
        return active;
    }

    private static String render(Snapshot snapshot) {
        return String.format(Locale.ROOT, """
                # HELP bluemap_web_http_connections Active connections accepted by the public HTTP listener.
                # TYPE bluemap_web_http_connections gauge
                bluemap_web_http_connections %d
                # HELP bluemap_web_http_connections_limit Configured maximum active connections for the public HTTP listener.
                # TYPE bluemap_web_http_connections_limit gauge
                bluemap_web_http_connections_limit %d
                # HELP bluemap_web_http_connections_average_1m Time-weighted active connection average over up to one minute.
                # TYPE bluemap_web_http_connections_average_1m gauge
                bluemap_web_http_connections_average_1m %s
                # HELP bluemap_web_http_connections_average_5m Time-weighted active connection average over up to five minutes.
                # TYPE bluemap_web_http_connections_average_5m gauge
                bluemap_web_http_connections_average_5m %s
                # HELP bluemap_web_http_connection_utilization_ratio Active connections divided by the configured limit.
                # TYPE bluemap_web_http_connection_utilization_ratio gauge
                bluemap_web_http_connection_utilization_ratio %s
                # HELP bluemap_web_http_connection_utilization_average_1m_ratio One-minute active connection average divided by the configured limit.
                # TYPE bluemap_web_http_connection_utilization_average_1m_ratio gauge
                bluemap_web_http_connection_utilization_average_1m_ratio %s
                # HELP bluemap_web_http_connection_utilization_average_5m_ratio Five-minute active connection average divided by the configured limit.
                # TYPE bluemap_web_http_connection_utilization_average_5m_ratio gauge
                bluemap_web_http_connection_utilization_average_5m_ratio %s
                # EOF
                """,
                snapshot.active(),
                snapshot.limit(),
                decimal(snapshot.average1m()),
                decimal(snapshot.average5m()),
                decimal(snapshot.utilization()),
                decimal(snapshot.utilization1m()),
                decimal(snapshot.utilization5m())
        );
    }

    private static String decimal(double value) {
        return Double.toString(value);
    }

    @Override
    public void close() {
        if (sampler != null) sampler.shutdownNow();
    }

    record Snapshot(
            int active,
            int limit,
            double average1m,
            double average5m,
            double utilization,
            double utilization1m,
            double utilization5m
    ) {}

    private record Sample(long time, int active) {}

    private static final class History {

        private final Deque<Sample> samples = new ArrayDeque<>();

        synchronized void record(long time, int active) {
            Sample last = samples.peekLast();
            if (last != null && time < last.time()) {
                throw new IllegalStateException("Monotonic clock moved backwards");
            }
            if (last != null && time == last.time()) samples.removeLast();
            samples.addLast(new Sample(time, active));

            long cutoff = time - FIVE_MINUTES.toNanos();
            while (samples.size() > 1) {
                Iterator<Sample> iterator = samples.iterator();
                iterator.next();
                Sample second = iterator.next();
                if (second.time() > cutoff) break;
                samples.removeFirst();
            }
        }

        synchronized Snapshot snapshot(
                long time,
                int active,
                int limit,
                Duration oneMinute,
                Duration fiveMinutes
        ) {
            Sample current = samples.peekLast();
            if (current == null) throw new IllegalStateException("No samples");
            if (time < current.time()) {
                throw new IllegalStateException("Monotonic clock moved backwards");
            }

            double average1m = average(time, oneMinute);
            double average5m = average(time, fiveMinutes);
            return new Snapshot(
                    active,
                    limit,
                    average1m,
                    average5m,
                    active / (double) limit,
                    average1m / limit,
                    average5m / limit
            );
        }

        private double average(long time, Duration window) {
            Sample first = samples.peekFirst();
            Sample last = samples.peekLast();
            if (first == null || last == null) {
                throw new IllegalStateException("No samples");
            }

            long start = Math.max(first.time(), time - window.toNanos());
            if (start == time) return last.active();

            Sample previous = first;
            long previousTime = start;
            double area = 0;
            for (Sample sample : samples) {
                if (sample.time() <= start) {
                    previous = sample;
                    continue;
                }
                if (sample.time() > time) break;
                area += previous.active()
                        * (double) (sample.time() - previousTime);
                previous = sample;
                previousTime = sample.time();
            }
            area += previous.active() * (double) (time - previousTime);
            return area / (time - start);
        }
    }
}
