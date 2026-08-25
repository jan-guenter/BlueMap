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

import de.bluecolored.bluemap.core.logger.Logger;
import lombok.RequiredArgsConstructor;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.EOFException;
import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.time.Duration;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

public class HttpConnection implements Runnable {

    private static final ScheduledThreadPoolExecutor DEFAULT_TIMEOUT_EXECUTOR =
            new ScheduledThreadPoolExecutor(1, runnable -> {
                Thread thread = new Thread(runnable, "BlueMap-Http-Timeouts");
                thread.setDaemon(true);
                return thread;
            });

    static {
        DEFAULT_TIMEOUT_EXECUTOR.setRemoveOnCancelPolicy(true);
    }

    private final Socket socket;
    private final HttpRequestInputStream requestIn;
    private final HttpResponseOutputStream responseOut;
    private final HttpRequestHandler requestHandler;
    private final ConnectionDeadline requestDeadline;
    private final ConnectionDeadline responseDeadline;
    private final Semaphore longLivedConnections;
    private final Object stateLock = new Object();
    private boolean processingRequest;
    private volatile boolean draining;

    public HttpConnection(Socket socket, HttpRequestHandler requestHandler) throws IOException {
        this(
                socket,
                requestHandler,
                HttpRequestLimits.DEFAULT,
                DEFAULT_TIMEOUT_EXECUTOR,
                HttpServerSettings.DEFAULT.idleTimeout(),
                null
        );
    }

    public HttpConnection(Socket socket, HttpRequestHandler requestHandler, HttpRequestLimits limits) throws IOException {
        this(
                socket,
                requestHandler,
                limits,
                DEFAULT_TIMEOUT_EXECUTOR,
                HttpServerSettings.DEFAULT.idleTimeout(),
                null
        );
    }

    HttpConnection(
            Socket socket,
            HttpRequestHandler requestHandler,
            HttpRequestLimits limits,
            ScheduledExecutorService timeoutExecutor,
            Duration timeout,
            Semaphore longLivedConnections
    ) throws IOException {
        this.socket = socket;
        this.requestHandler = requestHandler;
        this.requestDeadline = new ConnectionDeadline(
                socket, timeoutExecutor, timeout
        );
        this.responseDeadline = new ConnectionDeadline(
                socket, timeoutExecutor, timeout
        );
        this.longLivedConnections = longLivedConnections;

        this.requestIn = new HttpRequestInputStream(
                new BufferedInputStream(socket.getInputStream()),
                socket.getInetAddress(),
                limits
        );
        this.responseOut = new HttpResponseOutputStream(new BufferedOutputStream(
                new ProgressDeadlineOutputStream(
                        socket.getOutputStream(), responseDeadline
                )
        ));
    }

    public void run() {
        try {
            while (socket.isConnected() && !socket.isClosed() && !socket.isInputShutdown() && !socket.isOutputShutdown()) {
                HttpRequest request;
                requestDeadline.arm();
                try {
                    request = requestIn.read();
                } finally {
                    requestDeadline.disarm();
                }
                if (request == null) continue;

                synchronized (stateLock) {
                    if (draining) break;
                    processingRequest = true;
                }
                boolean longLivedPermit = false;
                responseDeadline.arm();
                try {
                    try (HttpResponse response = requestHandler.handle(request)) {
                        if (request.getMethod().equalsIgnoreCase("HEAD")) {
                            response.setBodySuppressed(true);
                        }
                        if (response.isLongLived()
                                && longLivedConnections != null) {
                            if (!longLivedConnections.tryAcquire()) {
                                writeLongLivedCapacityResponse();
                                return;
                            }
                            longLivedPermit = true;
                        }
                        responseOut.write(response);
                    }
                } finally {
                    responseDeadline.disarm();
                    if (longLivedPermit) longLivedConnections.release();
                    synchronized (stateLock) {
                        processingRequest = false;
                    }
                }
                if (draining) break;
            }
        } catch (EOFException | SocketTimeoutException ignore) {
            // ignore known exceptions that happen when browsers or us close the connection
        } catch (IOException e) {
            if ( // ignore known exceptions that happen when browsers close the connection
                    e.getMessage() == null ||
                    !e.getMessage().equals("Broken pipe")
            ) {
                Logger.global.logDebug("Exception in HttpConnection: " + e);
            }
        } catch (Exception e) {
            Logger.global.logDebug("Exception in HttpConnection: " + e);
        } finally {
            requestDeadline.disarm();
            responseDeadline.disarm();
            try {
                socket.close();
            } catch (IOException e) {
                Logger.global.logDebug("Exception closing HttpConnection: " + e);
            }
        }
    }

    private void writeLongLivedCapacityResponse() throws IOException {
        try (HttpResponse response = new HttpResponse(
                HttpStatusCode.SERVICE_UNAVAILABLE
        )) {
            response.addHeader("Retry-After", "5");
            response.addHeader("Cache-Control", "private,no-store,no-transform");
            responseOut.write(response);
        }
    }

    private static final class ConnectionDeadline {

        private final Socket socket;
        private final ScheduledExecutorService executor;
        private final Duration timeout;
        private final Object lock = new Object();
        private long generation;
        private ScheduledFuture<?> pending;
        private Thread owner;

        private ConnectionDeadline(
                Socket socket,
                ScheduledExecutorService executor,
                Duration timeout
        ) {
            this.socket = socket;
            this.executor = executor;
            this.timeout = timeout;
        }

        void arm() {
            long armedGeneration;
            synchronized (lock) {
                disarmLocked();
                armedGeneration = generation;
                owner = Thread.currentThread();
                pending = executor.schedule(
                        () -> expire(armedGeneration),
                        timeout.toNanos(),
                        TimeUnit.NANOSECONDS
                );
            }
        }

        void disarm() {
            synchronized (lock) {
                disarmLocked();
            }
        }

        private void disarmLocked() {
            generation++;
            owner = null;
            if (pending != null) {
                pending.cancel(false);
                pending = null;
            }
        }

        private void expire(long armedGeneration) {
            synchronized (lock) {
                if (armedGeneration != generation) return;
                generation++;
                pending = null;
                Thread thread = owner;
                owner = null;
                // Interrupt before the owner can disarm this deadline and
                // return to a reusable executor. Otherwise this callback could
                // interrupt the worker's next task.
                if (thread != null) thread.interrupt();
            }

            try {
                socket.close();
            } catch (IOException e) {
                Logger.global.logDebug("Exception closing timed out HttpConnection: " + e);
            }
        }

    }

    private static final class ProgressDeadlineOutputStream extends FilterOutputStream {

        private final ConnectionDeadline deadline;

        private ProgressDeadlineOutputStream(
                OutputStream outputStream,
                ConnectionDeadline deadline
        ) {
            super(outputStream);
            this.deadline = deadline;
        }

        @Override
        public void write(int value) throws IOException {
            out.write(value);
            deadline.arm();
        }

        @Override
        public void write(byte[] bytes, int offset, int length) throws IOException {
            out.write(bytes, offset, length);
            if (length > 0) deadline.arm();
        }

        @Override
        public void flush() throws IOException {
            out.flush();
        }

    }

    void beginDrain() {
        synchronized (stateLock) {
            draining = true;
            if (processingRequest) return;
        }

        try {
            socket.close();
        } catch (IOException e) {
            Logger.global.logDebug("Exception draining idle HttpConnection: " + e);
        }
    }

}
