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

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class HttpRequestInputStreamTest {

    @Test
    void parsesRequestWithinLimits() throws Exception {
        HttpRequest request = parse(
                "GET /health/ready HTTP/1.1\r\nHost: example.test\r\n\r\n",
                new HttpRequestLimits(128, 4, 128, 0)
        );

        assertEquals("GET", request.getMethod());
        assertEquals("/health/ready", request.getPath());
        assertEquals("example.test", request.getHeader("Host").getValue());
    }

    @Test
    void rejectsOversizedRequestLineAndHeaders() {
        assertThrows(
                IOException.class,
                () -> parse(
                        "GET /a-path-that-is-too-long HTTP/1.1\r\n\r\n",
                        new HttpRequestLimits(16, 4, 128, 0)
                )
        );
        assertThrows(
                IOException.class,
                () -> parse(
                        "GET / HTTP/1.1\r\nOne: 1\r\nTwo: 2\r\n\r\n",
                        new HttpRequestLimits(128, 1, 128, 0)
                )
        );
        assertThrows(
                IOException.class,
                () -> parse(
                        "GET / HTTP/1.1\r\nLarge: abcdefghijklmnop\r\n\r\n",
                        new HttpRequestLimits(128, 4, 12, 0)
                )
        );
    }

    @Test
    void rejectsOversizedBodyBeforeAllocatingIt() {
        assertThrows(
                IOException.class,
                () -> parse(
                        "POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\n12345",
                        new HttpRequestLimits(128, 4, 128, 4)
                )
        );
    }

    @Test
    void parsesContentLengthBodyWithoutConsumingTheNextRequest() throws Exception {
        String input = """
                POST /first HTTP/1.1\r
                Host: example.test\r
                Content-Length: 5\r
                \r
                helloGET /second HTTP/1.1\r
                Host: example.test\r
                \r
                """;

        try (HttpRequestInputStream requestInput = stream(input, new HttpRequestLimits(128, 8, 256, 16))) {
            HttpRequest first = requestInput.read();
            assertEquals("/first", first.getPath());
            assertArrayEquals("hello".getBytes(StandardCharsets.UTF_8), first.getBody());

            HttpRequest second = requestInput.read();
            assertEquals("/second", second.getPath());
            assertEquals(0, second.getBody().length);
        }
    }

    @Test
    void parsesChunkExtensionsAndTrailersWithoutConsumingTheNextRequest() throws Exception {
        String input = """
                POST /first HTTP/1.1\r
                Host: example.test\r
                Transfer-Encoding: chunked\r
                \r
                4;source=test\r
                Wiki\r
                5\r
                pedia\r
                0\r
                Checksum: ignored\r
                \r
                GET /second HTTP/1.1\r
                Host: example.test\r
                \r
                """;

        try (HttpRequestInputStream requestInput = stream(input, new HttpRequestLimits(128, 8, 256, 16))) {
            HttpRequest first = requestInput.read();
            assertArrayEquals("Wikipedia".getBytes(StandardCharsets.UTF_8), first.getBody());
            assertEquals("/second", requestInput.read().getPath());
        }
    }

    @Test
    void rejectsTruncatedFixedAndChunkedBodies() {
        HttpRequestLimits limits = new HttpRequestLimits(128, 8, 256, 16);

        assertThrows(
                IOException.class,
                () -> parse(
                        "POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\n1234",
                        limits
                )
        );
        assertThrows(
                IOException.class,
                () -> parse(
                        "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n1234",
                        limits
                )
        );
    }

    @Test
    void rejectsCumulativeChunkExtensionFramingOverTheHeaderBudget() {
        String input = """
                POST / HTTP/1.1\r
                Transfer-Encoding: chunked\r
                \r
                1;extension=0123456789\r
                a\r
                1;extension=0123456789\r
                b\r
                0\r
                \r
                """;

        assertThrows(
                IOException.class,
                () -> parse(
                        input,
                        new HttpRequestLimits(128, 8, 64, 16)
                )
        );
    }

    @Test
    void rejectsExcessiveTinyChunkCounts() {
        StringBuilder input = new StringBuilder("""
                POST / HTTP/1.1\r
                Transfer-Encoding: chunked\r
                \r
                """);
        for (int i = 0; i < 1025; i++) {
            input.append("1\r\na\r\n");
        }
        input.append("0\r\n\r\n");

        assertThrows(
                IOException.class,
                () -> parse(
                        input.toString(),
                        new HttpRequestLimits(128, 8, 16 * 1024, 2048)
                )
        );
    }

    private static HttpRequest parse(String request, HttpRequestLimits limits) throws Exception {
        try (HttpRequestInputStream input = stream(request, limits)) {
            return input.read();
        }
    }

    private static HttpRequestInputStream stream(String request, HttpRequestLimits limits) throws Exception {
        return new HttpRequestInputStream(
                new ByteArrayInputStream(request.getBytes(StandardCharsets.UTF_8)),
                InetAddress.getLoopbackAddress(),
                limits
        );
    }

}
