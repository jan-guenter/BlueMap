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

import de.bluecolored.bluemap.common.BlueMapService;
import de.bluecolored.bluemap.common.config.BlueMapConfigManager;
import de.bluecolored.bluemap.common.config.ConfigurationException;
import de.bluecolored.bluemap.common.config.CoreConfig;
import de.bluecolored.bluemap.common.config.CoreConfig.LogConfig;
import de.bluecolored.bluemap.common.config.MapConfig;
import de.bluecolored.bluemap.common.config.WebserverConfig;
import de.bluecolored.bluemap.common.web.BlueMapResponseModifier;
import de.bluecolored.bluemap.common.web.FileRequestHandler;
import de.bluecolored.bluemap.common.web.LoggingRequestHandler;
import de.bluecolored.bluemap.common.web.MapRequestHandler;
import de.bluecolored.bluemap.common.web.RoutingRequestHandler;
import de.bluecolored.bluemap.common.web.http.HttpResponse;
import de.bluecolored.bluemap.common.web.http.HttpRequestHandler;
import de.bluecolored.bluemap.common.web.http.HttpServer;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;
import de.bluecolored.bluemap.core.BlueMap;
import de.bluecolored.bluemap.core.logger.Logger;
import de.bluecolored.bluemap.core.storage.MapStorage;
import de.bluecolored.bluemap.core.util.FileHelper;
import de.bluecolored.bluemap.webserver.metrics.ConnectionMetricsEndpoint;
import org.jetbrains.annotations.Nullable;

import java.io.Closeable;
import java.io.IOException;
import java.net.BindException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.UnknownHostException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

public final class BlueMapWebServer implements Closeable {

    private static final Duration EXECUTOR_STOP_TIMEOUT =
            Duration.ofSeconds(5);

    private static final String DEFAULT_CONFIG_FOLDER = "config";
    private static final String DEFAULT_METRICS_IP = "127.0.0.1";

    private final BlueMapService blueMap;
    private final HttpServer webServer;
    private final @Nullable ConnectionMetricsEndpoint metricsEndpoint;
    private final ExecutorService connectionExecutor;
    private final Logger webLogger;
    private final Path webRoot;
    private final AtomicBoolean started;
    private final Duration shutdownGracePeriod;

    private BlueMapWebServer(
            BlueMapService blueMap,
            HttpServer webServer,
            @Nullable ConnectionMetricsEndpoint metricsEndpoint,
            ExecutorService connectionExecutor,
            Logger webLogger,
            Path webRoot,
            AtomicBoolean started,
            Duration shutdownGracePeriod
    ) {
        this.blueMap = blueMap;
        this.webServer = webServer;
        this.metricsEndpoint = metricsEndpoint;
        this.connectionExecutor = connectionExecutor;
        this.webLogger = webLogger;
        this.webRoot = webRoot;
        this.started = started;
        this.shutdownGracePeriod = shutdownGracePeriod;
    }

    public static BlueMapWebServer create(Path configFolder, boolean verbose)
            throws IOException, ConfigurationException, InterruptedException {
        return create(configFolder, verbose, null);
    }

    static BlueMapWebServer create(
            Path configFolder,
            boolean verbose,
            @Nullable Integer metricsPort
    ) throws IOException, ConfigurationException, InterruptedException {
        return create(
                configFolder,
                verbose,
                metricsPort,
                DEFAULT_METRICS_IP
        );
    }

