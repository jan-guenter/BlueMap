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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpServerTest {

    @Test
    void closesInternallyOwnedExecutor() throws Exception {
        HttpServer server = new HttpServer(
                "test",
                ignored -> new HttpResponse(HttpStatusCode.OK)
        );

        assertFalse(server.isExecutorShutdown());
        server.close();
        assertTrue(server.isExecutorShutdown());
    }

    @Test
    void gracefulCloseDrainsAnInFlightResponse() throws Exception {
        CountDownLatch handling = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        HttpServer server = new HttpServer(
                "test",
                ignored -> {
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
                ignored -> {
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
                ignored -> new HttpResponse(HttpStatusCode.OK),
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
