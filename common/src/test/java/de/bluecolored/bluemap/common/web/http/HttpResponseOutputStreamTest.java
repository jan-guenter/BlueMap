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

import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpResponseOutputStreamTest {

    @Test
    void doesNotWriteBodyFramingForHead() throws Exception {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.setBody("payload");
        response.setBodySuppressed(true);

        String wire = write(response);

        assertFalse(wire.contains("Transfer-Encoding"));
        assertFalse(wire.contains("Content-Length: 0"));
        assertFalse(wire.contains("payload"));
    }

    @Test
    void doesNotWriteBodyFramingForNoContentOrNotModified() throws Exception {
        for (HttpStatusCode status : new HttpStatusCode[]{
                HttpStatusCode.NO_CONTENT,
                HttpStatusCode.NOT_MODIFIED
        }) {
            HttpResponse response = new HttpResponse(status);
            response.setBody("payload");

            String wire = write(response);

            assertFalse(wire.contains("Transfer-Encoding"));
            assertFalse(wire.contains("Content-Length: 0"));
            assertFalse(wire.contains("payload"));
        }
    }

    @Test
    void retainsZeroLengthForOrdinaryEmptyResponses() throws Exception {
        HttpResponse response = new HttpResponse(HttpStatusCode.NOT_FOUND);

        assertTrue(write(response).contains("Content-Length: 0"));
    }

    @Test
    void writesDeclaredFixedLengthBodiesWithoutChunkFraming() throws Exception {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.addHeader("Content-Length", "7");
        response.setBody("payload");

        String wire = write(response);

        assertTrue(wire.contains("Content-Length: 7"));
        assertFalse(wire.contains("Transfer-Encoding"));
        assertTrue(wire.endsWith("\r\n\r\npayload"));
    }

    @Test
    void failsWhenAFixedLengthBodyIsShort() {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.addHeader("Content-Length", "8");
        response.setBody("payload");

        assertThrows(IOException.class, () -> write(response));
    }

    @Test
    void doesNotReadPastTheDeclaredFixedLength() throws Exception {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.addHeader("Content-Length", "7");
        response.setBody(new InputStream() {
            private final byte[] data = "payload".getBytes(StandardCharsets.UTF_8);
            private int offset;

            @Override
            public int read(byte[] bytes, int targetOffset, int length) {
                if (offset == data.length) {
                    throw new AssertionError("read past Content-Length");
                }
                int copied = Math.min(length, data.length - offset);
                System.arraycopy(data, offset, bytes, targetOffset, copied);
                offset += copied;
                return copied;
            }

            @Override
            public int read() {
                throw new AssertionError("unexpected single-byte read");
            }
        });
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        HttpResponseOutputStream output = new HttpResponseOutputStream(bytes);

        output.write(response);
        assertTrue(bytes.toString(StandardCharsets.UTF_8).endsWith("\r\n\r\npayload"));
    }

    @Test
    void usesLargeCopyBufferAndFlushesNormalResponsesOnce() throws Exception {
        CountingOutputStream output = new CountingOutputStream();
        HttpResponse response = responseWithChunks(2);

        new HttpResponseOutputStream(output).write(response);

        assertEquals(64 * 1024, HttpResponseOutputStream.COPY_BUFFER_SIZE);
        assertEquals(1, output.flushCount);
    }

    @Test
    void flushesEveryChunkForExplicitStreamingResponses() throws Exception {
        CountingOutputStream output = new CountingOutputStream();
        HttpResponse response = responseWithChunks(2);
        response.setFlushAfterEachChunk(true);

        new HttpResponseOutputStream(output).write(response);

        assertTrue(output.flushCount >= 3);
    }

    @Test
    void writesDirectStreamWritersWithChunkFraming() throws Exception {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.setBody(out -> {
            out.write("event".getBytes(StandardCharsets.UTF_8));
            out.flush();
        });

        String wire = write(response);

        assertTrue(wire.contains("Transfer-Encoding: chunked\r\n"));
        assertTrue(wire.endsWith("5\r\nevent\r\n0\r\n\r\n"));
    }

    @Test
    void doesNotInvokeDirectStreamWritersForHead() throws Exception {
        AtomicBoolean invoked = new AtomicBoolean();
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.setBody(out -> invoked.set(true));
        response.setBodySuppressed(true);

        String wire = write(response);

        assertFalse(invoked.get());
        assertFalse(wire.contains("Transfer-Encoding"));
    }

    @Test
    void rejectsContentLengthOnDirectStreamWriters() {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.addHeader("Content-Length", "5");
        response.setBody(out -> out.write("event".getBytes(StandardCharsets.UTF_8)));

        assertThrows(IOException.class, () -> write(response));
    }

    private static HttpResponse responseWithChunks(int chunks) {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.setBody(new byte[HttpResponseOutputStream.COPY_BUFFER_SIZE * chunks]);
        return response;
    }

    private static String write(HttpResponse response) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        new HttpResponseOutputStream(bytes).write(response);
        return bytes.toString(StandardCharsets.UTF_8);
    }

    private static class CountingOutputStream extends ByteArrayOutputStream {

        private int flushCount;

        @Override
        public void flush() {
            flushCount++;
        }

    }

}