    static BlueMapWebServer create(
            Path configFolder,
            boolean verbose,
            @Nullable Integer metricsPort,
            String metricsIp
    ) throws IOException, ConfigurationException, InterruptedException {
        BlueMapService blueMap = null;
        HttpServer webServer = null;
        ConnectionMetricsEndpoint metricsEndpoint = null;
        ExecutorService connectionExecutor = null;
        Logger webLogger = null;
        AtomicBoolean started = new AtomicBoolean();

        try {
            BlueMapConfigManager configs = BlueMapConfigManager.builder()
                    .configRoot(configFolder)
                    .usePluginConfig(false)
                    .useMetricsConfig(false)
                    .isCli(true)
                    .defaultDataFolder(Path.of("data"))
                    .defaultWebroot(Path.of("web"))
                    .build();

            configureCoreLogger(configs.getCoreConfig());

            blueMap = new BlueMapService(configs);
            blueMap.createOrUpdateWebApp(false);

            WebserverConfig config = configs.getWebserverConfig();
            if (!config.isEnabled()) {
                throw new ConfigurationException(
                        "The standalone BlueMap webserver is disabled in webserver.conf."
                );
            }

            FileHelper.createDirectories(config.getWebroot());

            RoutingRequestHandler routes = new RoutingRequestHandler();
            routes.register(".*", new FileRequestHandler(config.getWebroot()));

            for (var mapConfigEntry : configs.getMapConfigs().entrySet()) {
                String mapId = mapConfigEntry.getKey();
                MapConfig mapConfig = mapConfigEntry.getValue();
                MapStorage storage = blueMap.getOrLoadStorage(mapConfig.getStorage()).map(mapId);

                routes.register(
                        "maps/" + Pattern.quote(mapId) + "/(.*)",
                        "$1",
                        new MapRequestHandler(storage, config.getTileCacheMaxAge())
                );
            }

            BlueMapService initializedBlueMap = blueMap;
            Path webRoot = config.getWebroot();
            routes.register("health/live", _ -> healthResponse(true));
            routes.register(
                    "health/ready",
                    _ -> healthResponse(isReady(initializedBlueMap, webRoot, started))
            );

            List<Logger> webLoggers = new ArrayList<>();
            if (verbose) webLoggers.add(Logger.stdOut(true));
            if (config.getLog().getFile() != null) {
                ZonedDateTime timestamp = ZonedDateTime.ofInstant(Instant.now(), ZoneId.systemDefault());
                webLoggers.add(Logger.file(
                        Path.of(String.format(config.getLog().getFile(), timestamp)),
                        config.getLog().isAppend()
                ));
            }
            webLogger = Logger.combine(webLoggers);

            HttpRequestHandler requestHandler = new BlueMapResponseModifier(routes);
            requestHandler = LoggingRequestHandler.wrap(
                    requestHandler,
                    config.getLog().getFormat(),
                    webLogger,
                    !webLoggers.isEmpty()
            );

            connectionExecutor = Executors.newVirtualThreadPerTaskExecutor();
            webServer = new HttpServer(
                    "BlueMap-Webserver",
                    requestHandler,
                    connectionExecutor,
                    config.createHttpServerSettings()
            );

            try {
                var bindAddress = config.resolveIp();
                webServer.bind(new InetSocketAddress(bindAddress, config.getPort()));
                if (metricsPort != null) {
                    if (metricsPort == config.getPort()) {
                        throw new ConfigurationException(
                                "The metrics port must differ from the public webserver port."
                        );
                    }
                    InetAddress metricsBindAddress;
                    try {
                        metricsBindAddress = InetAddress.getByName(metricsIp);
                    } catch (UnknownHostException ex) {
                        throw new ConfigurationException(
                                "BlueMap failed to resolve the metrics bind address.",
                                ex
                        );
                    }
                    metricsEndpoint = new ConnectionMetricsEndpoint(
                            webServer,
                            config.getMaxActiveConnections(),
                            metricsBindAddress,
                            metricsPort
                    );
                }
            } catch (UnknownHostException ex) {
                throw new ConfigurationException(
                        "BlueMap failed to resolve the ip in webserver.conf.",
                        ex
                );
            } catch (BindException ex) {
                throw new ConfigurationException(
                        "BlueMap failed to bind a configured webserver or metrics port.",
                        ex
                );
            }

            return new BlueMapWebServer(
                    blueMap,
                    webServer,
                    metricsEndpoint,
                    connectionExecutor,
                    webLogger,
                    webRoot,
                    started,
                    Duration.ofSeconds(
                            config.getShutdownGracePeriodSeconds()
                    )
            );
        } catch (IOException | ConfigurationException | InterruptedException | RuntimeException ex) {
            closeAfterFailedCreate(
                    blueMap,
                    webServer,
                    metricsEndpoint,
                    connectionExecutor,
                    webLogger,
                    ex
            );
            throw ex;
        }
    }

