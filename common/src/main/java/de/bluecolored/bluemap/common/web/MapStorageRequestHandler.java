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

import de.bluecolored.bluemap.api.ContentTypeRegistry;
import de.bluecolored.bluemap.common.web.http.HttpRequest;
import de.bluecolored.bluemap.common.web.http.HttpRequestHandler;
import de.bluecolored.bluemap.common.web.http.HttpResponse;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;
import de.bluecolored.bluemap.core.logger.Logger;
import de.bluecolored.bluemap.core.storage.GridStorage;
import de.bluecolored.bluemap.core.storage.ItemStorage;
import de.bluecolored.bluemap.core.storage.MapStorage;
import de.bluecolored.bluemap.core.storage.CacheMetadata;
import de.bluecolored.bluemap.core.storage.StoredDataMetadata;
import de.bluecolored.bluemap.core.storage.compression.CompressedInputStream;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import de.bluecolored.bluemap.core.util.stream.OnCloseInputStream;
import lombok.Getter;
import lombok.NonNull;
import lombok.RequiredArgsConstructor;
import lombok.Setter;
import org.jetbrains.annotations.Nullable;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.NoSuchElementException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@RequiredArgsConstructor
@Getter @Setter
public class MapStorageRequestHandler implements HttpRequestHandler {

    private static final Pattern TILE_PATTERN = Pattern.compile("tiles/([\\d/]+)/x(-?[\\d/]+)z(-?[\\d/]+).*");
    private static final String OVERLOAD_PROBLEM = "{\"type\":\"about:blank\",\"title\":\"Service Unavailable\",\"status\":503,\"code\":\"bluemap_overloaded\"}";
    public static final long DEFAULT_TILE_MAX_AGE_SECONDS = 60;

    private @NonNull MapStorage mapStorage;
    private long tileMaxAgeSeconds = Long.getLong(
            "bluemap.web.tile-cache-max-age-seconds",
            DEFAULT_TILE_MAX_AGE_SECONDS
    );

    @SuppressWarnings("resource")
    @Override
    public HttpResponse handle(HttpRequest request) {
        boolean head = request.getMethod().equalsIgnoreCase("HEAD");
        if (!head && !request.getMethod().equalsIgnoreCase("GET")) {
            HttpResponse response = errorResponse(HttpStatusCode.METHOD_NOT_ALLOWED);
            response.addHeader("Allow", "GET, HEAD");
            return response;
        }

        String path = request.getPath();
        if (path == null) return errorResponse(HttpStatusCode.BAD_REQUEST);

        //normalize path
        if (path.startsWith("/")) path = path.substring(1);
        if (path.endsWith("/")) path = path.substring(0, path.length() - 1);
        boolean requestGzipped = path.endsWith(".gz");
        if (requestGzipped) path = path.substring(0, path.length() - 3);

        try {

            // provide map-tiles
            Matcher tileMatcher = TILE_PATTERN.matcher(path);
            if (tileMatcher.matches()) {
                int lod = Integer.parseInt(tileMatcher.group(1));
                int x = Integer.parseInt(tileMatcher.group(2).replace("/", ""));
                int z = Integer.parseInt(tileMatcher.group(3).replace("/", ""));

                GridStorage gridStorage = lod == 0 ? mapStorage.hiresTiles() : mapStorage.lowresTiles(lod);
                String cacheControl = "public,max-age=" + Math.max(0, tileMaxAgeSeconds)
                        + ",must-revalidate,no-transform";
                String contentType = lod == 0 ? "application/octet-stream" : "image/png";
                EncodingDecision encoding = requestGzipped
                        ? EncodingDecision.UNKNOWN
                        : encodingDecision(request, gridStorage.compression());
                return withReadPermit(() -> tileResponse(
                        gridStorage, x, z, contentType, cacheControl,
                        request, head, encoding, requestGzipped
                ));
            }

            // provide meta-data
            ItemStorage itemStorage = switch (path) {
                case "settings.json" -> mapStorage.settings();
                case "textures.json" -> mapStorage.textures();
                case "live/markers.json" -> mapStorage.markers();
                case "live/players.json" -> mapStorage.players();
                default -> path.startsWith("assets/") ? mapStorage.asset(path.substring(7)) : null;
            };
            if (itemStorage == null) return errorResponse(HttpStatusCode.NOT_FOUND);

            String cacheControl = switch (path) {
                case "live/players.json" -> "private,no-store,no-transform";
                default -> "public,no-cache,no-transform";
            };
            String contentType = ContentTypeRegistry.fromFileName(path);
            EncodingDecision encoding = requestGzipped
                    ? EncodingDecision.UNKNOWN
                    : encodingDecision(request, itemStorage.compression());
            return withReadPermit(() -> itemResponse(
                    itemStorage, contentType, cacheControl, request, head,
                    encoding, requestGzipped
            ));

        } catch (NumberFormatException | NoSuchElementException ignore){
        } catch (IOException ex) {
            Logger.global.logError("Failed to read map-tile for web-request.", ex);
            return errorResponse(HttpStatusCode.INTERNAL_SERVER_ERROR);
        }

        return errorResponse(HttpStatusCode.NOT_FOUND);
    }

