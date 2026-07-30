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
package de.bluecolored.bluemap.core.storage.sql;

import de.bluecolored.bluemap.core.logger.Logger;
import lombok.AccessLevel;
import lombok.Getter;
import org.jetbrains.annotations.Nullable;
import org.apache.commons.dbcp2.*;
import org.apache.commons.pool2.ObjectPool;
import org.apache.commons.pool2.impl.GenericObjectPool;
import org.apache.commons.pool2.impl.GenericObjectPoolConfig;

import javax.sql.DataSource;
import java.io.Closeable;
import java.io.IOException;
import java.sql.Connection;
import java.sql.Driver;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.SQLRecoverableException;
import java.sql.Statement;
import java.time.Duration;
import java.util.Map;
import java.util.Objects;
import java.util.Properties;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.LongSupplier;

@Getter
public class Database implements Closeable {

    private static final Duration HEALTH_STALE_AFTER = Duration.ofSeconds(10);

    private final DataSource dataSource;
    private final int maxPoolSize;
    private final int maxConcurrentReads;
    private final @Nullable Semaphore readPermits;
    @Getter(AccessLevel.NONE)
    private final ScheduledExecutorService healthExecutor;
    @Getter(AccessLevel.NONE)
    private final LongSupplier nanoTime;
    @Getter(AccessLevel.NONE)
    private final long healthStaleAfterNanos;
    @Getter(AccessLevel.NONE)
    private volatile long lastSuccessfulOperationNanos;
    private volatile boolean healthy = true;
    private volatile boolean isClosed = false;

    public Database(DataSource dataSource) {
        this(dataSource, -1);
    }

    public Database(DataSource dataSource, int maxPoolSize) {
        this(
                dataSource,
                maxPoolSize,
                System::nanoTime,
                HEALTH_STALE_AFTER
        );
    }

    Database(
            DataSource dataSource,
            int maxPoolSize,
            LongSupplier nanoTime,
            Duration healthStaleAfter
    ) {
        this.dataSource = dataSource;
        this.maxPoolSize = maxPoolSize;
        this.maxConcurrentReads = maxPoolSize;
        this.nanoTime = Objects.requireNonNull(nanoTime, "nanoTime");
        Objects.requireNonNull(healthStaleAfter, "healthStaleAfter");
        if (healthStaleAfter.isNegative() || healthStaleAfter.isZero()) {
            throw new IllegalArgumentException(
                    "healthStaleAfter must be positive"
            );
        }
        this.healthStaleAfterNanos = healthStaleAfter.toNanos();
        this.lastSuccessfulOperationNanos = nanoTime.getAsLong();
        this.readPermits =
                maxPoolSize > 0 ? new Semaphore(maxPoolSize, true) : null;
        this.healthExecutor = Executors.newSingleThreadScheduledExecutor(
                runnable -> {
                    Thread thread =
                            new Thread(runnable, "BlueMap-SQL-Health");
                    thread.setDaemon(true);
                    return thread;
                }
        );
        this.healthExecutor.scheduleWithFixedDelay(
                this::refreshHealth,
                1,
                2,
                TimeUnit.SECONDS
        );
    }

    public Database(String url, Map<String, String> properties, int maxPoolSize) {
        this(createDataSource(
                new DriverManagerConnectionFactory(url, properties(properties)),
                maxPoolSize
        ), maxPoolSize);
    }

    public Database(String url, Map<String, String> properties, int maxPoolSize, Driver driver) {
        this(createDataSource(
                new DriverConnectionFactory(driver, url, properties(properties)),
                maxPoolSize
        ), maxPoolSize);
    }

    private static Properties properties(Map<String, String> values) {
        Properties properties = new Properties();
        properties.putAll(values);
        return properties;
    }

    public @Nullable ReadPermit tryAcquireReadPermit() {
        if (readPermits == null) return new ReadPermit(false);
        return readPermits.tryAcquire() ? new ReadPermit(true) : null;
    }

    public void run(ConnectionConsumer action) throws IOException {
        run((ConnectionFunction<Void>) action);
    }

