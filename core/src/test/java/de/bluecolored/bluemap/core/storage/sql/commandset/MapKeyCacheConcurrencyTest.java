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
package de.bluecolored.bluemap.core.storage.sql.commandset;

import de.bluecolored.bluemap.core.storage.sql.Database;
import org.junit.jupiter.api.Test;
import org.sqlite.SQLiteDataSource;

import java.io.IOException;
import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MapKeyCacheConcurrencyTest {

    @Test
    void unrelatedMapMissesDoNotShareAJdbcCriticalSection() throws Exception {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite::memory:");
        CountDownLatch blockedLookupStarted = new CountDownLatch(1);
        CountDownLatch releaseBlockedLookup = new CountDownLatch(1);

        try (Database database = new Database(dataSource, -1);
             BlockingLookupCommandSet commands = new BlockingLookupCommandSet(
                     database, blockedLookupStarted, releaseBlockedLookup
             );
             ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor()) {
            Future<Integer> blocked = executor.submit(
                    () -> commands.lookup("blocked")
            );
            assertTrue(blockedLookupStarted.await(2, TimeUnit.SECONDS));

            Future<Integer> independent = executor.submit(
                    () -> commands.lookup("independent")
            );
            assertEquals(2, independent.get(1, TimeUnit.SECONDS));

            releaseBlockedLookup.countDown();
            assertEquals(1, blocked.get(1, TimeUnit.SECONDS));
        } finally {
            releaseBlockedLookup.countDown();
        }
    }

    private static final class BlockingLookupCommandSet
            extends SqliteCommandSet {

        private final CountDownLatch blockedLookupStarted;
        private final CountDownLatch releaseBlockedLookup;

        private BlockingLookupCommandSet(
                Database database,
                CountDownLatch blockedLookupStarted,
                CountDownLatch releaseBlockedLookup
        ) {
            super(database);
            this.blockedLookupStarted = blockedLookupStarted;
            this.releaseBlockedLookup = releaseBlockedLookup;
        }

        private Integer lookup(String mapId) throws IOException {
            return findMapKey(mapId);
        }

        @Override
        protected Integer loadMapKey(String mapId) throws IOException {
            if (!mapId.equals("blocked")) return 2;

            blockedLookupStarted.countDown();
            try {
                if (!releaseBlockedLookup.await(
                        Duration.ofSeconds(2).toMillis(), TimeUnit.MILLISECONDS
                )) {
                    throw new IOException("timed out waiting to release lookup");
                }
            } catch (InterruptedException ex) {
                Thread.currentThread().interrupt();
                throw new IOException("interrupted while waiting to release lookup", ex);
            }
            return 1;
        }

    }

}
