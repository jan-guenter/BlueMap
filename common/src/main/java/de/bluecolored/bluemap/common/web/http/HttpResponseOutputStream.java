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

import lombok.RequiredArgsConstructor;

import java.io.Closeable;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

@RequiredArgsConstructor
public class HttpResponseOutputStream implements Closeable {

    private static final byte[] CRLF = "\r\n".getBytes(StandardCharsets.UTF_8);

    private final OutputStream outputStream;

    static final int COPY_BUFFER_SIZE = 64 * 1024;

    private final byte[] byteBuffer = new byte[COPY_BUFFER_SIZE];

    public void write(HttpResponse response) throws IOException {
        HttpStatusCode statusCode = response.getStatusCode();
        InputStream body = response.getBody();
        HttpResponseStreamWriter streamWriter = response.getStreamWriter();
        boolean bodyAllowed = !response.isBodySuppressed()
                && statusCode.getCode() >= 200
                && statusCode != HttpStatusCode.NO_CONTENT
                && statusCode != HttpStatusCode.NOT_MODIFIED;
        HttpHeader contentLengthHeader = response.getHeader("Content-Length");
        Long contentLength = contentLengthHeader == null
                ? null
                : parseContentLength(contentLengthHeader.getValue());
        boolean fixedLength = contentLength != null;
        if (bodyAllowed && streamWriter != null && fixedLength) {
            throw new IOException("Streaming response cannot use Content-Length");
        }
        boolean chunked = (body != null || streamWriter != null)
                && bodyAllowed
                && !fixedLength;

        writeLine(response.getVersion() + " " + statusCode.getCode() + " " + statusCode.getMessage());

        // headers
        if (chunked) {
            response.addHeader("Transfer-Encoding", "chunked");
        } else if (bodyAllowed && !fixedLength) {
            response.addHeader("Content-Length", "0");
        }
        for (HttpHeader header : response.getHeaders().values()) {
            writeLine(header.getKey() + ": " + header.getValue());
        }
        writeLine();

        // body
        if (bodyAllowed && fixedLength) {
            writeFixedLengthBody(body, contentLength);
        } else if (bodyAllowed && streamWriter != null) {
            outputStream.flush();
            try (ChunkedOutputStream chunkedOut =
                         new ChunkedOutputStream(outputStream)) {
                streamWriter.write(chunkedOut);
            }
        } else if (chunked) {

            while (true) {
                int read = body.read(byteBuffer);
                if (read == -1) break;
                if (read == 0) continue;
                writeLine(Integer.toHexString(read));
                outputStream.write(byteBuffer, 0, read);
                writeLine();
                if (response.isFlushAfterEachChunk()) {
                    outputStream.flush();
                }
            }

            writeLine(Integer.toHexString(0));
            writeLine();
        }

        outputStream.flush();
    }

    private static long parseContentLength(String value) throws IOException {
        try {
            long parsed = Long.parseLong(value.trim());
            if (parsed < 0) throw new NumberFormatException("negative");
            return parsed;
        } catch (NumberFormatException e) {
            throw new IOException("Invalid response Content-Length: " + value, e);
        }
    }

    private void writeFixedLengthBody(InputStream body, long contentLength)
            throws IOException {
        if (body == null) {
            if (contentLength == 0) return;
            throw new EOFException("Response body is shorter than Content-Length");
        }

        long remaining = contentLength;
        while (remaining > 0) {
            int read = body.read(
                    byteBuffer,
                    0,
                    (int) Math.min(byteBuffer.length, remaining)
            );
            if (read == -1) {
                throw new EOFException("Response body is shorter than Content-Length");
            }
            if (read == 0) continue;
            outputStream.write(byteBuffer, 0, read);
            remaining -= read;
        }

        if (body.read() != -1) {
            throw new IOException("Response body is longer than Content-Length");
        }
    }

    private void writeLine() throws IOException {
        outputStream.write(CRLF);
    }

    private void writeLine(String line) throws IOException {
        outputStream.write(line.getBytes(StandardCharsets.UTF_8));
        outputStream.write(CRLF);
    }

    @Override
    public void close() throws IOException {
        outputStream.close();
    }

}
