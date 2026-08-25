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
package de.bluecolored.bluemap.common.web.http;

import lombok.Getter;
import lombok.Setter;

import java.io.IOException;
import java.nio.channels.SocketChannel;
import java.time.Duration;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public class HttpServer extends Server {

    private static final Duration EXECUTOR_STOP_TIMEOUT = Duration.ofSeconds(5);

    @Getter @Setter
    private HttpRequestHandler requestHandler;
    private final ExecutorService executor;
    private final boolean ownsExecutor;
    private final ScheduledThreadPoolExecutor timeoutExecutor;
    private final HttpServerSettings settings;
    private final Semaphore activeConnections;
    private final Semaphore longLivedConnections;
    private final Set<HttpConnection> httpConnections =
            ConcurrentHashMap.newKeySet();
    private final AtomicBoolean stopping = new AtomicBoolean();

    public HttpServer(
            String name,
            HttpRequestHandler requestHandler,
            ExecutorService executor,
            HttpServerSettings settings
    ) throws IOException {
        this(name, requestHandler, executor, settings, false);
    }

    private HttpServer(
            String name,
            HttpRequestHandler requestHandler,
            ExecutorService executor,
            HttpServerSettings settings,
            boolean ownsExecutor
    ) throws IOException {
        super(name);
        this.requestHandler = requestHandler;
        this.executor = executor;
        this.ownsExecutor = ownsExecutor;
        this.timeoutExecutor = new ScheduledThreadPoolExecutor(1, runnable -> {
            Thread thread = new Thread(runnable, name + "-Timeouts");
            thread.setDaemon(true);
            return thread;
        });
        this.timeoutExecutor.setRemoveOnCancelPolicy(true);
        this.settings = settings;
        this.activeConnections = new Semaphore(settings.maxActiveConnections());
        this.longLivedConnections = new Semaphore(
                settings.maxLongLivedConnections()
        );
    }

    public HttpServer(String name, HttpRequestHandler requestHandler, ExecutorService executor) throws IOException {
        this(name, requestHandler, executor, HttpServerSettings.DEFAULT);
    }

    public HttpServer(String name, HttpRequestHandler requestHandler) throws IOException {
        this(name, requestHandler, Executors.newVirtualThreadPerTaskExecutor(), HttpServerSettings.DEFAULT, true);
    }

    public HttpServer(String name, HttpRequestHandler requestHandler, HttpServerSettings settings) throws IOException {
        this(name, requestHandler, Executors.newVirtualThreadPerTaskExecutor(), settings, true);
    }

    @Override
    public void handleConnection(SocketChannel connection) throws IOException {
        if (stopping.get()) {
            connection.close();
            return;
        }

        trackConnection(connection);
        if (!activeConnections.tryAcquire()) {
            untrackConnection(connection);
            connection.close();
            return;
        }
        if (stopping.get()) {
            untrackConnection(connection);
            activeConnections.release();
            connection.close();
            return;
        }

        try {
            connection.socket().setSoTimeout((int) settings.idleTimeout().toMillis());
            HttpConnection httpConnection =
                    new HttpConnection(
                            connection.socket(),
                            requestHandler,
                            settings.requestLimits(),
                            timeoutExecutor,
                            settings.idleTimeout(),
                            longLivedConnections
                    );
            httpConnections.add(httpConnection);
            if (stopping.get()) httpConnection.beginDrain();
            try {
                executor.execute(() -> {
                    try {
                        httpConnection.run();
                    } finally {
                        httpConnections.remove(httpConnection);
                        untrackConnection(connection);
                        activeConnections.release();
                    }
                });
            } catch (RuntimeException ex) {
                httpConnections.remove(httpConnection);
                throw ex;
            }
        } catch (IOException | RuntimeException ex) {
            untrackConnection(connection);
            activeConnections.release();
            try {
                connection.close();
            } catch (IOException closeException) {
                ex.addSuppressed(closeException);
            }
            if (ex instanceof IOException ioException) throw ioException;
            throw new IOException("Failed to dispatch HTTP connection", ex);
        }
    }

    public int getActiveConnectionCount() {
        return settings.maxActiveConnections() - activeConnections.availablePermits();
    }

    boolean isExecutorShutdown() {
        return executor.isShutdown();
    }

    /**
     * Stops accepting new connections, lets current responses finish, and
     * force-closes anything still active after the grace period.
     *
     * @return true when every connection drained within the grace period
     */
    public boolean closeGracefully(Duration gracePeriod) throws IOException {
        Objects.requireNonNull(gracePeriod, "gracePeriod");
        if (gracePeriod.isNegative()) {
            throw new IllegalArgumentException("gracePeriod must not be negative");
        }
        stopping.set(true);

        IOException exception = null;
        try {
            stopAccepting();
        } catch (IOException ex) {
            exception = ex;
        }

        httpConnections.forEach(HttpConnection::beginDrain);

        boolean drained = false;
        try {
            drained = activeConnections.tryAcquire(
                    settings.maxActiveConnections(),
                    gracePeriod.toNanos(),
                    TimeUnit.NANOSECONDS
            );
            if (drained) {
                activeConnections.release(settings.maxActiveConnections());
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }

        if (!drained) {
            try {
                closeActiveConnections();
            } catch (IOException ex) {
                if (exception == null) exception = ex;
                else exception.addSuppressed(ex);
            }
        }

        try {
            stopOwnedExecutor(!drained);
        } catch (IOException ex) {
            if (exception == null) exception = ex;
            else exception.addSuppressed(ex);
        }
        try {
            stopTimeoutExecutor();
        } catch (IOException ex) {
            if (exception == null) exception = ex;
            else exception.addSuppressed(ex);
        }

        if (exception != null) throw exception;
        return drained;
    }

    @Override
    public void close() throws IOException {
        stopping.set(true);
        IOException exception = null;
        try {
            super.close();
        } catch (IOException ex) {
            exception = ex;
        }
        try {
            stopOwnedExecutor(true);
        } catch (IOException ex) {
            if (exception == null) exception = ex;
            else exception.addSuppressed(ex);
        }
        try {
            stopTimeoutExecutor();
        } catch (IOException ex) {
            if (exception == null) exception = ex;
            else exception.addSuppressed(ex);
        }
        if (exception != null) throw exception;
    }

    private void stopTimeoutExecutor() throws IOException {
        timeoutExecutor.shutdownNow();
        boolean terminated;
        try {
            terminated = timeoutExecutor.awaitTermination(
                    EXECUTOR_STOP_TIMEOUT.toNanos(),
                    TimeUnit.NANOSECONDS
            );
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            throw new IOException(
                    "Interrupted while stopping HTTP timeout tasks", ex
            );
        }
        if (!terminated) {
            throw new IOException(
                    "HTTP timeout tasks did not stop within "
                            + EXECUTOR_STOP_TIMEOUT
            );
        }
    }

    private void stopOwnedExecutor(boolean force) throws IOException {
        if (!ownsExecutor) return;
        if (force) executor.shutdownNow();
        else executor.shutdown();

        boolean terminated;
        try {
            terminated = executor.awaitTermination(
                    EXECUTOR_STOP_TIMEOUT.toNanos(),
                    TimeUnit.NANOSECONDS
            );
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            throw new IOException(
                    "Interrupted while stopping HTTP request tasks", ex
            );
        }
        if (!terminated) {
            throw new IOException(
                    "HTTP request tasks did not stop within "
                            + EXECUTOR_STOP_TIMEOUT
            );
        }
    }

}
