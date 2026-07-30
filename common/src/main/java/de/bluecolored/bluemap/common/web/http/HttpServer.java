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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;

public class HttpServer extends Server {

    @Getter @Setter
    private HttpRequestHandler requestHandler;
    private final ExecutorService executor;
    private final boolean ownsExecutor;
    private final HttpServerSettings settings;
    private final Semaphore activeConnections;

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
        this.settings = settings;
        this.activeConnections = new Semaphore(settings.maxActiveConnections());
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
        if (!activeConnections.tryAcquire()) {
            connection.close();
            return;
        }

        try {
            connection.socket().setSoTimeout((int) settings.idleTimeout().toMillis());
            HttpConnection httpConnection =
                    new HttpConnection(connection.socket(), requestHandler, settings.requestLimits());
            executor.execute(() -> {
                try {
                    httpConnection.run();
                } finally {
                    activeConnections.release();
                }
            });
        } catch (IOException | RuntimeException ex) {
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

    @Override
    public void close() throws IOException {
        try {
            super.close();
        } finally {
            if (ownsExecutor) executor.shutdownNow();
        }
    }

}
