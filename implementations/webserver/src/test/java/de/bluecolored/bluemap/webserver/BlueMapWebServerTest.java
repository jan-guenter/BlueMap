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
package de.bluecolored.bluemap.webserver;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class BlueMapWebServerTest {

    @TempDir
    Path tempDir;

    @Test
    void parsesDefaults() {
        BlueMapWebServer.Options options = BlueMapWebServer.Options.parse(new String[0]);

        assertEquals(Path.of("config"), options.configFolder());
        assertFalse(options.verbose());
        assertFalse(options.version());
        assertFalse(options.help());
    }

    @Test
    void parsesAllOptions() {
        BlueMapWebServer.Options options = BlueMapWebServer.Options.parse(new String[]{
                "--config", "/etc/bluemap",
                "--verbose",
                "--version",
                "--help"
        });

        assertEquals(Path.of("/etc/bluemap"), options.configFolder());
        assertTrue(options.verbose());
        assertTrue(options.version());
        assertTrue(options.help());
    }

    @Test
    void rejectsUnknownAndIncompleteOptions() {
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--unknown"})
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> BlueMapWebServer.Options.parse(new String[]{"--config"})
        );
    }

    @Test
    void startsWithFileStorageAndAWebOnlyMap() throws Exception {
        Path config = Files.createDirectories(tempDir.resolve("config"));
        Files.createDirectories(config.resolve("maps"));
        Files.createDirectories(config.resolve("storages"));

        String data = tempDir.resolve("data").toString().replace('\\', '/');
        String web = tempDir.resolve("web").toString().replace('\\', '/');
        String maps = tempDir.resolve("maps").toString().replace('\\', '/');

        Files.writeString(config.resolve("core.conf"), """
                accept-download: false
                data: "%s"
                render-thread-count: 1
                scan-for-mod-resources: false
                log: {}
                """.formatted(data));
        Files.writeString(config.resolve("webapp.conf"), """
                enabled: true
                webroot: "%s"
                update-settings-file: true
                """.formatted(web));
        Files.writeString(config.resolve("webserver.conf"), """
                enabled: true
                webroot: "%s"
                ip: "127.0.0.1"
                port: 0
                sse-enabled: false
                log: {}
                """.formatted(web));
        Files.writeString(config.resolve("storages/file.conf"), """
                storage-type: file
                root: "%s"
                compression: gzip
                """.formatted(maps));
        Files.writeString(config.resolve("maps/world.conf"), """
                name: "World"
                storage: "file"
                """);

        BlueMapWebServer server = BlueMapWebServer.create(config, false);
        assertFalse(server.isReady());
        try (server) {
            server.start();
            assertTrue(server.isReady());
        }
        assertFalse(server.isReady());

        assertTrue(Files.isRegularFile(Path.of(web).resolve("index.html")));
        assertTrue(Files.isRegularFile(Path.of(web).resolve("settings.json")));
    }
}
