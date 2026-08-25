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

import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpServerTest {

    @Test
    void closesInternallyOwnedExecutor() throws Exception {
        HttpServer server = new HttpServer(
                "test",
                _ -> new HttpResponse(HttpStatusCode.OK)
        );

        assertFalse(server.isExecutorShutdown());
        server.close();
        assertTrue(server.isExecutorShutdown());
    }

    @Test
    void ownedServerWaitsForRequestTasksBeforeCloseReturns() throws Exception {
        CountDownLatch handling = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    handling.countDown();
                    while (release.getCount() != 0) {
                        try {
                            release.await();
                        } catch (InterruptedException ignored) {
                            // Simulate a request task that needs an external
                            // resource to finish despite cancellation.
                        }
                    }
                    return new HttpResponse(HttpStatusCode.OK);
                }
        );

        try (SocketPair sockets = socketPair()) {
            sendRequest(sockets.client());
            server.handleConnection(sockets.server());
            assertTrue(handling.await(2, TimeUnit.SECONDS));

            CompletableFuture<Void> closing = CompletableFuture.runAsync(() -> {
                try {
                    server.close();
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
            Thread.sleep(25);
            assertFalse(closing.isDone());

            release.countDown();
            closing.get(2, TimeUnit.SECONDS);
            assertEquals(0, server.getActiveConnectionCount());
        } finally {
            release.countDown();
            server.close();
        }
    }

    @Test
    void gracefulCloseDrainsAnInFlightResponse() throws Exception {
        CountDownLatch handling = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    handling.countDown();
                    await(release);
                    HttpResponse response =
                            new HttpResponse(HttpStatusCode.OK);
                    response.addHeader("Content-Length", "2");
                    response.setBody("ok");
                    return response;
                },
                executor
        );

        try (SocketPair sockets = socketPair()) {
            sendRequest(sockets.client());
            server.handleConnection(sockets.server());
            assertTrue(handling.await(2, TimeUnit.SECONDS));

            CompletableFuture<Boolean> closing =
                    CompletableFuture.supplyAsync(() -> {
                        try {
                            return server.closeGracefully(
                                    Duration.ofSeconds(2)
                            );
                        } catch (Exception e) {
                            throw new RuntimeException(e);
                        }
                    });
            assertFalse(closing.isDone());

            release.countDown();
            assertTrue(closing.get(2, TimeUnit.SECONDS));
            String wire = new String(
                    sockets.client().socket().getInputStream().readAllBytes(),
                    StandardCharsets.UTF_8
            );
            assertTrue(wire.endsWith("\r\n\r\nok"));
            assertEquals(0, server.getActiveConnectionCount());
        } finally {
            release.countDown();
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void gracefulCloseForceClosesAfterTheBoundedTimeout() throws Exception {
        CountDownLatch handling = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    handling.countDown();
                    await(release);
                    return new HttpResponse(HttpStatusCode.OK);
                },
                executor
        );

        try (SocketPair sockets = socketPair()) {
            sendRequest(sockets.client());
            server.handleConnection(sockets.server());
            assertTrue(handling.await(2, TimeUnit.SECONDS));

            assertFalse(server.closeGracefully(Duration.ofMillis(25)));
            assertFalse(sockets.server().isOpen());
        } finally {
            release.countDown();
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void rejectsConnectionsThatRaceWithShutdown() throws Exception {
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> new HttpResponse(HttpStatusCode.OK),
                executor
        );

        try (SocketPair sockets = socketPair()) {
            assertTrue(server.closeGracefully(Duration.ZERO));
            server.handleConnection(sockets.server());
            assertFalse(sockets.server().isOpen());
            assertEquals(0, server.getActiveConnectionCount());
        } finally {
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void closesARequestAtTheAbsoluteDeadlineEvenWhileBytesArrive() throws Exception {
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> new HttpResponse(HttpStatusCode.OK),
                executor,
                settingsWithTimeout(Duration.ofMillis(75))
        );

        try (SocketPair sockets = socketPair()) {
            server.handleConnection(sockets.server());
            AtomicBoolean writing = new AtomicBoolean(true);
            AtomicInteger writes = new AtomicInteger();
            CompletableFuture<Void> trickle = CompletableFuture.runAsync(() -> {
                while (writing.get()) {
                    try {
                        sockets.client().write(ByteBuffer.wrap(new byte[]{'G'}));
                        writes.incrementAndGet();
                        Thread.sleep(20);
                    } catch (Exception ignored) {
                        return;
                    }
                }
            });

            long started = System.nanoTime();
            awaitNoActiveConnections(server);
            long elapsedMillis = Duration.ofNanos(
                    System.nanoTime() - started
            ).toMillis();
            writing.set(false);
            trickle.get(2, TimeUnit.SECONDS);

            assertTrue(writes.get() >= 3);
            assertTrue(elapsedMillis < 500, "elapsed: " + elapsedMillis);
            assertFalse(sockets.server().isOpen());
        } finally {
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void closesAResponseProducerThatStopsBeforeItsFirstWrite()
            throws Exception {
        CountDownLatch streaming = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    HttpResponse response = new HttpResponse(HttpStatusCode.OK);
                    response.setBody(out -> {
                        streaming.countDown();
                        try {
                            new CountDownLatch(1).await();
                        } catch (InterruptedException ex) {
                            Thread.currentThread().interrupt();
                        }
                    });
                    return response;
                },
                executor,
                settingsWithTimeout(Duration.ofMillis(75))
        );

        try (SocketPair sockets = socketPair()) {
            sendRequest(sockets.client());
            server.handleConnection(sockets.server());
            assertTrue(streaming.await(2, TimeUnit.SECONDS));

            awaitNoActiveConnections(server);
            assertFalse(sockets.server().isOpen());
        } finally {
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void emptyFlushesDoNotCountAsResponseProgress() throws Exception {
        CountDownLatch streaming = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    HttpResponse response = new HttpResponse(HttpStatusCode.OK);
                    response.setBody(out -> {
                        streaming.countDown();
                        while (!Thread.currentThread().isInterrupted()) {
                            out.flush();
                            try {
                                Thread.sleep(20);
                            } catch (InterruptedException ex) {
                                Thread.currentThread().interrupt();
                            }
                        }
                    });
                    return response;
                },
                executor,
                settingsWithTimeout(Duration.ofMillis(75))
        );

        try (SocketPair sockets = socketPair()) {
            sendRequest(sockets.client());
            server.handleConnection(sockets.server());
            assertTrue(streaming.await(2, TimeUnit.SECONDS));

            awaitNoActiveConnections(server);
            assertFalse(sockets.server().isOpen());
        } finally {
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void actualPeriodicWritesKeepTheResponseAlive() throws Exception {
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    HttpResponse response = new HttpResponse(HttpStatusCode.OK);
                    response.setBody(out -> {
                        for (int i = 0; i < 6; i++) {
                            out.write('x');
                            out.flush();
                            try {
                                Thread.sleep(40);
                            } catch (InterruptedException ex) {
                                Thread.currentThread().interrupt();
                                return;
                            }
                        }
                    });
                    return response;
                },
                executor,
                settingsWithTimeout(Duration.ofMillis(75))
        );

        try (SocketPair sockets = socketPair()) {
            sendRequest(sockets.client());
            server.handleConnection(sockets.server());
            String wire = new String(
                    sockets.client().socket().getInputStream().readAllBytes(),
                    StandardCharsets.UTF_8
            );

            assertEquals(6, occurrences(wire, "1\r\nx\r\n"));
            awaitNoActiveConnections(server);
        } finally {
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void capsLongLivedResponsesBelowTheGlobalConnectionLimit()
            throws Exception {
        CountDownLatch streaming = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    HttpResponse response = new HttpResponse(HttpStatusCode.OK);
                    response.setLongLived(true);
                    response.setBody(out -> {
                        streaming.countDown();
                        try {
                            release.await();
                        } catch (InterruptedException ex) {
                            Thread.currentThread().interrupt();
                        }
                    });
                    return response;
                },
                executor,
                new HttpServerSettings(
                        2,
                        1,
                        Duration.ofSeconds(2),
                        HttpRequestLimits.DEFAULT
                )
        );

        try (SocketPair first = socketPair();
             SocketPair second = socketPair()) {
            sendRequest(first.client());
            server.handleConnection(first.server());
            assertTrue(streaming.await(2, TimeUnit.SECONDS));

            sendRequest(second.client());
            server.handleConnection(second.server());
            String wire = new String(
                    second.client().socket().getInputStream().readAllBytes(),
                    StandardCharsets.UTF_8
            );
            assertTrue(wire.startsWith(
                    "HTTP/1.1 503 Service Unavailable\r\n"
            ));
            assertTrue(wire.contains("Retry-After: 5\r\n"));

            release.countDown();
            awaitNoActiveConnections(server);
            try (SocketPair third = socketPair()) {
                sendRequest(third.client());
                server.handleConnection(third.server());
                String recovered = new String(
                        third.client().socket().getInputStream().readAllBytes(),
                        StandardCharsets.UTF_8
                );
                assertTrue(recovered.startsWith("HTTP/1.1 200 OK\r\n"));
            }
        } finally {
            release.countDown();
            server.close();
            executor.shutdownNow();
        }
    }

    @Test
    void closesAResponseWhenTheClientStopsReading() throws Exception {
        CountDownLatch streaming = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                _ -> {
                    HttpResponse response = new HttpResponse(HttpStatusCode.OK);
                    response.setBody(out -> {
                        streaming.countDown();
                        byte[] block = new byte[64 * 1024];
                        while (true) out.write(block);
                    });
                    return response;
                },
                executor,
                settingsWithTimeout(Duration.ofMillis(75))
        );

        try (SocketPair sockets = socketPair()) {
            sockets.server().socket().setSendBufferSize(1024);
            sendRequest(sockets.client());
            server.handleConnection(sockets.server());
            assertTrue(streaming.await(2, TimeUnit.SECONDS));

            awaitNoActiveConnections(server);
            assertFalse(sockets.server().isOpen());
        } finally {
            server.close();
            executor.shutdownNow();
        }
    }

    private static void await(CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static void sendRequest(SocketChannel client) throws Exception {
        client.write(ByteBuffer.wrap(
                "GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
                        .getBytes(StandardCharsets.UTF_8)
        ));
        client.shutdownOutput();
    }

    private static HttpServerSettings settingsWithTimeout(Duration timeout) {
        return new HttpServerSettings(
                1,
                timeout,
                HttpRequestLimits.DEFAULT
        );
    }

    private static void awaitNoActiveConnections(HttpServer server)
            throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(3).toNanos();
        while (server.getActiveConnectionCount() != 0
                && System.nanoTime() < deadline) {
            Thread.sleep(5);
        }
        assertEquals(0, server.getActiveConnectionCount());
    }

    private static int occurrences(String value, String token) {
        int count = 0;
        int offset = 0;
        while ((offset = value.indexOf(token, offset)) >= 0) {
            count++;
            offset += token.length();
        }
        return count;
    }

    private static SocketPair socketPair() throws Exception {
        try (ServerSocketChannel listener = ServerSocketChannel.open()) {
            listener.bind(new InetSocketAddress("127.0.0.1", 0));
            SocketChannel client =
                    SocketChannel.open(listener.getLocalAddress());
            return new SocketPair(client, listener.accept());
        }
    }

    private record SocketPair(
            SocketChannel client,
            SocketChannel server
    ) implements AutoCloseable {

        @Override
        public void close() throws Exception {
            client.close();
            server.close();
        }

    }

}
