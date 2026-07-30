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

import de.bluecolored.bluemap.common.web.http.HttpHeader;
import de.bluecolored.bluemap.common.web.http.HttpRequest;
import de.bluecolored.bluemap.core.storage.CacheMetadata;
import org.jetbrains.annotations.Nullable;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatterBuilder;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.time.format.ResolverStyle;
import java.time.temporal.ChronoField;
import java.util.HexFormat;
import java.util.Locale;
import java.util.regex.Pattern;

final class HttpCacheSupport {

    private static final DateTimeFormatter IMF_FIXDATE = new DateTimeFormatterBuilder()
            .parseCaseInsensitive()
            .appendPattern("EEE, dd MMM uuuu HH:mm:ss 'GMT'")
            .toFormatter(Locale.US)
            .withResolverStyle(ResolverStyle.STRICT);
    private static final DateTimeFormatter RFC_850_DATE = new DateTimeFormatterBuilder()
            .parseCaseInsensitive()
            .appendPattern("EEEE, dd-MMM-")
            .appendValueReduced(ChronoField.YEAR, 2, 2, 1970)
            .appendPattern(" HH:mm:ss 'GMT'")
            .toFormatter(Locale.US)
            .withResolverStyle(ResolverStyle.STRICT);
    private static final DateTimeFormatter ASCTIME_DATE = new DateTimeFormatterBuilder()
            .parseCaseInsensitive()
            .appendPattern("EEE MMM d HH:mm:ss uuuu")
            .toFormatter(Locale.US)
            .withResolverStyle(ResolverStyle.STRICT);
    private static final Pattern QUALITY_VALUE =
            Pattern.compile("(?:0(?:\\.\\d{0,3})?|1(?:\\.0{0,3})?)");

    private HttpCacheSupport() {}

    static boolean acceptsEncoding(HttpRequest request, String requiredEncoding) {
        String required = requiredEncoding.isEmpty() ? "identity" : requiredEncoding.toLowerCase(Locale.ROOT);
        HttpHeader header = request.getHeader("Accept-Encoding");
        if (header == null) return true;
        if (header.getValue().isBlank()) return required.equals("identity");

        Double requiredQuality = null;
        Double wildcardQuality = null;
        for (String entry : header.getValue().split(",")) {
            String[] parts = entry.trim().split(";");
            String encoding = parts[0].trim().toLowerCase(Locale.ROOT);
            if (encoding.isEmpty()) continue;

            double quality = 1.0;
            boolean qualitySeen = false;
            for (int i = 1; i < parts.length; i++) {
                String[] pair = parts[i].trim().split("=", 2);
                if (pair.length == 2 && pair[0].trim().equalsIgnoreCase("q")) {
                    String value = pair[1].trim();
                    if (qualitySeen || !QUALITY_VALUE.matcher(value).matches()) {
                        quality = 0.0;
                    } else {
                        quality = Double.parseDouble(value);
                    }
                    qualitySeen = true;
                }
            }

            if (encoding.equals(required)) {
                requiredQuality = requiredQuality == null
                        ? quality
                        : Math.max(requiredQuality, quality);
            }
            if (encoding.equals("*")) {
                wildcardQuality = wildcardQuality == null
                        ? quality
                        : Math.max(wildcardQuality, quality);
            }
        }

        if (requiredQuality != null) return requiredQuality > 0.0;
        if (required.equals("identity")) return wildcardQuality == null || wildcardQuality > 0.0;
        return wildcardQuality != null && wildcardQuality > 0.0;
    }

    static @Nullable String eTag(@Nullable CacheMetadata metadata) {
        if (metadata == null || metadata.contentHash() == null) return null;
        return "\"" + HexFormat.of().formatHex(metadata.contentHash()) + "\"";
    }

    static @Nullable String lastModified(@Nullable CacheMetadata metadata) {
        if (metadata == null || metadata.updatedAt() <= 0) return null;
        return formatHttpDate(metadata.updatedAt());
    }