    private static void configureCoreLogger(CoreConfig coreConfig) throws IOException {
        LogConfig log = coreConfig.getLog();
        if (log.getFile() == null) return;

        ZonedDateTime timestamp = ZonedDateTime.ofInstant(Instant.now(), ZoneId.systemDefault());
        Logger.global.put(Logger.file(
                Path.of(String.format(log.getFile(), timestamp)),
                log.isAppend()
        ));
    }

    private static HttpResponse healthResponse(boolean healthy) {
        HttpResponse response = new HttpResponse(
                healthy ? HttpStatusCode.OK : HttpStatusCode.SERVICE_UNAVAILABLE
        );
        response.addHeader("Content-Type", "text/plain; charset=utf-8");
        response.addHeader("Cache-Control", "no-store,no-transform");
        response.setBody(healthy ? "ok\n" : "not ready\n");
        return response;
    }

    private static boolean isReady(BlueMapService blueMap, Path webRoot, AtomicBoolean started) {
        return started.get()
                && Files.isRegularFile(webRoot.resolve("index.html"))
                && Files.isRegularFile(webRoot.resolve("settings.json"))
                && blueMap.getLoadedStorages().values().stream()
                .allMatch(storage -> storage.isHealthy());
    }

    boolean isReady() {
        return isReady(blueMap, webRoot, started);
    }

    private static void closeAfterFailedCreate(
            BlueMapService blueMap,
            HttpServer webServer,
            ConnectionMetricsEndpoint metricsEndpoint,
            ExecutorService connectionExecutor,
            Logger webLogger,
            Exception original
    ) {
        if (metricsEndpoint != null) {
            try {
                metricsEndpoint.close();
            } catch (IOException closeException) {
                original.addSuppressed(closeException);
            }
        }
        if (webServer != null) {
            try {
                webServer.close();
            } catch (IOException closeException) {
                original.addSuppressed(closeException);
            }
        }
        if (connectionExecutor != null) connectionExecutor.shutdownNow();

        if (blueMap != null) {
            try {
                blueMap.close();
            } catch (Exception closeException) {
                original.addSuppressed(closeException);
            }
        }

        if (webLogger != null) {
            try {
                webLogger.close();
            } catch (Exception closeException) {
                original.addSuppressed(closeException);
            }
        }
    }

    public void start() {
        webServer.start();
        if (metricsEndpoint != null) metricsEndpoint.start();
        started.set(true);
    }

    public void awaitTermination() throws InterruptedException {
        webServer.join();
    }

    @Override
    public void close() throws IOException {
        IOException exception = null;
        started.set(false);
        boolean drained = false;

        if (metricsEndpoint != null) {
            try {
                metricsEndpoint.close();
            } catch (IOException ex) {
                exception = ex;
            }
        }

        try {
            drained = webServer.closeGracefully(shutdownGracePeriod);
        } catch (IOException ex) {
            if (exception == null) exception = ex;
            else exception.addSuppressed(ex);
        }

        boolean executorTerminated;
        try {
            executorTerminated = stopConnectionExecutor(
                    connectionExecutor,
                    !drained,
                    executorStopTimeout(shutdownGracePeriod)
            );
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            IOException interrupted = new IOException(
                    "Interrupted while waiting for HTTP requests to stop",
                    ex
            );
            if (exception == null) exception = interrupted;
            else exception.addSuppressed(interrupted);
            executorTerminated = false;
        }

        if (!executorTerminated) {
            IOException timeout = new IOException(
                    "HTTP request tasks did not stop before the shutdown timeout; "
                            + "BlueMap storage remains open"
            );
            if (exception == null) exception = timeout;
            else exception.addSuppressed(timeout);
            throw exception;
        }

        try {
            blueMap.close();
        } catch (IOException ex) {
            if (exception == null) exception = ex;
            else exception.addSuppressed(ex);
        }

        try {
            webLogger.close();
        } catch (Exception ex) {
            if (exception == null) exception = new IOException(ex);
            else exception.addSuppressed(ex);
        }

        if (exception != null) throw exception;
    }

    static boolean stopConnectionExecutor(
            ExecutorService executor,
            boolean force,
            Duration timeout
    ) throws InterruptedException {
        executor.shutdown();
        if (force) executor.shutdownNow();
        return executor.awaitTermination(
                timeout.toNanos(),
                TimeUnit.NANOSECONDS
        );
    }

