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
import de.bluecolored.bluemap.core.storage.CacheMetadata;
import de.bluecolored.bluemap.core.storage.GridStorage;
import de.bluecolored.bluemap.core.storage.ItemStorage;
import de.bluecolored.bluemap.core.storage.MapStorage;
import de.bluecolored.bluemap.core.storage.StoredDataMetadata;
import de.bluecolored.bluemap.core.storage.compression.CompressedInputStream;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.util.function.DoublePredicate;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.*;

class MapStorageRequestHandlerTest {

    @Test
    void ordinaryGetUsesOneDataReadWithoutMetadataPreflight() throws Exception {
        CountingItem item = new CountingItem(Compression.ZSTD);
        MapStorageRequestHandler handler =
                new MapStorageRequestHandler(new TestMapStorage(item));
        HttpRequest request = request("GET", "/settings.json");
        request.addHeader("Accept-Encoding", "zstd");

        try (HttpResponse response = handler.handle(request)) {
            assertEquals(HttpStatusCode.OK, response.getStatusCode());
            assertEquals(1, item.reads);
            assertEquals(0, item.metadataReads);
            assertEquals("zstd", header(response, "Content-Encoding"));
            assertEquals(
                    "public,no-cache,no-transform",
                    header(response, "Cache-Control")
            );
        }
    }

    @Test
    void headAndMatchingConditionalRequestsAvoidTheDataRead() throws Exception {
        CountingItem item = new CountingItem(Compression.ZSTD);
        MapStorageRequestHandler handler =
                new MapStorageRequestHandler(new TestMapStorage(item));

        HttpRequest head = request("HEAD", "/settings.json");
        head.addHeader("Accept-Encoding", "zstd");
        try (HttpResponse response = handler.handle(head)) {
            assertEquals(HttpStatusCode.OK, response.getStatusCode());
            assertNull(response.getBody());
            assertTrue(response.isBodySuppressed());
            assertEquals(
                    Integer.toString(item.data.length),
                    header(response, "Content-Length")
            );
            assertEquals("\"0123\"", header(response, "ETag"));
            assertNotNull(header(response, "Last-Modified"));
        }
        assertEquals(0, item.reads);
        assertEquals(1, item.metadataReads);

        HttpRequest conditional = request("GET", "/settings.json");
        conditional.addHeader("Accept-Encoding", "zstd");
        conditional.addHeader("If-None-Match", "W/\"0123\"");
        try (HttpResponse response = handler.handle(conditional)) {
            assertEquals(HttpStatusCode.NOT_MODIFIED, response.getStatusCode());
            assertEquals("\"0123\"", header(response, "ETag"));
            assertNotNull(header(response, "Last-Modified"));
            assertEquals(
                    "public,no-cache,no-transform",
                    header(response, "Cache-Control")
            );
        }
        assertEquals(0, item.reads);
        assertEquals(2, item.metadataReads);
    }

    @Test
    void conditionalMissReadsDataAfterTheMetadataQuery() throws Exception {
        CountingItem item = new CountingItem(Compression.NONE);
        MapStorageRequestHandler handler =
                new MapStorageRequestHandler(new TestMapStorage(item));
        HttpRequest request = request("GET", "/settings.json");
        request.addHeader("If-None-Match", "\"different\"");

        try (HttpResponse response = handler.handle(request)) {
            assertEquals(HttpStatusCode.OK, response.getStatusCode());
        }
        assertEquals(1, item.metadataReads);
        assertEquals(1, item.reads);
    }

    @Test
    void preservesStoredEncodingAndNoTransformAcrossMapDataPolicies()
            throws Exception {
        CountingItem item = new CountingItem(Compression.ZSTD);
        TestMapStorage storage = new TestMapStorage(item);
        MapStorageRequestHandler handler = new MapStorageRequestHandler(storage);

        for (String path : new String[]{
                "/settings.json",
                "/textures.json",
                "/assets/model.bin",
                "/live/markers.json",
                "/live/players.json"
        }) {
            HttpRequest request = request("GET", path);
            request.addHeader("Accept-Encoding", "zstd");
            try (HttpResponse response = handler.handle(request)) {
                assertEquals(HttpStatusCode.OK, response.getStatusCode(), path);
                assertTrue(
                        header(response, "Cache-Control").contains("no-transform"),
                        path
                );
                assertEquals("zstd", header(response, "Content-Encoding"), path);
                if (path.endsWith("players.json")) {
                    assertEquals(
                            "private,no-store,no-transform",
                            header(response, "Cache-Control")
                    );
                }
            }
        }

        HttpRequest tile = request("GET", "/tiles/0/x1z2");
        tile.addHeader("Accept-Encoding", "zstd");
        try (HttpResponse response = handler.handle(tile)) {
            assertEquals(
                    "public,max-age=60,must-revalidate,no-transform",
                    header(response, "Cache-Control")
            );
        }

        storage.grid.missing = true;
        try (HttpResponse response = handler.handle(
                request("GET", "/tiles/0/x1z2")
        )) {
            assertEquals(HttpStatusCode.NO_CONTENT, response.getStatusCode());
            assertEquals(
                    "no-store,no-transform",
                    header(response, "Cache-Control")
            );
        }
    }

