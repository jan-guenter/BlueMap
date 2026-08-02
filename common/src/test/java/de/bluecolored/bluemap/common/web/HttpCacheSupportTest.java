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
import de.bluecolored.bluemap.core.storage.CacheMetadata;
import org.junit.jupiter.api.Test;

import java.net.InetAddress;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.*;

class HttpCacheSupportTest {

    @Test
    void negotiatesOnlyTheStoredEncoding() throws Exception {
        HttpRequest request = request();
        assertTrue(HttpCacheSupport.acceptsEncoding(request, "zstd"));

        request.addHeader("Accept-Encoding", "");
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "zstd"));
        assertTrue(HttpCacheSupport.acceptsEncoding(request, "identity"));

        request.addHeader("Accept-Encoding", "gzip, deflate;q=0.5");
        assertTrue(HttpCacheSupport.acceptsEncoding(request, "gzip"));
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "zstd"));
        assertTrue(HttpCacheSupport.acceptsEncoding(request, "identity"));

        request.addHeader("Accept-Encoding", "gzip;q=0, *;q=0");
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "gzip"));
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "identity"));

        request.addHeader("Accept-Encoding", "zstd;q=1.001, *;q=0");
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "zstd"));

        request.addHeader("Accept-Encoding", "zstd;q=0, *;q=1");
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "zstd"));

        request.addHeader("Accept-Encoding", "zstd;q=0.123");
        assertTrue(HttpCacheSupport.acceptsEncoding(request, "zstd"));

        request.addHeader(
                "Accept-Encoding",
                "GZIP; level=9; Q=1.000, zstd;q=0.000, *;q=0"
        );
        assertTrue(HttpCacheSupport.acceptsEncoding(request, "gzip"));
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "zstd"));

        request.addHeader("Accept-Encoding", "zstd;q=1.0000, *;q=0");
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "zstd"));

        request.addHeader("Accept-Encoding", "zstd;q=1;q=0");
        assertFalse(HttpCacheSupport.acceptsEncoding(request, "zstd"));
    }

    @Test
    void ifNoneMatchTakesPrecedenceOverIfModifiedSince() throws Exception {
        CacheMetadata metadata = new CacheMetadata(new byte[] {0x01, 0x23}, 1_700_000_000_999L);
        String etag = HttpCacheSupport.eTag(metadata);
        assertEquals("\"0123\"", etag);

        HttpRequest request = request();
        request.addHeader("If-None-Match", "\"different\"");
        request.addHeader(
                "If-Modified-Since",
                HttpCacheSupport.lastModified(new CacheMetadata(
                        null, metadata.updatedAt() + 60_000
                ))
        );
        assertFalse(HttpCacheSupport.isNotModified(request, etag, metadata));

        request.addHeader("If-None-Match", "W/\"0123\"");
        assertTrue(HttpCacheSupport.isNotModified(request, etag, metadata));
    }

    @Test
    void formatsFileMetadataValidatorsAsWeakEntityTags() {
        CacheMetadata metadata = new CacheMetadata(
                new byte[] {0x01, 0x23},
                1_700_000_000_999L,
                true
        );

        assertEquals("W/\"0123\"", HttpCacheSupport.eTag(metadata));
    }

    @Test
    void ifNoneMatchWildcardMatchesExistingRepresentationWithoutHash() throws Exception {
        HttpRequest request = request();
        request.addHeader("If-None-Match", "*");

        assertTrue(HttpCacheSupport.isNotModified(
                request, null, new CacheMetadata(null, 0)
        ));
    }

    @Test
    void parsesWeakEntityTagListsWithoutSplittingQuotedCommas() throws Exception {
        CacheMetadata metadata = new CacheMetadata(null, 1);

        HttpRequest request = request();
        request.addHeader("If-None-Match", "\"different\", W/\"tag,with,commas\"");
        assertTrue(HttpCacheSupport.isNotModified(
                request, "\"tag,with,commas\"", metadata
        ));

        request.addHeader("If-None-Match", "\"different\", malformed");
        assertFalse(HttpCacheSupport.isNotModified(
                request, "\"tag,with,commas\"", metadata
        ));
    }

    @Test
    void fallsBackToSecondPrecisionLastModified() throws Exception {
        CacheMetadata metadata = new CacheMetadata(new byte[] {1}, 1_700_000_000_999L);
        HttpRequest request = request();
        request.addHeader("If-Modified-Since", HttpCacheSupport.lastModified(metadata));
        assertTrue(HttpCacheSupport.isNotModified(request, null, metadata));
    }

    @Test
    void formatsStrictImfFixdateAndParsesAllHttpDateFormats() {
        long expected = Instant.parse("1994-11-06T08:49:37Z").toEpochMilli();

        assertEquals(
                "Sun, 06 Nov 1994 08:49:37 GMT",
                HttpCacheSupport.lastModified(new CacheMetadata(null, expected))
        );
        assertEquals(expected, HttpCacheSupport.parseHttpDate("Sun, 06 Nov 1994 08:49:37 GMT"));
        assertEquals(expected, HttpCacheSupport.parseHttpDate("Sunday, 06-Nov-94 08:49:37 GMT"));
        assertEquals(expected, HttpCacheSupport.parseHttpDate("Sun Nov  6 08:49:37 1994"));
        assertNull(HttpCacheSupport.parseHttpDate("1994-11-06T08:49:37Z"));
    }

    private static HttpRequest request() throws Exception {
        return new HttpRequest(InetAddress.getLoopbackAddress(), "GET", "/");
    }

}
