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
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.FileTime;
import java.time.Instant;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

class FileRequestHandlerTest {

    @Test
    void servesGetAndHeadWithExactRepresentationHeaders(@TempDir Path webRoot)
            throws Exception {
        byte[] content = "<!doctype html>".getBytes(StandardCharsets.UTF_8);
        Files.write(webRoot.resolve("index.html"), content);
        FileRequestHandler handler = new FileRequestHandler(webRoot);

        try (HttpResponse get = handler.handle(request("GET", "/"))) {
            assertEquals(HttpStatusCode.OK, get.getStatusCode());
            assertArrayEquals(content, get.getBody().readAllBytes());
            assertEquals(Integer.toString(content.length), header(get, "Content-Length"));
            assertEquals("text/html; charset=utf-8", header(get, "Content-Type"));
            assertEquals("public, no-cache", header(get, "Cache-Control"));
            assertTrue(header(get, "ETag").matches("W/\"[0-9a-f]{64}\""));
            assertNotNull(header(get, "Last-Modified"));
        }

        try (HttpResponse head = handler.handle(request("HEAD", "/"))) {
            assertEquals(HttpStatusCode.OK, head.getStatusCode());
            assertNull(head.getBody());
            assertTrue(head.isBodySuppressed());
            assertEquals(Integer.toString(content.length), header(head, "Content-Length"));
            assertNotNull(header(head, "ETag"));
            assertNotNull(header(head, "Last-Modified"));
        }

        HttpResponse post = handler.handle(request("POST", "/"));
        assertEquals(HttpStatusCode.METHOD_NOT_ALLOWED, post.getStatusCode());
        assertEquals("GET, HEAD", header(post, "Allow"));
        assertEquals(
                "no-store,no-transform",
                header(post, "Cache-Control")
        );

        try (HttpResponse missing =
                     handler.handle(request("GET", "/missing.txt"))) {
            assertEquals(HttpStatusCode.NOT_FOUND, missing.getStatusCode());
            assertEquals(
                    "no-store,no-transform",
                    header(missing, "Cache-Control")
            );
        }
    }

    @Test
    void appliesConditionalPrecedenceAndReturnsValidatorsOn304(@TempDir Path webRoot)
            throws Exception {
        Files.writeString(webRoot.resolve("index.html"), "index");
        FileRequestHandler handler = new FileRequestHandler(webRoot);

        String eTag;
        String lastModified;
        try (HttpResponse initial = handler.handle(request("GET", "/"))) {
            eTag = header(initial, "ETag");
            lastModified = header(initial, "Last-Modified");
        }

        HttpRequest matching = request("GET", "/");
        matching.addHeader(
                "If-None-Match",
                "\"different\", " + eTag.substring(2)
        );
        try (HttpResponse response = handler.handle(matching)) {
            assertEquals(HttpStatusCode.NOT_MODIFIED, response.getStatusCode());
            assertEquals(eTag, header(response, "ETag"));
            assertEquals(lastModified, header(response, "Last-Modified"));
            assertEquals("public, no-cache", header(response, "Cache-Control"));
            assertNull(response.getBody());
        }

        HttpRequest wildcard = request("GET", "/");
        wildcard.addHeader("If-None-Match", "*");
        assertEquals(
                HttpStatusCode.NOT_MODIFIED,
                handler.handle(wildcard).getStatusCode()
        );

        HttpRequest precedence = request("GET", "/");
        precedence.addHeader("If-None-Match", "\"different\"");
        precedence.addHeader(
                "If-Modified-Since",
                HttpCacheSupport.formatHttpDate(System.currentTimeMillis() + 60_000)
        );
        try (HttpResponse response = handler.handle(precedence)) {
            assertEquals(HttpStatusCode.OK, response.getStatusCode());
        }
    }

