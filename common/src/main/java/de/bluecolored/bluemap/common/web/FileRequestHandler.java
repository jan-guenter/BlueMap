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

import de.bluecolored.bluemap.common.web.http.*;
import de.bluecolored.bluemap.core.logger.Logger;
import de.bluecolored.bluemap.core.storage.CacheMetadata;
import lombok.Getter;
import lombok.NonNull;

import java.io.FileNotFoundException;
import java.io.IOException;
import java.nio.channels.Channels;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.InvalidPathException;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Locale;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

@Getter
public class FileRequestHandler implements HttpRequestHandler {

    private static final long IMMUTABLE_MAX_AGE_SECONDS = TimeUnit.DAYS.toSeconds(365);
    private static final Pattern VITE_FINGERPRINTED_ASSET = Pattern.compile(
            "^assets/(?:.*/)?[^/]+-[A-Za-z0-9_-]{8}\\.[^/]+$"
    );

    private @NonNull Path webRoot;

    public FileRequestHandler(Path webRoot) {
        setWebRoot(webRoot);
    }

    public void setWebRoot(@NonNull Path webRoot) {
        this.webRoot = webRoot.toAbsolutePath().normalize();
    }

    @Override
    public HttpResponse handle(HttpRequest request) {
        boolean head = request.getMethod().equalsIgnoreCase("HEAD");
        if (!head && !request.getMethod().equalsIgnoreCase("GET")) {
            HttpResponse response = new HttpResponse(HttpStatusCode.METHOD_NOT_ALLOWED);
            response.addHeader("Allow", "GET, HEAD");
            return applyErrorCachePolicy(response);
        }

        try {
            return applyErrorCachePolicy(generateResponse(request, head));
        } catch (IOException e) {
            Logger.global.logError("Failed to serve file", e);
            return applyErrorCachePolicy(
                    new HttpResponse(HttpStatusCode.INTERNAL_SERVER_ERROR)
            );
        }
    }

    private static HttpResponse applyErrorCachePolicy(HttpResponse response) {
        if (response.getStatusCode().getCode() >= 400
                && response.getHeader("Cache-Control") == null) {
            response.addHeader("Cache-Control", "no-store,no-transform");
        }
        return response;
    }

    private HttpResponse generateResponse(HttpRequest request, boolean head) throws IOException {
        String requestPath = request.getPath();
        if (requestPath == null) return new HttpResponse(HttpStatusCode.BAD_REQUEST);

        // normalize path
        String path = requestPath;
        if (path.startsWith("/")) path = path.substring(1);
        if (path.endsWith("/")) path = path.substring(0, path.length() - 1);

        Path filePath;
        try {
            filePath = webRoot.resolve(path);
        } catch (InvalidPathException e){
            return new HttpResponse(HttpStatusCode.NOT_FOUND);
        }

        // check if file is in web-root
        filePath = filePath.normalize();
        if (!filePath.startsWith(webRoot)){
            return new HttpResponse(HttpStatusCode.FORBIDDEN);
        }

        Path realWebRoot;
        try {
            realWebRoot = webRoot.toRealPath();
        } catch (FileNotFoundException | NoSuchFileException e) {
            return new HttpResponse(HttpStatusCode.NOT_FOUND);
        }

        // redirect to have correct relative paths
        boolean directory = Files.isDirectory(filePath);
        if (directory && !isContained(filePath, realWebRoot)) {
            return new HttpResponse(HttpStatusCode.FORBIDDEN);
        }
        if (directory && !requestPath.endsWith("/")) {
            HttpResponse response = new HttpResponse(HttpStatusCode.SEE_OTHER);
            response.addHeader("Location", "/" + path + "/" + (request.getRawQueryString().isEmpty() ? "" : "?" + request.getRawQueryString()));
            return response;
        }

        // default to index.html
        if (!Files.exists(filePath) || directory){
            filePath = filePath.resolve("index.html");
        }

        if (!Files.exists(filePath) || Files.isDirectory(filePath)){
            return new HttpResponse(HttpStatusCode.NOT_FOUND);
        }

        // don't send php files
        if (filePath.getFileName().toString().toLowerCase(Locale.ROOT).endsWith(".php")) {
            return new HttpResponse(HttpStatusCode.FORBIDDEN);
        }

        Path realFilePath;
        try {
            realFilePath = filePath.toRealPath();
        } catch (FileNotFoundException | NoSuchFileException e) {
            return new HttpResponse(HttpStatusCode.NOT_FOUND);
        }
        if (!realFilePath.startsWith(realWebRoot)
                || realFilePath.getFileName().toString().toLowerCase(Locale.ROOT).endsWith(".php")) {
            return new HttpResponse(HttpStatusCode.FORBIDDEN);
        }

        OpenedFile openedFile;
        try {
            openedFile = openStableFile(realFilePath);
        } catch (FileNotFoundException | NoSuchFileException e) {
            return new HttpResponse(HttpStatusCode.NOT_FOUND);
        }

        boolean responseOwnsFile = false;
        try {
            BasicFileAttributes attributes = openedFile.attributes();
            if (!attributes.isRegularFile()) {
                return new HttpResponse(HttpStatusCode.FORBIDDEN);
            }

            long contentLength = openedFile.channel().size();
            long lastModified = attributes.lastModifiedTime().toMillis();
            String eTag = weakFileETag(contentLength, lastModified);
            String relativePath =
                    webRoot.relativize(filePath).toString().replace('\\', '/');
            String cacheControl = cacheControl(relativePath);
            String contentType = toContentType(filePath.getFileName().toString());
            CacheMetadata cacheMetadata = new CacheMetadata(null, lastModified);

            if (HttpCacheSupport.isNotModified(request, eTag, cacheMetadata)) {
                HttpResponse response =
                        new HttpResponse(HttpStatusCode.NOT_MODIFIED);
                addFileHeaders(
                        response, eTag, lastModified, cacheControl, contentType,
                        contentLength, false
                );
                return response;
            }

            HttpResponse response = new HttpResponse(HttpStatusCode.OK);
            addFileHeaders(
                    response, eTag, lastModified, cacheControl, contentType,
                    contentLength, true
            );
            if (head) {
                response.setBodySuppressed(true);
                return response;
            }

            response.setBody(Channels.newInputStream(openedFile.channel()));
            responseOwnsFile = true;
            return response;
        } finally {
            if (!responseOwnsFile) {
                openedFile.close();
            }
        }
    }