    private HttpResponse tileResponse(
            GridStorage gridStorage,
            int x,
            int z,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head,
            EncodingDecision encoding,
            boolean requestGzipped
    ) throws IOException {
        return tileResponse(
                gridStorage, x, z, contentType, cacheControl, request, head,
                encoding, requestGzipped, null, 1
        );
    }

    private HttpResponse tileResponse(
            GridStorage gridStorage,
            int x,
            int z,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head,
            EncodingDecision encoding,
            boolean requestGzipped,
            @Nullable StoredDataMetadata prefetchedMetadata,
            int remainingLengthRetries
    ) throws IOException {
        boolean responseAdmission = mapStorage.requiresResponseAdmission();
        boolean atomicLengthRead = responseAdmission
                && gridStorage.supportsAtomicLengthRead();
        StoredDataMetadata metadata = prefetchedMetadata;
        if (metadata == null && (responseAdmission || (!requestGzipped
                && (needsMetadata(request) || encoding.requiresPreflight())))) {
            metadata = gridStorage.readMetadata(x, z);
        }

        if (!requestGzipped && metadata != null) {
            HttpResponse metadataResponse = metadataResponse(
                    metadata,
                    contentType,
                    cacheControl,
                    request,
                    head,
                    encoding
            );
            if (metadataResponse != null) return metadataResponse;
        }

        boolean exactResponseReservation = atomicLengthRead
                && metadata != null
                && (!requestGzipped
                || metadata.compression() == Compression.GZIP);
        MapStorage.ResponsePermit responsePermit = responseAdmission
                ? mapStorage.tryAcquireResponsePermit(
                        exactResponseReservation
                                ? metadata.contentLength()
                                : -1
                ) : null;
        if (responseAdmission && responsePermit == null) {
            return overloadResponse();
        }

        try {
            CompressedInputStream in = atomicLengthRead && metadata != null
                    ? gridStorage.read(x, z, metadata.contentLength())
                    : gridStorage.read(x, z);
            if (in == null) {
                if (atomicLengthRead && metadata != null) {
                    StoredDataMetadata current =
                            gridStorage.readMetadata(x, z);
                    if (current != null) {
                        responsePermit.close();
                        responsePermit = null;
                        if (remainingLengthRetries == 0) {
                            return overloadResponse();
                        }
                        return tileResponse(
                                gridStorage, x, z, contentType, cacheControl,
                                request, head, encoding, requestGzipped,
                                current, remainingLengthRetries - 1
                        );
                    }
                }
                HttpResponse response = new HttpResponse(
                        HttpStatusCode.NO_CONTENT
                );
                response.addHeader("Cache-Control", "no-store,no-transform");
                return attachResponsePermit(response, responsePermit);
            }

            HttpResponse response = requestGzipped
                    ? gzipFileResponse(
                            in, contentType, cacheControl, request, head
                    )
                    : storedResponse(
                            in, contentType, cacheControl, request, head,
                            encoding
                    );
            return attachResponsePermit(response, responsePermit);
        } catch (IOException | RuntimeException ex) {
            if (responsePermit != null) responsePermit.close();
            throw ex;
        }
    }