    @Test
    void revalidatesStaticAssetsAndUsesCommonMimeTypes(
            @TempDir Path webRoot
    ) throws Exception {
        Path assets = Files.createDirectories(webRoot.resolve("assets"));
        Files.writeString(assets.resolve("index-Ab12_cd3.js"), "export {}");
        Files.writeString(assets.resolve("stable-name.js"), "export {}");
        Files.write(assets.resolve("module.wasm"), new byte[]{0});
        Files.writeString(webRoot.resolve("manifest.webmanifest"), "{}");
        FileRequestHandler handler = new FileRequestHandler(webRoot);

        try (HttpResponse fingerprinted =
                     handler.handle(request("GET", "/assets/index-Ab12_cd3.js"))) {
            assertEquals("public, no-cache", header(fingerprinted, "Cache-Control"));
            assertEquals(
                    "text/javascript; charset=utf-8",
                    header(fingerprinted, "Content-Type")
            );
        }
        try (HttpResponse stable =
                     handler.handle(request("GET", "/assets/stable-name.js"))) {
            assertEquals("public, no-cache", header(stable, "Cache-Control"));
        }
        try (HttpResponse wasm =
                     handler.handle(request("GET", "/assets/module.wasm"))) {
            assertEquals("application/wasm", header(wasm, "Content-Type"));
        }
        try (HttpResponse manifest =
                     handler.handle(request("GET", "/manifest.webmanifest"))) {
            assertEquals(
                    "application/manifest+json",
                    header(manifest, "Content-Type")
            );
        }
    }

    @Test
    void changesValidatorWhenAFileIsAtomicallyReplacedWithPreservedMetadata(
            @TempDir Path webRoot
    ) throws Exception {
        Path index = webRoot.resolve("index.html");
        FileTime fixedTime = FileTime.from(Instant.parse("2025-01-01T00:00:00Z"));
        Files.writeString(index, "first");
        Files.setLastModifiedTime(index, fixedTime);
        FileRequestHandler handler = new FileRequestHandler(webRoot);

        String initialETag;
        try (HttpResponse initial = handler.handle(request("GET", "/"))) {
            initialETag = header(initial, "ETag");
        }

        Path replacement = Files.writeString(
                webRoot.resolve("index.html.replacement"),
                "other"
        );
        Files.setLastModifiedTime(replacement, fixedTime);
        try {
            Files.move(
                    replacement,
                    index,
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING
            );
        } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
            Files.move(
                    replacement,
                    index,
                    StandardCopyOption.REPLACE_EXISTING
            );
        }

