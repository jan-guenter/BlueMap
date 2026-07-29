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
import de.bluecolored.bluemap.common.web.http.HttpServer;
import de.bluecolored.bluemap.common.web.http.HttpStatusCode;
import de.bluecolored.bluemap.core.BlueMap;
import de.bluecolored.bluemap.core.logger.Logger;
import de.bluecolored.bluemap.core.storage.MapStorage;
import de.bluecolored.bluemap.core.util.FileHelper;

import java.io.Closeable;
import java.io.IOException;
import java.net.BindException;
import java.net.InetSocketAddress;
import java.net.UnknownHostException;
import java.nio.file.Path;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

public final class BlueMapWebServer implements Closeable {

    private static final String DEFAULT_CONFIG_FOLDER = "config";

    private final BlueMapService blueMap;
    private final HttpServer webServer;
    private final ExecutorService connectionExecutor;
    private final Logger webLogger;

    private BlueMapWebServer(
            BlueMapService blueMap,
            HttpServer webServer,
            ExecutorService connectionExecutor,
            Logger webLogger
    ) {
        this.blueMap = blueMap;
        this.webServer = webServer;
        this.connectionExecutor = connectionExecutor;
        this.webLogger = webLogger;
    }

    public static BlueMapWebServer create(Path configFolder, boolean verbose)
            throws IOException, ConfigurationException, InterruptedException {
        BlueMapService blueMap = null;
        ExecutorService connectionExecutor = null;
        Logger webLogger = null;

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
                        new MapRequestHandler(storage)
                );
            }

            routes.register("health/live", _ -> healthResponse());
            routes.register("health/ready", _ -> healthResponse());

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

            connectionExecutor = Executors.newVirtualThreadPerTaskExecutor();
            HttpServer webServer = new HttpServer(
                    "BlueMap-Webserver",
                    new LoggingRequestHandler(
                            new BlueMapResponseModifier(routes),
                            config.getLog().getFormat(),
                            webLogger
                    ),
                    connectionExecutor
            );

            try {
                webServer.bind(new InetSocketAddress(config.resolveIp(), config.getPort()));
            } catch (UnknownHostException ex) {
                throw new ConfigurationException(
                        "BlueMap failed to resolve the ip in webserver.conf.",
                        ex
                );
            } catch (BindException ex) {
                throw new ConfigurationException(
                        "BlueMap failed to bind the configured webserver port " + config.getPort() + ".",
                        ex
                );
            }

            return new BlueMapWebServer(blueMap, webServer, connectionExecutor, webLogger);
        } catch (IOException | ConfigurationException | InterruptedException | RuntimeException ex) {
            closeAfterFailedCreate(blueMap, connectionExecutor, webLogger, ex);
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

    private static HttpResponse healthResponse() {
        HttpResponse response = new HttpResponse(HttpStatusCode.OK);
        response.addHeader("Content-Type", "text/plain; charset=utf-8");
        response.addHeader("Cache-Control", "no-store");
        response.setBody("ok\n");
        return response;
    }

    private static void closeAfterFailedCreate(
            BlueMapService blueMap,
            ExecutorService connectionExecutor,
            Logger webLogger,
            Exception original
    ) {
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
    }

    public void awaitTermination() throws InterruptedException {
        webServer.join();
    }

    @Override
    public void close() throws IOException {
        IOException exception = null;

        try {
            webServer.close();
        } catch (IOException ex) {
            exception = ex;
        }

        connectionExecutor.shutdownNow();

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
            BlueMapWebServer server = create(options.configFolder(), options.verbose());
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
                  -V, --version          Print the BlueMap version
                  -h, --help             Show this help
                """);
    }

    record Options(Path configFolder, boolean verbose, boolean version, boolean help) {

        static Options parse(String[] args) {
            Path configFolder = Path.of(DEFAULT_CONFIG_FOLDER);
            boolean verbose = false;
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
                    case "-V", "--version" -> version = true;
                    case "-h", "--help" -> help = true;
                    default -> throw new IllegalArgumentException("Unknown option: " + args[i]);
                }
            }

            return new Options(configFolder, verbose, version, help);
        }
    }
}
