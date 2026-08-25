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

import java.time.Duration;
import java.util.Objects;

public record HttpServerSettings(
        int maxActiveConnections,
        int maxLongLivedConnections,
        Duration idleTimeout,
        HttpRequestLimits requestLimits
) {

    public static final HttpServerSettings DEFAULT = new HttpServerSettings(
            256,
            32,
            Duration.ofSeconds(60),
            HttpRequestLimits.DEFAULT
    );

    public HttpServerSettings(
            int maxActiveConnections,
            Duration idleTimeout,
            HttpRequestLimits requestLimits
    ) {
        this(
                maxActiveConnections,
                Math.min(32, maxActiveConnections),
                idleTimeout,
                requestLimits
        );
    }

    public HttpServerSettings {
        Objects.requireNonNull(idleTimeout, "idleTimeout");
        Objects.requireNonNull(requestLimits, "requestLimits");
        if (maxActiveConnections < 1) throw new IllegalArgumentException("maxActiveConnections must be positive");
        if (maxLongLivedConnections < 1
                || maxLongLivedConnections > maxActiveConnections) {
            throw new IllegalArgumentException(
                    "maxLongLivedConnections must be between 1 and maxActiveConnections"
            );
        }
        if (idleTimeout.isNegative() || idleTimeout.isZero()) {
            throw new IllegalArgumentException("idleTimeout must be positive");
        }
        if (idleTimeout.toMillis() > Integer.MAX_VALUE) {
            throw new IllegalArgumentException("idleTimeout is too large");
        }
    }

}
