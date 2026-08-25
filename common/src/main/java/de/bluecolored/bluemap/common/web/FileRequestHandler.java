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
import java.nio.channels.SeekableByteChannel;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.InvalidPathException;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.OpenOption;
import java.nio.file.Path;
import java.nio.file.SecureDirectoryStream;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributeView;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Objects;
import java.util.Set;

public class FileRequestHandler implements HttpRequestHandler {

    private static final Set<OpenOption> READ_NOFOLLOW = Set.of(
            StandardOpenOption.READ,
            LinkOption.NOFOLLOW_LINKS
    );

    @Getter
    private volatile @NonNull Path webRoot;
    private volatile Path realWebRoot;
    private final OpenHook beforeOpen;

    public FileRequestHandler(Path webRoot) {
        this(webRoot, () -> {});
    }

    FileRequestHandler(Path webRoot, OpenHook beforeOpen) {
        this.beforeOpen = Objects.requireNonNull(beforeOpen, "beforeOpen");
        setWebRoot(webRoot);
    }

    public void setWebRoot(@NonNull Path webRoot) {
        this.webRoot = webRoot.toAbsolutePath().normalize();
        this.realWebRoot = null;
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
            realWebRoot = realWebRoot();
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
            beforeOpen.run();
            openedFile = openContainedFile(
                    realWebRoot,
                    realWebRoot.relativize(realFilePath)
            );
        } catch (OutsideRootException e) {
            return new HttpResponse(HttpStatusCode.FORBIDDEN);
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
            String eTag = weakFileETag(attributes);
            String cacheControl = "public, no-cache";
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
     * Opens a file relative to its already-canonicalized web root. File-system
     * providers supporting {@link SecureDirectoryStream} anchor traversal to
     * an open web-root handle and use no-follow child handles, so renaming a
     * checked directory and replacing it with an outside-root symlink cannot
     * redirect the subsequent open. Other providers use a fail-closed
     * canonical-path recheck around the open.
     */
    private static OpenedFile openContainedFile(
            Path realRoot,
            Path relativePath
    ) throws IOException {
        Path normalized = relativePath.normalize();
        if (normalized.isAbsolute() || normalized.startsWith("..")) {
            throw new OutsideRootException();
        }
        if (!realRoot.equals(realRoot.toRealPath())) {
            throw new OutsideRootException();
        }

        DirectoryStream<Path> rootDirectory = Files.newDirectoryStream(realRoot);
        try {
            if (rootDirectory instanceof SecureDirectoryStream<Path> secureRoot) {
                OpenedFile openedFile = openSecurely(secureRoot, normalized);
                try {
                    rootDirectory.close();
                } catch (IOException e) {
                    try {
                        openedFile.close();
                    } catch (IOException closeFailure) {
                        e.addSuppressed(closeFailure);
                    }
                    throw e;
                }
                return openedFile;
            }
        } catch (IOException | RuntimeException e) {
            try {
                rootDirectory.close();
            } catch (IOException closeFailure) {
                e.addSuppressed(closeFailure);
            }
            throw e;
        }
        rootDirectory.close();

        return openWithCanonicalRecheck(realRoot, normalized);
    }

    private static OpenedFile openSecurely(
            SecureDirectoryStream<Path> webRoot,
            Path relativePath
    ) throws IOException {
        List<SecureDirectoryStream<Path>> openedDirectories = new ArrayList<>();
        SecureDirectoryStream<Path> directory = webRoot;
        OpenedFile openedFile;
        try {
            int fileIndex = relativePath.getNameCount() - 1;
            if (fileIndex < 0) {
                throw new IOException("Cannot serve the web-root directory");
            }
            for (int index = 0; index < fileIndex; index++) {
                directory = openDirectory(
                        directory,
                        relativePath.getName(index)
                );
                openedDirectories.add(directory);
            }

            openedFile = openStableFile(
                    directory,
                    relativePath.getName(fileIndex)
            );
        } catch (IOException | RuntimeException e) {
            IOException closeFailure = closeDirectories(openedDirectories);
            if (closeFailure != null) e.addSuppressed(closeFailure);
            throw e;
        }

        IOException closeFailure = closeDirectories(openedDirectories);
        if (closeFailure != null) {
            try {
                openedFile.close();
            } catch (IOException openedFileCloseFailure) {
                closeFailure.addSuppressed(openedFileCloseFailure);
            }
            throw closeFailure;
        }
        return openedFile;
    }

    private static IOException closeDirectories(
            List<SecureDirectoryStream<Path>> directories
    ) {
        IOException failure = null;
        for (int index = directories.size() - 1; index >= 0; index--) {
            try {
                directories.get(index).close();
            } catch (IOException e) {
                if (failure == null) failure = e;
                else failure.addSuppressed(e);
            }
        }
        return failure;
    }

    private static SecureDirectoryStream<Path> openDirectory(
            SecureDirectoryStream<Path> parent,
            Path name
    ) throws IOException {
        BasicFileAttributeView view = parent.getFileAttributeView(
                name,
                BasicFileAttributeView.class,
                LinkOption.NOFOLLOW_LINKS
        );
        if (view == null) {
            throw new IOException("Basic file attributes are unavailable");
        }
        BasicFileAttributes attributes = view.readAttributes();
        if (!attributes.isDirectory() || attributes.isSymbolicLink()) {
            throw new OutsideRootException();
        }
        return parent.newDirectoryStream(name, LinkOption.NOFOLLOW_LINKS);
    }

    private static OpenedFile openStableFile(
            SecureDirectoryStream<Path> directory,
            Path fileName
    ) throws IOException {
        IOException lastFailure = null;

        for (int attempt = 0; attempt < 3; attempt++) {
            BasicFileAttributeView beforeView = directory.getFileAttributeView(
                    fileName,
                    BasicFileAttributeView.class,
                    LinkOption.NOFOLLOW_LINKS
            );
            if (beforeView == null) {
                throw new IOException("Basic file attributes are unavailable");
            }
            BasicFileAttributes before = beforeView.readAttributes();
            if (!before.isRegularFile() || before.isSymbolicLink()) {
                throw new OutsideRootException();
            }
            SeekableByteChannel channel = null;

            try {
                channel = directory.newByteChannel(fileName, READ_NOFOLLOW);
                BasicFileAttributeView afterView = directory.getFileAttributeView(
                        fileName,
                        BasicFileAttributeView.class,
                        LinkOption.NOFOLLOW_LINKS
                );
                if (afterView == null) {
                    throw new IOException("Basic file attributes are unavailable");
                }
                BasicFileAttributes after = afterView.readAttributes();

                if (sameFileVersion(before, after) && channel.size() == after.size()) {
                    return new OpenedFile(channel, after);
                }

                lastFailure = new IOException(
                        "File changed while it was being opened: " + fileName
                );
            } catch (IOException e) {
                lastFailure = e;
            }

            if (channel != null) {
                channel.close();
            }
        }

        throw Objects.requireNonNullElseGet(
                lastFailure,
                () -> new IOException("Failed to open file: " + fileName)
        );
    }

    private static OpenedFile openWithCanonicalRecheck(
            Path realRoot,
            Path relativePath
    ) throws IOException {
        Path path = realRoot.resolve(relativePath).normalize();
        IOException lastFailure = null;

        for (int attempt = 0; attempt < 3; attempt++) {
            Path beforeRealPath = path.toRealPath();
            if (!beforeRealPath.startsWith(realRoot)) {
                throw new OutsideRootException();
            }
            BasicFileAttributes before = Files.readAttributes(
                    beforeRealPath,
                    BasicFileAttributes.class,
                    LinkOption.NOFOLLOW_LINKS
            );
            if (!before.isRegularFile() || before.isSymbolicLink()) {
                throw new OutsideRootException();
            }
            SeekableByteChannel channel = null;

            try {
                channel = Files.newByteChannel(beforeRealPath, READ_NOFOLLOW);
                Path afterRealPath = path.toRealPath();
                BasicFileAttributes after = Files.readAttributes(
                        afterRealPath,
                        BasicFileAttributes.class,
                        LinkOption.NOFOLLOW_LINKS
                );

                if (afterRealPath.startsWith(realRoot)
                        && beforeRealPath.equals(afterRealPath)
                        && sameFileVersion(before, after)
                        && channel.size() == after.size()) {
                    return new OpenedFile(channel, after);
                }

                lastFailure = new IOException(
                        "File changed while it was being opened: " + path
                );
            } catch (IOException e) {
                lastFailure = e;
            }

            if (channel != null) channel.close();
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

    private Path realWebRoot() throws IOException {
        Path cached = realWebRoot;
        if (cached != null) return cached;

        synchronized (this) {
            cached = realWebRoot;
            if (cached == null) {
                cached = webRoot.toRealPath();
                realWebRoot = cached;
            }
        }
        return cached;
    }

    private static String weakFileETag(BasicFileAttributes attributes) {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is unavailable", e);
        }

        updateDigest(digest, "bluemap-web-file-validator-v1");
        Object fileKey = attributes.fileKey();
        updateDigest(digest, fileKey == null ? "" : fileKey.getClass().getName());
        updateDigest(digest, fileKey == null ? "" : fileKey.toString());

        Instant modified = attributes.lastModifiedTime().toInstant();
        updateDigest(digest, Long.toString(modified.getEpochSecond()));
        updateDigest(digest, Integer.toString(modified.getNano()));
        updateDigest(digest, Long.toString(attributes.size()));

        return "W/\"" + HexFormat.of().formatHex(digest.digest()) + "\"";
    }

    private static void updateDigest(MessageDigest digest, String value) {
        byte[] bytes = value.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        digest.update((byte) (bytes.length >>> 24));
        digest.update((byte) (bytes.length >>> 16));
        digest.update((byte) (bytes.length >>> 8));
        digest.update((byte) bytes.length);
        digest.update(bytes);
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
            SeekableByteChannel channel,
            BasicFileAttributes attributes
    ) implements AutoCloseable {

        @Override
        public void close() throws IOException {
            channel.close();
        }

    }

    @FunctionalInterface
    interface OpenHook {

        void run() throws IOException;

    }

    private static final class OutsideRootException extends IOException {}

}