    private HttpResponse itemResponse(
            ItemStorage itemStorage,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head,
            EncodingDecision encoding,
            boolean requestGzipped
    ) throws IOException {
        return itemResponse(
                itemStorage, contentType, cacheControl, request, head,
                encoding, requestGzipped, null, 1
        );
    }

    private HttpResponse itemResponse(
            ItemStorage itemStorage,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head,
            EncodingDecision encoding,
            boolean requestGzipped,
            @Nullable StoredDataMetadata prefetchedMetadata,
            int remainingLengthRetries
    ) throws IOException {
        boolean responseAdmission = mapStorage.requiresResponseAdmission();
        boolean atomicLengthRead = responseAdmission
                && itemStorage.supportsAtomicLengthRead();
        StoredDataMetadata metadata = prefetchedMetadata;
        if (metadata == null && (responseAdmission || (!requestGzipped
                && (needsMetadata(request) || encoding.requiresPreflight())))) {
            metadata = itemStorage.readMetadata();
        }

        if (!requestGzipped && metadata != null) {
            HttpResponse metadataResponse = metadataResponse(
                    metadata,
                    contentType,
                    cacheControl,
                    request,
                    head,
                    encoding
            );
            if (metadataResponse != null) return metadataResponse;
        }

        boolean exactResponseReservation = atomicLengthRead
                && metadata != null
                && (!requestGzipped
                || metadata.compression() == Compression.GZIP);
        MapStorage.ResponsePermit responsePermit = responseAdmission
                ? mapStorage.tryAcquireResponsePermit(
                        exactResponseReservation
                                ? metadata.contentLength()
                                : -1
                ) : null;
        if (responseAdmission && responsePermit == null) {
            return overloadResponse();
        }

        try {
            CompressedInputStream in = atomicLengthRead && metadata != null
                    ? itemStorage.read(metadata.contentLength())
                    : itemStorage.read();
            if (in == null) {
                if (atomicLengthRead && metadata != null) {
                    StoredDataMetadata current = itemStorage.readMetadata();
                    if (current != null) {
                        responsePermit.close();
                        responsePermit = null;
                        if (remainingLengthRetries == 0) {
                            return overloadResponse();
                        }
                        return itemResponse(
                                itemStorage, contentType, cacheControl, request,
                                head, encoding, requestGzipped, current,
                                remainingLengthRetries - 1
                        );
                    }
                }
                return attachResponsePermit(
                        errorResponse(HttpStatusCode.NOT_FOUND),
                        responsePermit
                );
            }
            HttpResponse response = requestGzipped
                    ? gzipFileResponse(
                            in, contentType, cacheControl, request, head
                    )
                    : storedResponse(
                            in, contentType, cacheControl, request, head,
                            encoding
                    );
            return attachResponsePermit(response, responsePermit);
        } catch (IOException | RuntimeException ex) {
            if (responsePermit != null) responsePermit.close();
            throw ex;
        }
    }

    private HttpResponse gzipFileResponse(
            CompressedInputStream data,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head
    ) throws IOException {
        Compression compression = data.getCompression();
        CacheMetadata metadata = data.getCacheMetadata();
        String eTag = compression == Compression.GZIP
                ? HttpCacheSupport.eTag(metadata)
                : null;
        String lastModified = HttpCacheSupport.lastModified(metadata);

        if (HttpCacheSupport.isNotModified(request, eTag, metadata)) {
            data.close();
            HttpResponse response = new HttpResponse(HttpStatusCode.NOT_MODIFIED);
            addGzipFileHeaders(
                    response, contentType, cacheControl,
                    eTag, lastModified, -1
            );
            return response;
        }

        if (compression == Compression.GZIP) {
            HttpResponse response = new HttpResponse(HttpStatusCode.OK);
            addGzipFileHeaders(
                    response, contentType, cacheControl,
                    eTag, lastModified, data.getContentLength()
            );
            if (head) {
                data.close();
                response.setBodySuppressed(true);
            } else {
                response.setBody(data);
            }
            return response;
        }

        byte[] compressedData;
        try (
                data;
                InputStream decompressed = data.decompress();
                ByteArrayOutputStream byteOut = new ByteArrayOutputStream();
                OutputStream compressedOut = Compression.GZIP.compress(byteOut)
        ) {
            decompressed.transferTo(compressedOut);
            compressedOut.close();
            compressedData = byteOut.toByteArray();
        }

        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        addGzipFileHeaders(
                response, contentType, cacheControl,
                null, lastModified, compressedData.length
        );
        if (head) {
            response.setBodySuppressed(true);
        } else {
            response.setBody(new ByteArrayInputStream(compressedData));
        }
        return response;
    }

