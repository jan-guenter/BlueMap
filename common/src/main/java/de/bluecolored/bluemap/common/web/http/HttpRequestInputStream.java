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

import org.jetbrains.annotations.Nullable;

import java.io.*;
import java.net.InetAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class HttpRequestInputStream implements Closeable {

    private static final Pattern REQUEST_PATTERN = Pattern.compile("^(\\w+) (\\S+) (.+)$");

    private final InetAddress source;
    private final DataInputStream in;
    private final HttpRequestLimits limits;

    private byte[] byteBuffer = new byte[1024];

    public HttpRequestInputStream(InputStream in, InetAddress source) {
        this(in, source, HttpRequestLimits.DEFAULT);
    }

    public HttpRequestInputStream(InputStream in, InetAddress source, HttpRequestLimits limits) {
        this.source = source;
        this.in = new DataInputStream(in);
        this.limits = limits;
    }

    public @Nullable HttpRequest read() throws IOException {

        String requestLine = readLine(limits.maxRequestLineBytes(), "request line").value();

        Matcher m = REQUEST_PATTERN.matcher(requestLine);
        if (!m.find()) throw new IOException("Invalid HTTP Request: Request-Pattern not matching '%s'".formatted(requestLine));

        URI address = URI.create(m.group(2));

        HttpRequest request = new HttpRequest(
                source,
                m.group(1),
                address.getPath()
        );
        request.setVersion(m.group(3));
        request.setRawQueryString(address.getRawQuery());

        // headers
        HeaderBudget headerBudget = new HeaderBudget();
        readHeaders(request, headerBudget);

        // body
        HttpHeader transferEncodingHeader = request.getHeader("transfer-encoding");
        HttpHeader contentLengthHeader = request.getHeader("content-length");
        if (transferEncodingHeader != null) {
            if (contentLengthHeader != null) {
                throw new IOException("Invalid HTTP Request: transfer-encoding and content-length cannot be combined");
            }

            List<String> transferEncodings = transferEncodingHeader.getValues();
            if (transferEncodings.size() != 1 || !transferEncodings.getFirst().equalsIgnoreCase("chunked")) {
                throw new IOException("Invalid HTTP Request: unsupported transfer-encoding");
            }

            request.setBody(readChunkedBody(headerBudget));
        } else {
            int contentLength = 0;
            if (contentLengthHeader != null) {
                try {
                    contentLength = Integer.parseInt(contentLengthHeader.getValue().trim());
                } catch (NumberFormatException ex) {
                    throw new IOException("Invalid HTTP Request: content-length is not a number", ex);
                }
            }

            if (contentLength < 0 || contentLength > limits.maxBodyBytes()) {
                throw new IOException("Invalid HTTP Request: body too large");
            }

            if (contentLength > 0) {
                request.setBody(readBody(contentLength));
            }
        }

        return request;
    }

    private void readHeaders(@Nullable HttpRequest request, HeaderBudget budget) throws IOException {
        while (true) {
            Line line = readLine(limits.maxHeaderBytes(), request == null ? "trailer line" : "header line");
            budget.add(line.bytes());
            if (line.value().isEmpty()) return;

            budget.addHeader();
            int separator = line.value().indexOf(':');
            if (separator <= 0) {
                throw new IOException("Invalid HTTP Request: malformed header");
            }

            if (request != null) {
                String name = line.value().substring(0, separator);
                String value = line.value().substring(separator + 1).trim();
                HttpHeader existingHeader = request.getHeader(name);
                if (existingHeader == null) {
                    request.addHeader(name, value);
                } else {
                    existingHeader.add(value);
                }
            }
        }
    }

    private Line readLine(int maxBytes, String part) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream(Math.min(maxBytes, 1024));

        while (true) {
            int value = in.read();
            if (value == -1) throw new EOFException();
            if (line.size() == maxBytes) {
                throw new IOException("Invalid HTTP Request: " + part + " too large");
            }
            line.write(value);

            if (value == '\n') {
                byte[] bytes = line.toByteArray();
                if (bytes.length < 2 || bytes[bytes.length - 2] != '\r') {
                    throw new IOException("Invalid HTTP Request: " + part + " must end with CRLF");
                }

                return new Line(
                        new String(bytes, 0, bytes.length - 2, StandardCharsets.ISO_8859_1),
                        bytes.length
                );
            }
        }
    }

    private byte[] readChunkedBody(HeaderBudget headerBudget) throws IOException {
        ByteArrayOutputStream body = new ByteArrayOutputStream(1024);

        while (true) {
            String prefix = readLine(limits.maxHeaderBytes(), "chunk prefix").value();
            int extensionSeparator = prefix.indexOf(';');
            String sizeText = (extensionSeparator < 0 ? prefix : prefix.substring(0, extensionSeparator)).trim();
            long parsedSize;
            try {
                parsedSize = Long.parseLong(sizeText, 16);
            } catch (NumberFormatException ex) {
                throw new IOException("Invalid HTTP Request: chunk size is not a number", ex);
            }
            if (parsedSize < 0 || parsedSize > Integer.MAX_VALUE) {
                throw new IOException("Invalid HTTP Request: body too large");
            }

            int size = (int) parsedSize;
            if (body.size() > limits.maxBodyBytes() - size) {
                throw new IOException("Invalid HTTP Request: body too large");
            }

            if (size == 0) {
                readHeaders(null, headerBudget);
                break;
            }

            if (size > byteBuffer.length) byteBuffer = new byte[size];
            in.readFully(byteBuffer, 0, size);
            body.write(byteBuffer, 0, size);
            readCrlf("chunk data");
        }

        return body.toByteArray();
    }

    private byte[] readBody(int contentLength) throws IOException {
        byte[] body = new byte[contentLength];
        in.readFully(body);
        return body;
    }

    private void readCrlf(String part) throws IOException {
        if (in.read() != '\r' || in.read() != '\n') {
            throw new IOException("Invalid HTTP Request: " + part + " must end with CRLF");
        }
    }

    private record Line(String value, int bytes) {}

    private final class HeaderBudget {

        private int count;
        private int bytes;

        void addHeader() throws IOException {
            count++;
            if (count > limits.maxHeaderCount()) {
                throw new IOException("Invalid HTTP Request: too many headers");
            }
        }

        void add(int additionalBytes) throws IOException {
            if (bytes > limits.maxHeaderBytes() - additionalBytes) {
                throw new IOException("Invalid HTTP Request: headers too large");
            }
            bytes += additionalBytes;
        }
    }

    @Override
    public void close() throws IOException {
        in.close();
    }

}