    static Duration executorStopTimeout(Duration shutdownGracePeriod) {
        return shutdownGracePeriod.compareTo(EXECUTOR_STOP_TIMEOUT) < 0
                ? EXECUTOR_STOP_TIMEOUT
                : shutdownGracePeriod;
    }

    public static void main(String[] args) {
        Options options;
        try {
            options = Options.parse(args);
        } catch (IllegalArgumentException ex) {
            Logger.global.logError(ex.getMessage(), null);
            printUsage();
            System.exit(1);
            return;
        }

        if (options.help()) {
            printUsage();
            return;
        }

        if (options.version()) {
            System.out.println(BlueMap.VERSION);
            return;
        }

        try {
            BlueMapWebServer server = create(
                    options.configFolder(),
                    options.verbose(),
                    options.metricsPort(),
                    options.metricsIp()
            );
            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                try {
                    server.close();
                } catch (IOException ex) {
                    Logger.global.logError("Failed to stop the BlueMap webserver cleanly.", ex);
                }
            }, "BlueMap-Webserver-Shutdown"));

            server.start();
            server.awaitTermination();
        } catch (ConfigurationException ex) {
            ex.printLog(Logger.global);
            System.exit(1);
        } catch (IOException ex) {
            Logger.global.logError("Failed to start the BlueMap webserver.", ex);
            System.exit(1);
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
        } catch (RuntimeException ex) {
            Logger.global.logError("An unexpected error occurred.", ex);
            System.exit(1);
        }
    }

    private static void printUsage() {
        System.out.println("""
                Usage: java -jar bluemap-webserver.jar [options]

                  -c, --config <folder>  BlueMap configuration folder (default: config)
                  -b, --verbose          Log HTTP requests to stdout
                      --metrics-port <n> Start a separate OpenMetrics listener
                      --metrics-ip <ip>  Metrics bind address (default: 127.0.0.1)
                  -V, --version          Print the BlueMap version
                  -h, --help             Show this help
                """);
    }

    record Options(
            Path configFolder,
            boolean verbose,
            @Nullable Integer metricsPort,
            String metricsIp,
            boolean version,
            boolean help
    ) {

        static Options parse(String[] args) {
            Path configFolder = Path.of(DEFAULT_CONFIG_FOLDER);
            boolean verbose = false;
            Integer metricsPort = null;
            String metricsIp = DEFAULT_METRICS_IP;
            boolean metricsIpConfigured = false;
            boolean version = false;
            boolean help = false;

            for (int i = 0; i < args.length; i++) {
                switch (args[i]) {
                    case "-c", "--config" -> {
                        if (++i >= args.length) {
                            throw new IllegalArgumentException("Missing folder after " + args[i - 1] + ".");
                        }
                        configFolder = Path.of(args[i]);
                    }
                    case "-b", "--verbose" -> verbose = true;
                    case "--metrics-port" -> {
                        if (++i >= args.length) {
                            throw new IllegalArgumentException(
                                    "Missing port after " + args[i - 1] + "."
                            );
                        }
                        try {
                            metricsPort = Integer.parseInt(args[i]);
                        } catch (NumberFormatException ex) {
                            throw new IllegalArgumentException(
                                    "Invalid metrics port: " + args[i], ex
                            );
                        }
                        if (metricsPort < 1 || metricsPort > 65535) {
                            throw new IllegalArgumentException(
                                    "Metrics port must be between 1 and 65535."
                            );
                        }
                    }
                    case "--metrics-ip" -> {
                        if (++i >= args.length) {
                            throw new IllegalArgumentException(
                                    "Missing address after " + args[i - 1] + "."
                            );
                        }
                        metricsIp = args[i];
                        metricsIpConfigured = true;
                    }
                    case "-V", "--version" -> version = true;
                    case "-h", "--help" -> help = true;
                    default -> throw new IllegalArgumentException("Unknown option: " + args[i]);
                }
            }

            if (metricsIpConfigured && metricsPort == null) {
                throw new IllegalArgumentException(
                        "--metrics-ip requires --metrics-port."
                );
            }

            return new Options(
                    configFolder,
                    verbose,
                    metricsPort,
                    metricsIp,
                    version,
                    help
            );
        }
    }
}