    @Test
    void unsupportedEncodingReturnsOneNoTransformErrorWithoutReadingOnHead()
            throws Exception {
        CountingItem item = new CountingItem(Compression.ZSTD);
        MapStorageRequestHandler handler =
                new MapStorageRequestHandler(new TestMapStorage(item));
        HttpRequest request = request("HEAD", "/settings.json");
        request.addHeader("Accept-Encoding", "gzip");

        try (HttpResponse response = handler.handle(request)) {
            assertEquals(HttpStatusCode.NOT_ACCEPTABLE, response.getStatusCode());
            assertEquals(
                    "no-store,no-transform",
                    header(response, "Cache-Control")
            );
            assertEquals(
                    "zstd",
                    header(response, "X-BlueMap-Required-Content-Encoding")
            );
            assertNull(response.getBody());
        }
        assertEquals(1, item.metadataReads);
        assertEquals(0, item.reads);
    }

    @Test
    void mapErrorsAreExplicitlyNonCacheable() throws Exception {
        MapStorageRequestHandler handler = new MapStorageRequestHandler(
                new TestMapStorage(new CountingItem(Compression.NONE))
        );

        try (HttpResponse missing = handler.handle(
                request("GET", "/unknown-map-path")
        )) {
            assertEquals(HttpStatusCode.NOT_FOUND, missing.getStatusCode());
            assertEquals(
                    "no-store,no-transform",
                    header(missing, "Cache-Control")
            );
        }

        try (HttpResponse method = handler.handle(
                request("POST", "/settings.json")
        )) {
            assertEquals(HttpStatusCode.METHOD_NOT_ALLOWED, method.getStatusCode());
            assertEquals("GET, HEAD", header(method, "Allow"));
            assertEquals(
                    "no-store,no-transform",
                    header(method, "Cache-Control")
            );
        }
    }

    @Test
    void dynamicPlayerRouteRemainsPrivateAndNonCacheable() throws Exception {
        TestMapStorage storage =
                new TestMapStorage(new CountingItem(Compression.NONE));
        MapRequestHandler handler = new MapRequestHandler(
                storage,
                () -> "{\"players\":[]}",
                null,
                false
        );

        try (HttpResponse response = handler.handle(
                request("GET", "/live/players.json")
        )) {
            assertEquals(HttpStatusCode.OK, response.getStatusCode());
            assertEquals(
                    "private,no-store,no-transform",
                    header(response, "Cache-Control")
            );
            assertArrayEquals(
                    "{\"players\":[]}".getBytes(),
                    response.getBody().readAllBytes()
            );
        }
    }

    @Test
    void liveEventStreamRemainsPrivateAndNonCacheable() throws Exception {
        TestMapStorage storage =
                new TestMapStorage(new CountingItem(Compression.NONE));
        MapRequestHandler handler = new MapRequestHandler(
                storage,
                () -> "{\"players\":[]}",
                () -> "{\"markers\":[]}",
                true
        );

        try (HttpResponse response = handler.handle(
                request("GET", "/live/sse")
        )) {
            assertEquals(HttpStatusCode.OK, response.getStatusCode());
            assertEquals(
                    "private,no-store,no-transform",
                    header(response, "Cache-Control")
            );
        }
    }

    @Test
    void rejectsSaturatedStorageReadsUntilTheResponseCloses() throws Exception {
        CountingItem item = new CountingItem(Compression.NONE);
        TestMapStorage storage = new TestMapStorage(item);
        storage.availableReadPermits = 1;
        MapStorageRequestHandler handler = new MapStorageRequestHandler(storage);

        HttpResponse first = handler.handle(request("GET", "/settings.json"));
        assertEquals(HttpStatusCode.OK, first.getStatusCode());
        try (HttpResponse saturated = handler.handle(
                request("GET", "/settings.json")
        )) {
            assertEquals(
                    HttpStatusCode.SERVICE_UNAVAILABLE,
                    saturated.getStatusCode()
            );
            assertEquals("1", header(saturated, "Retry-After"));
            assertEquals(
                    "no-store,no-transform",
                    header(saturated, "Cache-Control")
            );
        }

        first.close();
        try (HttpResponse recovered = handler.handle(
                request("HEAD", "/settings.json")
        )) {
            assertEquals(HttpStatusCode.OK, recovered.getStatusCode());
        }
        try (HttpResponse recoveredAgain = handler.handle(
                request("GET", "/settings.json")
        )) {
            assertEquals(HttpStatusCode.OK, recoveredAgain.getStatusCode());
        }
    }