    private HttpResponse withReadPermit(ReadOperation operation)
            throws IOException {
        MapStorage.ReadPermit readPermit = mapStorage.tryAcquireReadPermit();
        if (readPermit == null) {
            return overloadResponse();
        }

        try {
            return operation.read();
        } finally {
            readPermit.close();
        }
    }

    /**
     * Reconciles a metadata reservation with the outgoing representation,
     * including legacy {@code .gz} transcoding, and transfers the permit to
     * the response body.
     */
    private HttpResponse attachResponsePermit(
            HttpResponse response,
            @Nullable MapStorage.ResponsePermit responsePermit
    ) throws IOException {
        if (response.getStatusCode() != HttpStatusCode.OK
                || response.getBody() == null
                || response.isBodySuppressed()) {
            if (responsePermit != null) responsePermit.close();
            return response;
        }

        long contentLength = responseContentLength(response);
        if (responsePermit == null) {
            responsePermit = mapStorage.tryAcquireResponsePermit(
                    contentLength
            );
        } else if (!responsePermit.tryResize(contentLength)) {
            responsePermit.close();
            response.close();
            return overloadResponse();
        }

        if (responsePermit == null) {
            response.close();
            return overloadResponse();
        }

        response.setBody(new OnCloseInputStream(
                response.getBody(),
                responsePermit
        ));
        return response;
    }

    private static long responseContentLength(HttpResponse response) {
        var header = response.getHeader("Content-Length");
        if (header == null) return -1;
        try {
            long length = Long.parseLong(header.getValue());
            return length < 0 ? -1 : length;
        } catch (NumberFormatException ignored) {
            return -1;
        }
    }

    private static boolean needsMetadata(HttpRequest request) {
        return request.getMethod().equalsIgnoreCase("HEAD")
                || request.getHeader("If-None-Match") != null
                || request.getHeader("If-Modified-Since") != null;
    }

    private static EncodingDecision encodingDecision(
            HttpRequest request, @Nullable Compression compression
    ) {
        if (compression == null) return EncodingDecision.UNKNOWN;
        return new EncodingDecision(
                compression,
                HttpCacheSupport.acceptsEncoding(
                        request, contentEncoding(compression)
                )
        );
    }

    private @Nullable HttpResponse metadataResponse(
            @Nullable StoredDataMetadata data,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head,
            EncodingDecision encoding
    ) {
        if (data == null) return null;

        Compression compression = data.compression();
        boolean accepted = encoding.compression() == compression
                ? encoding.accepted()
                : HttpCacheSupport.acceptsEncoding(
                        request, contentEncoding(compression)
                );
        if (!accepted) {
            return notAcceptableResponse(compression, head);
        }

        CacheMetadata cacheMetadata = data.cacheMetadata();
        String eTag = HttpCacheSupport.eTag(cacheMetadata);
        String lastModified = HttpCacheSupport.lastModified(cacheMetadata);
        if (HttpCacheSupport.isNotModified(request, eTag, cacheMetadata)) {
            HttpResponse response = new HttpResponse(HttpStatusCode.NOT_MODIFIED);
            addStoredHeaders(
                    response, compression, contentType, cacheControl,
                    eTag, lastModified, -1
            );
            return response;
        }

        if (!head) return null;
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.setBodySuppressed(true);
        addStoredHeaders(
                response, compression, contentType, cacheControl,
                eTag, lastModified, data.contentLength()
        );
        return response;
    }

