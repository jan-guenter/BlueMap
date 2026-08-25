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
package de.bluecolored.bluemap.common.config.storage;

import de.bluecolored.bluemap.common.config.ConfigurationException;
import de.bluecolored.bluemap.core.storage.sql.Database;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Field;
import java.util.HashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class SQLConfigTest {

    @Test
    void defaultsToABoundedSqlResponseBodyBudget() {
        SQLConfig config = new SQLConfig();

        assertEquals(
                Database.DEFAULT_MAX_IN_FLIGHT_RESPONSE_BYTES,
                config.getMaxInFlightResponseBytes()
        );
    }

    @Test
    void rejectsANonPositiveSqlResponseBodyBudget() throws Exception {
        SQLConfig config = new SQLConfig();
        setField(config, "maxInFlightResponseBytes", 0L);

        assertThrows(ConfigurationException.class, config::createStorage);
    }

    @Test
    void opensReadOnlySqliteConnectionsInReadOnlyMode() throws Exception {
        SQLConfig config = new SQLConfig();
        setField(config, "readOnly", true);
        setField(
                config,
                "connectionProperties",
                new HashMap<>(Map.of("open_mode", "70", "busy_timeout", "5"))
        );

        Map<String, String> effective =
                config.effectiveConnectionProperties(Dialect.SQLITE);

        assertEquals("1", effective.get("open_mode"));
        assertEquals("5", effective.get("busy_timeout"));
        assertEquals("70", config.getConnectionProperties().get("open_mode"));
    }

    @Test
    void leavesNetworkSqlConnectionPropertiesUntouched() throws Exception {
        SQLConfig config = new SQLConfig();
        setField(config, "readOnly", true);
        setField(
                config,
                "connectionProperties",
                new HashMap<>(Map.of("sslmode", "require"))
        );

        assertEquals(
                Map.of("sslmode", "require"),
                config.effectiveConnectionProperties(Dialect.POSTGRESQL)
        );
    }

    private static void setField(Object target, String name, Object value)
            throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }

}
