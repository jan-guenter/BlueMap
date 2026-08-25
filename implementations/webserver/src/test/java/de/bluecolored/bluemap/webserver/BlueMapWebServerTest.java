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
package de.bluecolored.bluemap.webserver;

import de.bluecolored.bluemap.common.web.LoggingRequestHandler;
import de.bluecolored.bluemap.common.web.http.HttpRequestHandler;
import de.bluecolored.bluemap.common.web.http.HttpResponse;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;
import de.bluecolored.bluemap.common.config.ConfigurationException;
import de.bluecolored.bluemap.core.logger.VoidLogger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse.BodyHandlers;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class BlueMapWebServerTest {

    @TempDir
    Path tempDir;

    @Test
    void parsesDefaults() {
        BlueMapWebServer.Options options = BlueMapWebServer.Options.parse(new String[0]);

        assertEquals(Path.of("config"), options.configFolder());
        assertFalse(options.verbose());
        assertNull(options.metricsPort());
        assertEquals("127.0.0.1", options.metricsIp());
        assertFalse(options.version());
        assertFalse(options.help());
    }

    @Test
    void parsesAllOptions() {
        BlueMapWebServer.Options options = BlueMapWebServer.Options.parse(new String[]{
                "--config", "/etc/bluemap",
                "--verbose",
                "--metrics-port", "9090",
                "--metrics-ip", "0.0.0.0",
                "--version",
                "--help"
        });

        assertEquals(Path.of("/etc/bluemap"), options.configFolder());
        assertTrue(options.verbose());
        assertEquals(9090, options.metricsPort());
        assertEquals("0.0.0.0", options.metricsIp());
        assertTrue(options.version());
        assertTrue(options.help());
    }

    @Test
    void rejectsUnknownAndIncompleteOptions() {
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--unknown"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--config"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--metrics-port"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--metrics-port", "0"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--metrics-port", "invalid"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--metrics-port", "65536"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--metrics-ip"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--metrics-ip", "0.0.0.0"})
        );
    }

    @Test
    void skipsRequestLoggingWhenNoLogDestinationIsConfigured() {
        HttpRequestHandler delegate = _ -> new HttpResponse(HttpStatusCode.OK);

        HttpRequestHandler withoutLogging = LoggingRequestHandler.wrap(
                delegate, "%3$s %4$s", new VoidLogger(), false
        );
        HttpRequestHandler withLogging = LoggingRequestHandler.wrap(
                delegate, "%3$s %4$s", new VoidLogger(), true
        );

        assertSame(delegate, withoutLogging);
        assertInstanceOf(LoggingRequestHandler.class, withLogging);
    }

    @Test
    void startsWithFileStorageAndAWebOnlyMap() throws Exception {
        Path config = writeFileStorageConfig(0, 256);
        Path web = tempDir.resolve("web");

        BlueMapWebServer server = BlueMapWebServer.create(config, false);
        assertFalse(server.isReady());
        try (server) {
            server.start();
            awaitReady(server);
        }
        assertFalse(server.isReady());

        assertTrue(Files.isRegularFile(web.resolve("index.html")));
        assertTrue(Files.isRegularFile(web.resolve("settings.json")));
    }

    @Test
    void servesMetricsForThePublicConnectionSemaphore() throws Exception {
        int publicPort = freePort();
        int metricsPort = freePortOtherThan(publicPort);
        Path config = writeFileStorageConfig(publicPort, 2);
        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(2))
                .build();

        BlueMapWebServer server = BlueMapWebServer.create(
                config,
                false,
                metricsPort
        );
        try (server) {
            server.start();
            awaitReady(server);

            try (Socket publicConnection = new Socket()) {
                publicConnection.connect(new InetSocketAddress(
                        InetAddress.getLoopbackAddress(),
                        publicPort
                ));
                String metrics = awaitMetric(
                        client,
                        metricsPort,
                        "bluemap_web_http_connections 1\n"
                );
                assertTrue(metrics.contains(
                        "bluemap_web_http_connections_limit 2\n"
                ));
                assertTrue(metrics.contains(
                        "bluemap_web_http_connection_utilization_ratio 0.5\n"
                ));

                java.net.http.HttpResponse<byte[]> head = client.send(
                        HttpRequest.newBuilder(metricsUri(metricsPort))
                                .method("HEAD", HttpRequest.BodyPublishers.noBody())
                                .build(),
                        BodyHandlers.ofByteArray()
                );
                assertEquals(200, head.statusCode());
                assertEquals(0, head.body().length);
            }

            awaitMetric(
                    client,
                    metricsPort,
                    "bluemap_web_http_connections 0\n"
            );
        }

        assertPortClosed(publicPort);
        assertPortClosed(metricsPort);
    }

    @Test
    void failedMetricsBindingReleasesThePublicListener() throws Exception {
        int publicPort = freePort();
        int metricsPort = freePortOtherThan(publicPort);
        Path config = writeFileStorageConfig(publicPort, 2);

        try (ServerSocket occupied = bind(metricsPort)) {
            assertThrows(
                    ConfigurationException.class,
                    () -> BlueMapWebServer.create(config, false, metricsPort)
            );
        }
        try (ServerSocket ignored = bind(publicPort)) {
            assertTrue(ignored.isBound());
        }
    }

    @Test
    void rejectsEqualPublicAndMetricsPortsWithoutLeakingTheListener()
            throws Exception {
        int port = freePort();
        Path config = writeFileStorageConfig(port, 2);

        assertThrows(
                ConfigurationException.class,
                () -> BlueMapWebServer.create(config, false, port)
        );
        try (ServerSocket ignored = bind(port)) {
            assertTrue(ignored.isBound());
        }
    }

    @Test
    void reportsRequestTasksThatIgnoreForcedShutdown() throws Exception {
        CountDownLatch running = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        executor.execute(() -> {
            running.countDown();
            while (release.getCount() != 0) {
                try {
                    release.await();
                } catch (InterruptedException ignored) {
                    // Model a request task that does not respond to interruption.
                }
            }
        });
        assertTrue(running.await(2, TimeUnit.SECONDS));

        try {
            assertFalse(BlueMapWebServer.stopConnectionExecutor(
                    executor,
                    true,
                    Duration.ofMillis(25)
            ));
        } finally {
            release.countDown();
            assertTrue(executor.awaitTermination(2, TimeUnit.SECONDS));
        }
    }

    @Test
    void usesABoundedExecutorStopWindowWhenGraceIsZero() {
        assertEquals(
                Duration.ofSeconds(5),
                BlueMapWebServer.executorStopTimeout(Duration.ZERO)
        );
        assertEquals(
                Duration.ofSeconds(9),
                BlueMapWebServer.executorStopTimeout(Duration.ofSeconds(9))
        );
    }

    private static void awaitReady(BlueMapWebServer server)
            throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        while (!server.isReady() && System.nanoTime() < deadline) {
            Thread.sleep(5);
        }
        assertTrue(server.isReady());
    }

    private Path writeFileStorageConfig(int port, int maxActiveConnections)
            throws IOException {
        Path config = Files.createDirectories(tempDir.resolve("config"));
        Files.createDirectories(config.resolve("maps"));
        Files.createDirectories(config.resolve("storages"));
        Files.createDirectories(tempDir.resolve("maps"));

        String data = unixPath(tempDir.resolve("data"));
        String web = unixPath(tempDir.resolve("web"));
        String maps = unixPath(tempDir.resolve("maps"));

        Files.writeString(config.resolve("core.conf"), """
                accept-download: false
                data: "%s"
                render-thread-count: 1
                scan-for-mod-resources: false
                log: {}
                """.formatted(data));
        Files.writeString(config.resolve("webapp.conf"), """
                enabled: true
                webroot: "%s"
                update-settings-file: true
                """.formatted(web));
        Files.writeString(config.resolve("webserver.conf"), """
                enabled: true
                webroot: "%s"
                ip: "127.0.0.1"
                port: %d
                sse-enabled: false
                max-active-connections: %d
                max-sse-connections: 1
                log: {}
                """.formatted(web, port, maxActiveConnections));
        Files.writeString(config.resolve("storages/file.conf"), """
                storage-type: file
                root: "%s"
                compression: gzip
                """.formatted(maps));
        Files.writeString(config.resolve("maps/world.conf"), """
                name: "World"
                storage: "file"
                """);
        return config;
    }

    private static String unixPath(Path path) {
        return path.toString().replace('\\', '/');
    }

    private static int freePort() throws IOException {
        try (ServerSocket socket = bind(0)) {
            return socket.getLocalPort();
        }
    }

    private static int freePortOtherThan(int excluded) throws IOException {
        int port;
        do {
            port = freePort();
        } while (port == excluded);
        return port;
    }

    private static ServerSocket bind(int port) throws IOException {
        ServerSocket socket = new ServerSocket();
        try {
            socket.bind(new InetSocketAddress(
                    InetAddress.getLoopbackAddress(),
                    port
            ));
            return socket;
        } catch (IOException ex) {
            socket.close();
            throw ex;
        }
    }

    private static URI metricsUri(int port) {
        return URI.create("http://127.0.0.1:" + port + "/metrics");
    }

    private static String awaitMetric(
            HttpClient client,
            int port,
            String expected
    ) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        String lastBody = "";
        Exception lastFailure = null;
        while (System.nanoTime() < deadline) {
            try {
                java.net.http.HttpResponse<String> response = client.send(
                        HttpRequest.newBuilder(metricsUri(port)).GET().build(),
                        BodyHandlers.ofString()
                );
                lastBody = response.body();
                if (response.statusCode() == 200 && lastBody.contains(expected)) {
                    return lastBody;
                }
            } catch (IOException ex) {
                lastFailure = ex;
            }
            Thread.sleep(5);
        }
        AssertionError failure = new AssertionError(
                "Metrics did not contain expected sample: " + expected
                        + "\nLast body:\n" + lastBody
        );
        if (lastFailure != null) failure.initCause(lastFailure);
        throw failure;
    }

    private static void assertPortClosed(int port) {
        assertThrows(IOException.class, () -> {
            try (Socket socket = new Socket()) {
                socket.connect(
                        new InetSocketAddress(
                                InetAddress.getLoopbackAddress(),
                                port
                        ),
                        250
                );
            }
        });
    }
}
