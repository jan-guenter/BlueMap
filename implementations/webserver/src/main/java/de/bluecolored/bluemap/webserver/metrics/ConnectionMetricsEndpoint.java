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

import de.bluecolored.bluemap.common.web.http.HttpRequestLimits;
import de.bluecolored.bluemap.common.web.http.HttpServer;
import de.bluecolored.bluemap.common.web.http.HttpServerSettings;

import java.io.Closeable;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.time.Duration;

public final class ConnectionMetricsEndpoint implements Closeable {

    private static final int MAX_METRICS_CONNECTIONS = 4;
    private static final Duration METRICS_IDLE_TIMEOUT = Duration.ofSeconds(10);

    private final ConnectionMetrics metrics;
    private final HttpServer server;

    public ConnectionMetricsEndpoint(
            HttpServer monitoredServer,
            int connectionLimit,
            InetAddress bindAddress,
            int port
    ) throws IOException {
        ConnectionMetrics createdMetrics = new ConnectionMetrics(
                monitoredServer::getActiveConnectionCount,
                connectionLimit
        );
        HttpServer createdServer = null;
        try {
            createdServer = new HttpServer(
                    "BlueMap-Webserver-Metrics",
                    createdMetrics,
                    new HttpServerSettings(
                            MAX_METRICS_CONNECTIONS,
                            METRICS_IDLE_TIMEOUT,
                            HttpRequestLimits.DEFAULT
                    )
            );
            createdServer.bind(new InetSocketAddress(bindAddress, port));
        } catch (IOException | RuntimeException ex) {
            if (createdServer != null) {
                try {
                    createdServer.close();
                } catch (IOException closeException) {
                    ex.addSuppressed(closeException);
                }
            }
            createdMetrics.close();
            throw ex;
        }

        this.metrics = createdMetrics;
        this.server = createdServer;
    }

    public void start() {
        server.start();
    }

    @Override
    public void close() throws IOException {
        IOException exception = null;
        try {
            server.close();
        } catch (IOException ex) {
            exception = ex;
        }
        metrics.close();
        if (exception != null) throw exception;
    }
}
