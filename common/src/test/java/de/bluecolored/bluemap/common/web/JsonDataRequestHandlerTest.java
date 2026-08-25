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
package de.bluecolored.bluemap.common.web;

import de.bluecolored.bluemap.common.web.http.HttpRequest;
import de.bluecolored.bluemap.common.web.http.HttpResponse;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;
import org.junit.jupiter.api.Test;

import java.net.InetAddress;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class JsonDataRequestHandlerTest {

    @Test
    void appliesConfiguredPolicyAndSupportsGetHeadOnly() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        JsonDataRequestHandler handler = new JsonDataRequestHandler(
                () -> {
                    calls.incrementAndGet();
                    return "{}";
                },
                "private,no-store,no-transform"
        );

        try (HttpResponse get = handler.handle(request("GET"))) {
            assertEquals(HttpStatusCode.OK, get.getStatusCode());
            assertEquals(
                    "private,no-store,no-transform",
                    header(get, "Cache-Control")
            );
            assertArrayEquals("{}".getBytes(), get.getBody().readAllBytes());
        }
        assertEquals(1, calls.get());

        try (HttpResponse head = handler.handle(request("HEAD"))) {
            assertEquals(HttpStatusCode.OK, head.getStatusCode());
            assertTrue(head.isBodySuppressed());
            assertNull(head.getBody());
        }
        assertEquals(1, calls.get());

        try (HttpResponse post = handler.handle(request("POST"))) {
            assertEquals(HttpStatusCode.METHOD_NOT_ALLOWED, post.getStatusCode());
            assertEquals("GET, HEAD", header(post, "Allow"));
            assertEquals(
                    "no-store,no-transform",
                    header(post, "Cache-Control")
            );
        }
        assertEquals(1, calls.get());
    }

    private static HttpRequest request(String method) throws Exception {
        return new HttpRequest(
                InetAddress.getLoopbackAddress(),
                method,
                "/live/players.json"
        );
    }

    private static String header(HttpResponse response, String name) {
        var header = response.getHeader(name);
        return header == null ? null : header.getValue();
    }

}
