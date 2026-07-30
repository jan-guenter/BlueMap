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
import de.bluecolored.bluemap.core.storage.MapStorage;
import de.bluecolored.bluemap.core.storage.CacheMetadata;
import de.bluecolored.bluemap.core.storage.compression.CompressedInputStream;
import de.bluecolored.bluemap.core.storage.compression.Compression;
import lombok.Getter;
import lombok.NonNull;
import lombok.RequiredArgsConstructor;
import lombok.Setter;

import java.io.IOException;
import java.util.NoSuchElementException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@RequiredArgsConstructor
@Getter @Setter
public class MapStorageRequestHandler implements HttpRequestHandler {

    private static final Pattern TILE_PATTERN = Pattern.compile("tiles/([\\d/]+)/x(-?[\\d/]+)z(-?[\\d/]+).*");
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
            HttpResponse response = new HttpResponse(HttpStatusCode.METHOD_NOT_ALLOWED);
            response.addHeader("Allow", "GET, HEAD");
            return response;
        }

        String path = request.getPath();

        //normalize path
        if (path.startsWith("/")) path = path.substring(1);
        if (path.endsWith("/")) path = path.substring(0, path.length() - 1);

        try {

            // provide map-tiles
            Matcher tileMatcher = TILE_PATTERN.matcher(path);
            if (tileMatcher.matches()) {
                int lod = Integer.parseInt(tileMatcher.group(1));
                int x = Integer.parseInt(tileMatcher.group(2).replace("/", ""));
                int z = Integer.parseInt(tileMatcher.group(3).replace("/", ""));

                GridStorage gridStorage = lod == 0 ? mapStorage.hiresTiles() : mapStorage.lowresTiles(lod);
                CompressedInputStream in = gridStorage.read(x, z);
                if (in == null) {
                    HttpResponse response = new HttpResponse(HttpStatusCode.NO_CONTENT);
                    response.addHeader("Cache-Control", "no-store");
                    return response;
                }

                String cacheControl = "public,max-age=" + Math.max(0, tileMaxAgeSeconds) + ",must-revalidate";
                String contentType = lod == 0 ? "application/octet-stream" : "image/png";
                return storedResponse(in, contentType, cacheControl, request, head);
            }

            // provide meta-data
            CompressedInputStream in = switch (path) {
                case "settings.json" -> mapStorage.settings().read();
                case "textures.json" -> mapStorage.textures().read();
                case "live/markers.json" -> mapStorage.markers().read();
                case "live/players.json" -> mapStorage.players().read();
                default -> path.startsWith("assets/") ? mapStorage.asset(path.substring(7)).read() : null;
            };
            if (in != null){
                String cacheControl = switch (path) {
                    case "live/players.json" -> "private,no-store";
                    case "live/markers.json" -> "public,no-cache";
                    default -> "public,no-cache";
                };
                return storedResponse(
                        in, ContentTypeRegistry.fromFileName(path), cacheControl,
                        request, head
                );
            }

        } catch (NumberFormatException | NoSuchElementException ignore){
        } catch (IOException ex) {
            Logger.global.logError("Failed to read map-tile for web-request.", ex);
            return new HttpResponse(HttpStatusCode.INTERNAL_SERVER_ERROR);
        }

        return new HttpResponse(HttpStatusCode.NOT_FOUND);
    }

    private HttpResponse storedResponse(
            CompressedInputStream data,
            String contentType,
            String cacheControl,
            HttpRequest request,
            boolean head
    ) throws IOException {
        Compression compression = data.getCompression();
        String requiredEncoding = compression == Compression.NONE ? "identity" : compression.getId();

        if (!HttpCacheSupport.acceptsEncoding(request, requiredEncoding)) {
            data.close();
            HttpResponse response = new HttpResponse(HttpStatusCode.NOT_ACCEPTABLE);
            response.addHeader("Cache-Control", "no-store");
            response.addHeader("Vary", "Accept-Encoding");
            response.addHeader("Content-Type", "application/problem+json");
            response.addHeader("X-BlueMap-Required-Content-Encoding", requiredEncoding);
            if (!head) {
                response.setBody(
                        "{\"code\":\"bluemap_required_content_encoding\","
                                + "\"requiredEncoding\":\"" + requiredEncoding + "\"}"
                );
            }
            return response;
        }

        CacheMetadata metadata = data.getCacheMetadata();
        String eTag = HttpCacheSupport.eTag(metadata);
        String lastModified = HttpCacheSupport.lastModified(metadata);

        if (HttpCacheSupport.isNotModified(request, eTag, metadata)) {
            data.close();
            HttpResponse response = new HttpResponse(HttpStatusCode.NOT_MODIFIED);
            addStoredHeaders(response, compression, contentType, cacheControl, eTag, lastModified);
            return response;
        }

        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        addStoredHeaders(response, compression, contentType, cacheControl, eTag, lastModified);
        if (head) data.close();
        else response.setBody(data);
        return response;
    }

    private static void addStoredHeaders(
            HttpResponse response,
            Compression compression,
            String contentType,
            String cacheControl,
            String eTag,
            String lastModified
    ) {
        response.addHeader("Cache-Control", cacheControl);
        response.addHeader("Vary", "Accept-Encoding");
        response.addHeader("Content-Type", contentType);
        if (compression != Compression.NONE) response.addHeader("Content-Encoding", compression.getId());
        if (eTag != null) response.addHeader("ETag", eTag);
        if (lastModified != null) response.addHeader("Last-Modified", lastModified);
    }

}