    /**
     * Opens a file and verifies that the metadata used for the response belongs to the opened
     * handle. This avoids combining a content length from one file version with a body from
     * another when a file is replaced while it is being served.
     */
    private static OpenedFile openStableFile(Path path) throws IOException {
        IOException lastFailure = null;

        for (int attempt = 0; attempt < 3; attempt++) {
            BasicFileAttributes before =
                    Files.readAttributes(path, BasicFileAttributes.class);
            FileChannel channel = null;

            try {
                channel = FileChannel.open(path, StandardOpenOption.READ);
                BasicFileAttributes after =
                        Files.readAttributes(path, BasicFileAttributes.class);

                if (sameFileVersion(before, after) && channel.size() == after.size()) {
                    return new OpenedFile(channel, after);
                }

                lastFailure = new IOException("File changed while it was being opened: " + path);
            } catch (IOException e) {
                lastFailure = e;
            }

            if (channel != null) {
                channel.close();
            }
        }

        throw Objects.requireNonNullElseGet(
                lastFailure,
                () -> new IOException("Failed to open file: " + path)
        );
    }

    private static boolean sameFileVersion(
            BasicFileAttributes before,
            BasicFileAttributes after
    ) {
        Object beforeKey = before.fileKey();
        Object afterKey = after.fileKey();
        if ((beforeKey != null || afterKey != null) && !Objects.equals(beforeKey, afterKey)) {
            return false;
        }

        return before.isRegularFile() == after.isRegularFile()
                && before.size() == after.size()
                && before.lastModifiedTime().equals(after.lastModifiedTime());
    }

    private static boolean isContained(Path path, Path realWebRoot) throws IOException {
        try {
            return path.toRealPath().startsWith(realWebRoot);
        } catch (FileNotFoundException | NoSuchFileException e) {
            return false;
        }
    }

    private static String weakFileETag(long contentLength, long lastModified) {
        return "W/\"" + Long.toHexString(contentLength)
                + "-" + Long.toHexString(lastModified) + "\"";
    }

    private static String cacheControl(String path) {
        if (VITE_FINGERPRINTED_ASSET.matcher(path).matches()) {
            return "public, max-age=" + IMMUTABLE_MAX_AGE_SECONDS + ", immutable";
        }
        return "public, no-cache";
    }

    private static void addFileHeaders(
            HttpResponse response,
            String eTag,
            long lastModified,
            String cacheControl,
            String contentType,
            long contentLength,
            boolean includeContentLength
    ) {
        response.addHeader("ETag", eTag);
        if (lastModified > 0) {
            response.addHeader("Last-Modified", HttpCacheSupport.formatHttpDate(lastModified));
        }
        response.addHeader("Cache-Control", cacheControl);
        response.addHeader("Content-Type", contentType);
        if (includeContentLength) {
            response.addHeader("Content-Length", Long.toString(contentLength));
        }
    }

    private static String toContentType(String fileName) {
        int pointIndex = fileName.lastIndexOf('.');
        String fileEnding = pointIndex < 0
                ? ""
                : fileName.substring(pointIndex + 1).toLowerCase(Locale.ROOT);
        return switch (fileEnding) {
            case "avif" -> "image/avif";
            case "gif" -> "image/gif";
            case "ico" -> "image/x-icon";
            case "png" -> "image/png";
            case "jpg",
                 "jpeg",
                 "jpe" -> "image/jpeg";
            case "svg" -> "image/svg+xml";
            case "webp" -> "image/webp";
            case "css" -> "text/css; charset=utf-8";
            case "js",
                 "mjs" -> "text/javascript; charset=utf-8";
            case "html",
                 "htm",
                 "shtml" -> "text/html; charset=utf-8";
            case "json",
                 "map" -> "application/json; charset=utf-8";
            case "webmanifest" -> "application/manifest+json";
            case "txt" -> "text/plain; charset=utf-8";
            case "xml" -> "application/xml";
            case "wasm" -> "application/wasm";
            case "woff" -> "font/woff";
            case "woff2" -> "font/woff2";
            case "ttf" -> "font/ttf";
            case "otf" -> "font/otf";
            case "eot" -> "application/vnd.ms-fontobject";
            case "pdf" -> "application/pdf";
            case "zip" -> "application/zip";
            default -> "application/octet-stream";
        };
    }

    private record OpenedFile(
            FileChannel channel,
            BasicFileAttributes attributes
    ) implements AutoCloseable {

        @Override
        public void close() throws IOException {
            channel.close();
        }

    }

}
