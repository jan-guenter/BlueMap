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
    public static final long DEFAULT_MAX_IN_FLIGHT_RESPONSE_BYTES =
            64L * 1024L * 1024L;

    private final DataSource dataSource;
    private final int maxPoolSize;
    private final int maxConcurrentReads;
    private final long maxInFlightResponseBytes;
    private final boolean readOnly;
    private final @Nullable Semaphore readPermits;
    @Getter(AccessLevel.NONE)
    private final Object responsePermitLock = new Object();
    @Getter(AccessLevel.NONE)
    private long inFlightResponseBytes;
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
        this(dataSource, maxPoolSize, false);
    }

    public Database(DataSource dataSource, int maxPoolSize, boolean readOnly) {
        this(
                dataSource,
                maxPoolSize,
                DEFAULT_MAX_IN_FLIGHT_RESPONSE_BYTES,
                readOnly
        );
    }

    public Database(
            DataSource dataSource,
            int maxPoolSize,
            long maxInFlightResponseBytes,
            boolean readOnly
    ) {
        this(
                dataSource,
                maxPoolSize,
                maxInFlightResponseBytes,
                readOnly,
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
        this(
                dataSource,
                maxPoolSize,
                DEFAULT_MAX_IN_FLIGHT_RESPONSE_BYTES,
                false,
                nanoTime,
                healthStaleAfter
        );
    }

    Database(
            DataSource dataSource,
            int maxPoolSize,
            boolean readOnly,
            LongSupplier nanoTime,
            Duration healthStaleAfter
    ) {
        this(
                dataSource,
                maxPoolSize,
                DEFAULT_MAX_IN_FLIGHT_RESPONSE_BYTES,
                readOnly,
                nanoTime,
                healthStaleAfter
        );
    }

    Database(
            DataSource dataSource,
            int maxPoolSize,
            long maxInFlightResponseBytes,
            boolean readOnly,
            LongSupplier nanoTime,
            Duration healthStaleAfter
    ) {
        this.dataSource = dataSource;
        this.maxPoolSize = maxPoolSize;
        this.maxConcurrentReads = maxPoolSize;
        if (maxInFlightResponseBytes <= 0) {
            throw new IllegalArgumentException(
                    "maxInFlightResponseBytes must be positive"
            );
        }
        this.maxInFlightResponseBytes = maxInFlightResponseBytes;
        this.readOnly = readOnly;
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
        this(url, properties, maxPoolSize, false);
    }

    public Database(
            String url,
            Map<String, String> properties,
            int maxPoolSize,
            boolean readOnly
    ) {
        this(
                url,
                properties,
                maxPoolSize,
                DEFAULT_MAX_IN_FLIGHT_RESPONSE_BYTES,
                readOnly
        );
    }

    public Database(
            String url,
            Map<String, String> properties,
            int maxPoolSize,
            long maxInFlightResponseBytes,
            boolean readOnly
    ) {
        this(createDataSource(
                new DriverManagerConnectionFactory(url, properties(properties)),
                maxPoolSize,
                readOnly
        ), maxPoolSize, maxInFlightResponseBytes, readOnly);
    }

    public Database(String url, Map<String, String> properties, int maxPoolSize, Driver driver) {
        this(url, properties, maxPoolSize, driver, false);
    }

    public Database(
            String url,
            Map<String, String> properties,
            int maxPoolSize,
            Driver driver,
            boolean readOnly
    ) {
        this(
                url,
                properties,
                maxPoolSize,
                driver,
                DEFAULT_MAX_IN_FLIGHT_RESPONSE_BYTES,
                readOnly
        );
    }

    public Database(
            String url,
            Map<String, String> properties,
            int maxPoolSize,
            Driver driver,
            long maxInFlightResponseBytes,
            boolean readOnly
    ) {
        this(createDataSource(
                new DriverConnectionFactory(driver, url, properties(properties)),
                maxPoolSize,
                readOnly
        ), maxPoolSize, maxInFlightResponseBytes, readOnly);
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

    public @Nullable ResponsePermit tryAcquireResponsePermit(
            long contentLength
    ) {
        // Unknown legacy/custom-schema lengths reserve the entire budget. This
        // admits one compatible response while preventing another BLOB from
        // being materialized before the actual length is known.
        long reservation = contentLength < 0
                ? maxInFlightResponseBytes
                : contentLength;
        synchronized (responsePermitLock) {
            if (reservation > maxInFlightResponseBytes) {
                // Preserve compatibility for a pre-existing large object while
                // ensuring that it is the only retained SQL response.
                if (inFlightResponseBytes != 0) return null;
            } else if (inFlightResponseBytes
                    > maxInFlightResponseBytes - reservation) {
                return null;
            }
            inFlightResponseBytes += reservation;
        }
        return new ResponsePermit(reservation);
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
                        if (readOnly && !connection.isReadOnly()) {
                            connection.setReadOnly(true);
                        }
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
            int maxPoolSize,
            boolean readOnly
    ) {
        PoolableConnectionFactory poolableConnectionFactory =
                new PoolableConnectionFactory(() -> {
                    Logger.global.logDebug("Creating new SQL-Connection...");
                    return connectionFactory.createConnection();
                }, null);
        poolableConnectionFactory.setPoolStatements(true);
        poolableConnectionFactory.setMaxOpenPreparedStatements(20);
        poolableConnectionFactory.setDefaultAutoCommit(false);
        poolableConnectionFactory.setDefaultReadOnly(readOnly);
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

    public final class ResponsePermit implements AutoCloseable {

        private long reservation;
        private final AtomicBoolean released = new AtomicBoolean();

        private ResponsePermit(long reservation) {
            this.reservation = reservation;
        }

        public boolean tryResize(long contentLength) {
            if (contentLength < 0) return false;
            synchronized (responsePermitLock) {
                if (released.get()) return false;

                long otherReservations =
                        inFlightResponseBytes - reservation;
                if (contentLength > maxInFlightResponseBytes) {
                    if (otherReservations != 0) return false;
                } else if (otherReservations
                        > maxInFlightResponseBytes - contentLength) {
                    return false;
                }

                inFlightResponseBytes =
                        otherReservations + contentLength;
                reservation = contentLength;
                return true;
            }
        }

        @Override
        public void close() {
            if (!released.compareAndSet(false, true)) return;
            synchronized (responsePermitLock) {
                inFlightResponseBytes -= reservation;
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
