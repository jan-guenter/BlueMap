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
    private HttpCacheSupport() {}

    static boolean acceptsEncoding(HttpRequest request, String requiredEncoding) {
        String required = requiredEncoding.isEmpty() ? "identity" : requiredEncoding.toLowerCase(Locale.ROOT);
        HttpHeader header = request.getHeader("Accept-Encoding");
        if (header == null) return true;
        if (header.getValue().isBlank()) return required.equals("identity");

        int requiredQuality = -1;
        int wildcardQuality = -1;
        String value = header.getValue();
        for (int entryStart = 0; entryStart <= value.length();) {
            int entryEnd = value.indexOf(',', entryStart);
            if (entryEnd < 0) entryEnd = value.length();

            int separator = value.indexOf(';', entryStart);
            if (separator < 0 || separator > entryEnd) separator = entryEnd;
            int encodingStart = skipWhitespace(value, entryStart, separator);
            int encodingEnd = trimWhitespace(value, encodingStart, separator);

            int quality = 1000;
            boolean qualitySeen = false;
            for (int parameterStart = separator;
                 parameterStart < entryEnd;) {
                parameterStart++;
                int parameterEnd = value.indexOf(';', parameterStart);
                if (parameterEnd < 0 || parameterEnd > entryEnd) {
                    parameterEnd = entryEnd;
                }

                int equals = value.indexOf('=', parameterStart);
                if (equals >= 0 && equals < parameterEnd) {
                    int nameStart = skipWhitespace(
                            value, parameterStart, equals
                    );
                    int nameEnd = trimWhitespace(value, nameStart, equals);
                    if (nameEnd - nameStart == 1
                            && (value.charAt(nameStart) == 'q'
                            || value.charAt(nameStart) == 'Q')) {
                        int qualityStart = skipWhitespace(
                                value, equals + 1, parameterEnd
                        );
                        int qualityEnd = trimWhitespace(
                                value, qualityStart, parameterEnd
                        );
                        if (qualitySeen) {
                            quality = 0;
                        } else {
                            quality = parseQuality(
                                    value, qualityStart, qualityEnd
                            );
                        }
                        qualitySeen = true;
                    }
                }
                parameterStart = parameterEnd;
            }

            if (encodingEnd > encodingStart) {
                if (regionEqualsIgnoreCase(
                        value, encodingStart, encodingEnd, required
                )) {
                    requiredQuality = Math.max(requiredQuality, quality);
                }
                if (encodingEnd - encodingStart == 1
                        && value.charAt(encodingStart) == '*') {
                    wildcardQuality = Math.max(wildcardQuality, quality);
                }
            }

            if (entryEnd == value.length()) break;
            entryStart = entryEnd + 1;
        }

        if (requiredQuality >= 0) return requiredQuality > 0;
        if (required.equals("identity")) {
            return wildcardQuality < 0 || wildcardQuality > 0;
        }
        return wildcardQuality > 0;
    }

    private static int parseQuality(String value, int start, int end) {
        int length = end - start;
        if (length == 1) {
            return switch (value.charAt(start)) {
                case '0' -> 0;
                case '1' -> 1000;
                default -> 0;
            };
        }
        if (length < 2 || length > 5 || value.charAt(start + 1) != '.') {
            return 0;
        }

        char whole = value.charAt(start);
        if (whole != '0' && whole != '1') return 0;
        int quality = whole == '1' ? 1000 : 0;
        int fractionDigits = length - 2;
        for (int i = 0; i < fractionDigits; i++) {
            char digit = value.charAt(start + 2 + i);
            if (digit < '0' || digit > '9') return 0;
            if (whole == '1' && digit != '0') return 0;
            if (whole == '0') {
                quality += (digit - '0') * switch (i) {
                    case 0 -> 100;
                    case 1 -> 10;
                    default -> 1;
                };
            }
        }
        return quality;
    }

    private static int skipWhitespace(String value, int start, int end) {
        while (start < end) {
            char character = value.charAt(start);
            if (character != ' ' && character != '\t') break;
            start++;
        }
        return start;
    }

    private static int trimWhitespace(String value, int start, int end) {
        while (end > start) {
            char character = value.charAt(end - 1);
            if (character != ' ' && character != '\t') break;
            end--;
        }
        return end;
    }

    private static boolean regionEqualsIgnoreCase(
            String value, int start, int end, String expected
    ) {
        return end - start == expected.length()
                && value.regionMatches(true, start, expected, 0, expected.length());
    }

    static @Nullable String eTag(@Nullable CacheMetadata metadata) {
        if (metadata == null) return null;
        byte[] contentHash = metadata.contentHash();
        if (contentHash == null) return null;
        return (metadata.weak() ? "W/" : "")
                + "\"" + HexFormat.of().formatHex(contentHash) + "\"";
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
