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
package de.bluecolored.bluemap.common.plugin;

import de.bluecolored.bluemap.common.serverinterface.Player;
import de.bluecolored.bluemap.common.serverinterface.Server;
import de.bluecolored.bluemap.common.serverinterface.ServerEventListener;
import de.bluecolored.bluemap.common.serverinterface.ServerWorld;
import de.bluecolored.bluemap.common.web.http.HttpResponse;
import de.bluecolored.bluemap.common.web.http.HttpServer;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.lang.reflect.Field;
import java.nio.file.Path;
import java.util.Collection;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PluginLifecycleTest {

    @TempDir
    Path tempDir;

    @Test
    void failedWebShutdownAbortsReloadAndCanBeRetried() throws Exception {
        Plugin plugin = new Plugin("test", new TestServer(tempDir));
        FailingCloseHttpServer webServer = new FailingCloseHttpServer();
        setField(plugin, "webServer", webServer);
        setField(plugin, "loaded", true);

        assertThrows(IOException.class, plugin::reload);
        assertTrue(plugin.isLoaded());
        assertTrue(webServer.failedOnce);

        webServer.fail = false;
        plugin.unload();
        assertFalse(plugin.isLoaded());
    }

    private static void setField(Object instance, String name, Object value)
            throws Exception {
        Field field = instance.getClass().getDeclaredField(name);
        field.setAccessible(true);
        field.set(instance, value);
    }

    private static final class FailingCloseHttpServer extends HttpServer {

        private boolean fail = true;
        private boolean failedOnce;

        private FailingCloseHttpServer() throws IOException {
            super(
                    "test-webserver",
                    _ -> new HttpResponse(HttpStatusCode.OK)
            );
        }

        @Override
        public void close() throws IOException {
            if (fail) {
                failedOnce = true;
                throw new IOException("request task is still running");
            }
            super.close();
        }

    }

    private record TestServer(Path configFolder) implements Server {

        @Override
        public String getMinecraftVersion() {
            return null;
        }

        @Override
        public Path getConfigFolder() {
            return configFolder;
        }

        @Override
        public Optional<Path> getModsFolder() {
            return Optional.empty();
        }

        @Override
        public Collection<ServerWorld> getLoadedServerWorlds() {
            return java.util.List.of();
        }

        @Override
        public Map<UUID, Player> getOnlinePlayers() {
            return Map.of();
        }

        @Override
        public void registerListener(ServerEventListener listener) {
        }

        @Override
        public void unregisterAllListeners() {
        }

    }

}