        HttpRequest conditional = request("GET", "/");
        conditional.addHeader("If-None-Match", initialETag);
        try (HttpResponse response = handler.handle(conditional)) {
            assertEquals(HttpStatusCode.OK, response.getStatusCode());
            assertNotEquals(initialETag, header(response, "ETag"));
            assertEquals(
                    "other",
                    new String(response.getBody().readAllBytes(), StandardCharsets.UTF_8)
            );
        }
    }

    @Test
    void rejectsTraversalAndSymlinksOutsideTheWebRoot(@TempDir Path tempDir)
            throws Exception {
        Path webRoot = Files.createDirectories(tempDir.resolve("web"));
        Path outside = Files.writeString(tempDir.resolve("outside.txt"), "private");
        FileRequestHandler handler = new FileRequestHandler(webRoot);

        assertEquals(
                HttpStatusCode.FORBIDDEN,
                handler.handle(request("GET", "/../outside.txt")).getStatusCode()
        );

        try {
            Files.createSymbolicLink(webRoot.resolve("escape.txt"), outside);
        } catch (IOException | UnsupportedOperationException e) {
            assumeTrue(false, "Symbolic links are unavailable on this filesystem: " + e);
        }

        assertEquals(
                HttpStatusCode.FORBIDDEN,
                handler.handle(request("GET", "/escape.txt")).getStatusCode()
        );
    }

    @Test
    void rejectsRequestsAfterTheConfiguredRootSymlinkIsRetargeted(
            @TempDir Path tempDir
    ) throws Exception {
        Path firstRoot = Files.createDirectories(tempDir.resolve("first"));
        Path secondRoot = Files.createDirectories(tempDir.resolve("second"));
        Files.writeString(firstRoot.resolve("index.html"), "first");
        Files.writeString(secondRoot.resolve("index.html"), "second");
        Path configuredRoot = tempDir.resolve("web");

        try {
            Files.createSymbolicLink(configuredRoot, firstRoot);
        } catch (IOException | UnsupportedOperationException e) {
            assumeTrue(false, "Symbolic links are unavailable on this filesystem: " + e);
        }

        FileRequestHandler handler = new FileRequestHandler(configuredRoot);
        try (HttpResponse initial = handler.handle(request("GET", "/"))) {
            assertEquals(HttpStatusCode.OK, initial.getStatusCode());
            assertEquals("first", new String(
                    initial.getBody().readAllBytes(), StandardCharsets.UTF_8
            ));
        }

        Files.delete(configuredRoot);
        Files.createSymbolicLink(configuredRoot, secondRoot);

        try (HttpResponse retargeted = handler.handle(request("GET", "/"))) {
            assertEquals(HttpStatusCode.FORBIDDEN, retargeted.getStatusCode());
        }
    }

    @Test
    void rejectsDirectorySwapBetweenContainmentCheckAndOpen(
            @TempDir Path tempDir
    ) throws Exception {
        Path webRoot = Files.createDirectories(tempDir.resolve("web"));
        Path directory = Files.createDirectories(webRoot.resolve("inside"));
        Files.writeString(directory.resolve("file.txt"), "public");
        Path outside = Files.createDirectories(tempDir.resolve("outside"));
        Files.writeString(outside.resolve("file.txt"), "private");
        AtomicBoolean swapped = new AtomicBoolean();

        Path probe = tempDir.resolve("symlink-probe");
        try {
            Files.createSymbolicLink(probe, outside);
            Files.delete(probe);
        } catch (IOException | UnsupportedOperationException e) {
            assumeTrue(false, "Symbolic links are unavailable on this filesystem: " + e);
        }

        FileRequestHandler handler = new FileRequestHandler(webRoot, () -> {
            if (!swapped.compareAndSet(false, true)) return;
            Files.move(directory, webRoot.resolve("displaced"));
            Files.createSymbolicLink(webRoot.resolve("inside"), outside);
        });

        HttpResponse response = handler.handle(request("GET", "/inside/file.txt"));
        assertEquals(HttpStatusCode.FORBIDDEN, response.getStatusCode());
        assertEquals("no-store,no-transform", header(response, "Cache-Control"));
        if (response.getBody() != null) {
            assertNotEquals(
                    "private",
                    new String(response.getBody().readAllBytes(), StandardCharsets.UTF_8)
            );
        }
    }

    @Test
    void rejectsWebRootSwapBetweenContainmentCheckAndOpen(
            @TempDir Path tempDir
    ) throws Exception {
        Path webRoot = Files.createDirectories(tempDir.resolve("web"));
        Files.writeString(webRoot.resolve("index.html"), "public");
        Path outside = Files.createDirectories(tempDir.resolve("outside"));
        Files.writeString(outside.resolve("index.html"), "private");
        AtomicBoolean swapped = new AtomicBoolean();

        Path probe = tempDir.resolve("symlink-probe");
        try {
            Files.createSymbolicLink(probe, outside);
            Files.delete(probe);
        } catch (IOException | UnsupportedOperationException e) {
            assumeTrue(false, "Symbolic links are unavailable on this filesystem: " + e);
        }

        FileRequestHandler handler = new FileRequestHandler(webRoot, () -> {
            if (!swapped.compareAndSet(false, true)) return;
            Files.move(webRoot, tempDir.resolve("displaced"));
            Files.createSymbolicLink(webRoot, outside);
        });

        HttpResponse response = handler.handle(request("GET", "/"));
        assertEquals(HttpStatusCode.FORBIDDEN, response.getStatusCode());
        assertEquals("no-store,no-transform", header(response, "Cache-Control"));
    }

    private static HttpRequest request(String method, String path) throws Exception {
        return new HttpRequest(InetAddress.getLoopbackAddress(), method, path);
    }

    private static String header(HttpResponse response, String name) {
        var header = response.getHeader(name);
        return header == null ? null : header.getValue();
    }

}