    static String formatHttpDate(long epochMillis) {
        return IMF_FIXDATE.format(
                Instant.ofEpochMilli(epochMillis).atOffset(ZoneOffset.UTC)
        );
    }

    static boolean isNotModified(
            HttpRequest request, @Nullable String eTag, @Nullable CacheMetadata metadata
    ) {
        HttpHeader ifNoneMatch = request.getHeader("If-None-Match");
        if (ifNoneMatch != null) {
            String normalizedCurrent = eTag == null ? null : weakOpaqueTag(eTag);
            return weakListContains(ifNoneMatch.getValue(), normalizedCurrent);
        }

        if (metadata == null || metadata.updatedAt() <= 0) return false;
        HttpHeader ifModifiedSince = request.getHeader("If-Modified-Since");
        if (ifModifiedSince == null) return false;
        Long since = parseHttpDate(ifModifiedSince.getValue());
        return since != null && since / 1000 >= metadata.updatedAt() / 1000;
    }

    static @Nullable Long parseHttpDate(String value) {
        Long parsed = parseHttpDate(value, IMF_FIXDATE);
        if (parsed != null) return parsed;

        parsed = parseHttpDate(value, RFC_850_DATE);
        if (parsed != null) return parsed;

        return parseHttpDate(value.trim().replaceAll("\\s+", " "), ASCTIME_DATE);
    }

    private static @Nullable Long parseHttpDate(String value, DateTimeFormatter formatter) {
        try {
            return LocalDateTime.parse(value, formatter).toInstant(ZoneOffset.UTC).toEpochMilli();
        } catch (DateTimeParseException ignored) {
            return null;
        }
    }

    private static boolean weakListContains(
            String value, @Nullable String currentOpaqueTag
    ) {
        int index = 0;
        while (index < value.length()) {
            while (index < value.length()
                    && (value.charAt(index) == ' '
                    || value.charAt(index) == '\t'
                    || value.charAt(index) == ',')) {
                index++;
            }
            if (index >= value.length()) return false;

            if (value.charAt(index) == '*') {
                index++;
                while (index < value.length()
                        && (value.charAt(index) == ' ' || value.charAt(index) == '\t')) {
                    index++;
                }
                if (index == value.length() || value.charAt(index) == ',') return true;
                index = nextListEntry(value, index);
                continue;
            }
            if (value.regionMatches(true, index, "W/", 0, 2)) index += 2;
            if (index >= value.length() || value.charAt(index) != '"') {
                index = nextListEntry(value, index);
                continue;
            }

            int tagStart = index++;
            boolean valid = true;
            while (index < value.length() && value.charAt(index) != '"') {
                if (!isEntityTagCharacter(value.charAt(index))) valid = false;
                index++;
            }
            if (index >= value.length()) return false;

            String candidate = value.substring(tagStart, ++index);
            while (index < value.length()
                    && (value.charAt(index) == ' ' || value.charAt(index) == '\t')) {
                index++;
            }
            if (index < value.length() && value.charAt(index) != ',') {
                valid = false;
                index = nextListEntry(value, index);
            }

            if (valid && currentOpaqueTag != null
                    && candidate.equals(currentOpaqueTag)) return true;
        }
        return false;
    }

    private static int nextListEntry(String value, int start) {
        int comma = value.indexOf(',', start);
        return comma < 0 ? value.length() : comma + 1;
    }

    private static @Nullable String weakOpaqueTag(String value) {
        String trimmed = value.trim();
        if (trimmed.regionMatches(true, 0, "W/", 0, 2)) {
            trimmed = trimmed.substring(2);
        }
        if (trimmed.length() < 2
                || trimmed.charAt(0) != '"'
                || trimmed.charAt(trimmed.length() - 1) != '"') {
            return null;
        }
        for (int i = 1; i < trimmed.length() - 1; i++) {
            if (!isEntityTagCharacter(trimmed.charAt(i))) return null;
        }
        return trimmed;
    }

    private static boolean isEntityTagCharacter(char character) {
        return character == 0x21
                || character >= 0x23 && character <= 0x7e
                || character >= 0x80;
    }

}