    public <R> R run(ConnectionFunction<R> action) throws IOException {
        SQLException sqlException = null;

        try {
            // try the action 2 times if a "recoverable" exception is thrown
            for (int i = 0; i < 2; i++) {
                try (Connection connection = dataSource.getConnection()) {
                    try {
                        R result = action.apply(connection);
                        connection.commit();
                        if (!isClosed) {
                            lastSuccessfulOperationNanos = nanoTime.getAsLong();
                            healthy = true;
                        }
                        return result;
                    } catch (SQLRecoverableException ex) {
                        if (sqlException == null) {
                            sqlException = ex;
                        } else {
                            sqlException.addSuppressed(ex);
                        }
                    }
                }
            }
        } catch (SQLException ex) {
            healthy = false;
            if (sqlException != null)
                ex.addSuppressed(sqlException);
            throw new IOException(ex);
        } catch (IOException | RuntimeException ex) {
            if (sqlException != null)
                ex.addSuppressed(sqlException);
            throw ex;
        }

        healthy = false;
        throw new IOException(sqlException);
    }

    public boolean isHealthy() {
        if (isClosed || !healthy) return false;
        long elapsed = nanoTime.getAsLong() - lastSuccessfulOperationNanos;
        return elapsed >= 0 && elapsed <= healthStaleAfterNanos;
    }

    void refreshHealth() {
        if (isClosed) {
            healthy = false;
            return;
        }

        try {
            run(connection -> {
                try (Statement statement = connection.createStatement();
                     ResultSet result = statement.executeQuery("SELECT 1")) {
                    if (!result.next() || result.getInt(1) != 1) {
                        throw new SQLException(
                                "SQL health check returned an unexpected result"
                        );
                    }
                }
            });
        } catch (IOException | RuntimeException e) {
            healthy = false;
        }
    }

    @Override
    public void close() throws IOException {
        isClosed = true;
        healthy = false;
        healthExecutor.shutdownNow();
        if (dataSource instanceof AutoCloseable closeable) {
            try {
                closeable.close();
            } catch (IOException ex) {
                throw ex;
            } catch (Exception ex) {
                throw new IOException("Failed to close datasource!", ex);
            }
        }
    }

    private static DataSource createDataSource(
            ConnectionFactory connectionFactory,
            int maxPoolSize
    ) {
        PoolableConnectionFactory poolableConnectionFactory =
                new PoolableConnectionFactory(() -> {
                    Logger.global.logDebug("Creating new SQL-Connection...");
                    return connectionFactory.createConnection();
                }, null);
        poolableConnectionFactory.setPoolStatements(true);
        poolableConnectionFactory.setMaxOpenPreparedStatements(20);
        poolableConnectionFactory.setDefaultAutoCommit(false);
        poolableConnectionFactory.setAutoCommitOnReturn(false);
        poolableConnectionFactory.setRollbackOnReturn(true);
        poolableConnectionFactory.setFastFailValidation(true);

        GenericObjectPoolConfig<PoolableConnection> objectPoolConfig = new GenericObjectPoolConfig<>();
        objectPoolConfig.setTestWhileIdle(true);
        objectPoolConfig.setTimeBetweenEvictionRuns(Duration.ofSeconds(10));
        objectPoolConfig.setNumTestsPerEvictionRun(3);
        objectPoolConfig.setBlockWhenExhausted(true);
        objectPoolConfig.setMinIdle(1);
        objectPoolConfig.setMaxIdle(Runtime.getRuntime().availableProcessors());
        objectPoolConfig.setMaxTotal(maxPoolSize);
        objectPoolConfig.setMaxWaitMillis(Duration.ofSeconds(30).toMillis());

        ObjectPool<PoolableConnection> connectionPool =
                new GenericObjectPool<>(poolableConnectionFactory, objectPoolConfig);
        poolableConnectionFactory.setPool(connectionPool);

        return new PoolingDataSource<>(connectionPool);
    }

    public final class ReadPermit implements AutoCloseable {

        private final boolean limited;
        private final AtomicBoolean released = new AtomicBoolean();

        private ReadPermit(boolean limited) {
            this.limited = limited;
        }

        @Override
        public void close() {
            if (limited && released.compareAndSet(false, true)) {
                Objects.requireNonNull(readPermits).release();
            }
        }

    }

    @FunctionalInterface
    public interface ConnectionConsumer extends ConnectionFunction<Void> {

        void accept(java.sql.Connection connection) throws SQLException, IOException;

        @Override
        default Void apply(java.sql.Connection connection) throws SQLException, IOException {
            accept(connection);
            return null;
        }

    }

    @FunctionalInterface
    public interface ConnectionFunction<R>  {

        R apply(java.sql.Connection connection) throws SQLException, IOException;

    }

}