    private static HttpRequest request(String method, String path) throws Exception {
        return new HttpRequest(InetAddress.getLoopbackAddress(), method, path);
    }

    private static String header(HttpResponse response, String name) {
        var header = response.getHeader(name);
        return header == null ? null : header.getValue();
    }

    private static final class CountingItem implements ItemStorage {

        private final byte[] data = "stored-data".getBytes();
        private final Compression compression;
        private final CacheMetadata cacheMetadata =
                new CacheMetadata(new byte[]{0x01, 0x23}, 1_700_000_000_000L);
        private int reads;
        private int metadataReads;
        private boolean missing;

        private CountingItem(Compression compression) {
            this.compression = compression;
        }

        @Override
        public OutputStream write() {
            return new ByteArrayOutputStream();
        }

        @Override
        public CompressedInputStream read() {
            reads++;
            if (missing) return null;
            return new CompressedInputStream(
                    new ByteArrayInputStream(data),
                    compression,
                    cacheMetadata
            );
        }

        @Override
        public StoredDataMetadata readMetadata() {
            metadataReads++;
            if (missing) return null;
            return new StoredDataMetadata(compression, cacheMetadata, data.length);
        }

        @Override
        public void delete() {
            missing = true;
        }

        @Override
        public boolean exists() {
            return !missing;
        }

        @Override
        public boolean isClosed() {
            return false;
        }
    }

    private static final class CountingGrid implements GridStorage {

        private final CountingItem item;
        private boolean missing;

        private CountingGrid(CountingItem item) {
            this.item = item;
        }

        @Override
        public OutputStream write(int x, int z) {
            return item.write();
        }

        @Override
        public CompressedInputStream read(int x, int z) {
            return missing ? null : item.read();
        }

        @Override
        public StoredDataMetadata readMetadata(int x, int z) {
            return missing ? null : item.readMetadata();
        }

        @Override
        public void delete(int x, int z) {
            missing = true;
        }

        @Override
        public boolean exists(int x, int z) {
            return !missing;
        }

        @Override
        public ItemStorage cell(int x, int z) {
            return item;
        }

        @Override
        public Stream<Cell> stream() {
            return Stream.empty();
        }

        @Override
        public boolean isClosed() {
            return false;
        }
    }

    private static final class TestMapStorage implements MapStorage {

        private final CountingItem item;
        private final CountingGrid grid;
        private int availableReadPermits = -1;

        private TestMapStorage(CountingItem item) {
            this.item = item;
            this.grid = new CountingGrid(item);
        }

        @Override
        public synchronized ReadPermit tryAcquireReadPermit() {
            if (availableReadPermits < 0) return ReadPermit.NOOP;
            if (availableReadPermits == 0) return null;

            availableReadPermits--;
            return new ReadPermit() {

                private boolean closed;

                @Override
                public void close() {
                    synchronized (TestMapStorage.this) {
                        if (closed) return;
                        closed = true;
                        availableReadPermits++;
                    }
                }

            };
        }

        @Override
        public GridStorage hiresTiles() {
            return grid;
        }

        @Override
        public GridStorage lowresTiles(int lod) {
            return grid;
        }

        @Override
        public GridStorage tileState() {
            return grid;
        }

        @Override
        public GridStorage chunkState() {
            return grid;
        }

        @Override
        public GridStorage regionState() {
            return grid;
        }

        @Override
        public ItemStorage asset(String name) {
            return item;
        }

        @Override
        public ItemStorage settings() {
            return item;
        }

        @Override
        public ItemStorage textures() {
            return item;
        }

        @Override
        public ItemStorage markers() {
            return item;
        }

        @Override
        public ItemStorage players() {
            return item;
        }

        @Override
        public void delete(DoublePredicate onProgress) {
            item.delete();
            grid.missing = true;
        }

        @Override
        public boolean exists() {
            return item.exists();
        }

        @Override
        public boolean isClosed() {
            return false;
        }
    }

}