    private HttpResponse storedResponse(
            CompressedInputStream data,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head,
            EncodingDecision encoding
    ) throws IOException {
        Compression compression = data.getCompression();
        String requiredEncoding = contentEncoding(compression);

        boolean accepted = encoding.compression() == compression
                ? encoding.accepted()
                : HttpCacheSupport.acceptsEncoding(request, requiredEncoding);
        if (!accepted) {
            data.close();
            return notAcceptableResponse(compression, head);
        }

        CacheMetadata metadata = data.getCacheMetadata();
        String eTag = HttpCacheSupport.eTag(metadata);
        String lastModified = HttpCacheSupport.lastModified(metadata);

        if (HttpCacheSupport.isNotModified(request, eTag, metadata)) {
            data.close();
            HttpResponse response = new HttpResponse(HttpStatusCode.NOT_MODIFIED);
            addStoredHeaders(
                    response, compression, contentType, cacheControl,
                    eTag, lastModified, -1
            );
            return response;
        }

        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        addStoredHeaders(
                response, compression, contentType, cacheControl,
                eTag, lastModified, data.getContentLength()
        );
        if (head) {
            data.close();
            response.setBodySuppressed(true);
        }
        else response.setBody(data);
        return response;
    }

    private static HttpResponse notAcceptableResponse(
            Compression compression, boolean head
    ) {
        String requiredEncoding =
                compression == Compression.NONE ? "identity" : compression.getId();
        HttpResponse response = new HttpResponse(HttpStatusCode.NOT_ACCEPTABLE);
        response.addHeader("Cache-Control", "no-store,no-transform");
        response.addHeader("Vary", "Accept-Encoding");
        response.addHeader("Content-Type", "application/problem+json");
        response.addHeader("X-BlueMap-Required-Content-Encoding", requiredEncoding);
        if (!head) {
            response.setBody(
                    "{\"code\":\"bluemap_required_content_encoding\","
                            + "\"requiredEncoding\":\"" + requiredEncoding + "\"}"
            );
        } else {
            response.setBodySuppressed(true);
        }
        return response;
    }

    private static String contentEncoding(Compression compression) {
        return compression == Compression.NONE ? "identity" : compression.getId();
    }

    private static HttpResponse errorResponse(HttpStatusCode statusCode) {
        HttpResponse response = new HttpResponse(statusCode);
        response.addHeader("Cache-Control", "no-store,no-transform");
        return response;
    }

    private static HttpResponse overloadResponse() {
        HttpResponse response = new HttpResponse(HttpStatusCode.SERVICE_UNAVAILABLE);
        response.addHeader("Retry-After", "1");
        response.addHeader("Cache-Control", "private,no-store,no-transform");
        response.addHeader("Content-Type", "application/problem+json");
        response.addHeader("X-BlueMap-Overload", "capacity");
        response.setBody(OVERLOAD_PROBLEM);
        return response;
    }

    private static void addStoredHeaders(
            HttpResponse response,
            Compression compression,
            String contentType,
            String cacheControl,
            String eTag,
            String lastModified,
            long contentLength
    ) {
        response.addHeader("Cache-Control", cacheControl);
        response.addHeader("Vary", "Accept-Encoding");
        response.addHeader("Content-Type", contentType);
        if (compression != Compression.NONE) response.addHeader("Content-Encoding", compression.getId());
        if (eTag != null) response.addHeader("ETag", eTag);
        if (lastModified != null) response.addHeader("Last-Modified", lastModified);
        if (contentLength >= 0) {
            response.addHeader("Content-Length", Long.toString(contentLength));
        }
    }

    private static void addGzipFileHeaders(
            HttpResponse response,
            String contentType,
            String cacheControl,
            String eTag,
            String lastModified,
            long contentLength
    ) {
        response.addHeader("Cache-Control", cacheControl);
        response.addHeader("Content-Type", contentType);
        if (eTag != null) response.addHeader("ETag", eTag);
        if (lastModified != null) response.addHeader("Last-Modified", lastModified);
        if (contentLength >= 0) {
            response.addHeader("Content-Length", Long.toString(contentLength));
        }
    }

    @FunctionalInterface
    private interface ReadOperation {

        HttpResponse read() throws IOException;

    }

    private record EncodingDecision(
            @Nullable Compression compression,
            boolean accepted
    ) {

        private static final EncodingDecision UNKNOWN =
                new EncodingDecision(null, false);

        boolean requiresPreflight() {
            return compression != null && !accepted;
        }

    }

}
