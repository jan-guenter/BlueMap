mod config;
mod storage;
mod web;

use std::{future::Future, path::PathBuf, process::ExitCode, sync::atomic::Ordering};

use clap::Parser;
use thiserror::Error;
use tokio::{
    net::TcpListener,
    runtime::Builder,
    task::JoinHandle,
    time::{self, Instant},
};
use tracing_subscriber::EnvFilter;

use crate::{
    config::Config,
    storage::Backend,
    web::{AppState, router},
};

pub const VERSION: &str = match option_env!("BLUEMAP_VERSION") {
    Some(version) => version,
    None => "development",
};

type Result<T, E = AppError> = std::result::Result<T, E>;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("failed to read configuration {0}: {1}")]
    ConfigIo(PathBuf, #[source] std::io::Error),
    #[error("failed to parse configuration {0}: {1}")]
    ConfigParse(PathBuf, #[source] toml::de::Error),
    #[error("invalid configuration: {0}")]
    InvalidConfig(String),
    #[error("invalid BlueMap SQL schema: {0}")]
    InvalidSchema(String),
    #[error("database error: {0}")]
    Database(#[source] sqlx::Error),
    #[error("storage I/O error for {0}: {1}")]
    StorageIo(PathBuf, #[source] std::io::Error),
    #[error("{0} timed out")]
    StorageTimeout(&'static str),
    #[error("unsupported stored encoding: {0}")]
    UnsupportedEncoding(String),
    #[error("stored object is {actual} bytes, above the configured {limit}-byte limit")]
    ObjectTooLarge { actual: u64, limit: u64 },
    #[error("invalid storage path")]
    InvalidPath,
    #[error("failed to serialize web settings: {0}")]
    SettingsSerialization(#[source] serde_json::Error),
    #[error("failed to bind HTTP listener: {0}")]
    Bind(#[source] std::io::Error),
    #[error("HTTP server failed: {0}")]
    Serve(#[source] std::io::Error),
    #[error("failed to create async runtime: {0}")]
    Runtime(#[source] std::io::Error),
}

#[derive(Debug, Parser)]
#[command(name = "bluemap-rust-webserver", version = VERSION)]
struct Cli {
    #[arg(short, long, default_value = "/etc/bluemap-web/config.toml")]
    config: PathBuf,
}

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .compact()
        .init();

    let config = match Config::load(&Cli::parse().config) {
        Ok(config) => config,
        Err(error) => return report_exit(error),
    };
    let runtime_shutdown_timeout = config.runtime_shutdown_timeout();
    let runtime = match Builder::new_multi_thread().enable_all().build() {
        Ok(runtime) => runtime,
        Err(error) => return report_exit(AppError::Runtime(error)),
    };
    let result = runtime.block_on(run(config));
    // Blocking filesystem workers cannot be force-cancelled. This explicit
    // timeout lets the process return even when a remote filesystem syscall
    // remains stuck after the graceful HTTP and storage shutdown budget.
    runtime.shutdown_timeout(runtime_shutdown_timeout);
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => report_exit(error),
    }
}

fn report_exit(error: AppError) -> ExitCode {
    tracing::error!(error = %error, "BlueMap Rust webserver stopped");
    ExitCode::FAILURE
}

async fn run(config: Config) -> Result<()> {
    tracing::info!(
        version = VERSION,
        bind = %config.bind,
        maps = config.maps.len(),
        "starting BlueMap Rust webserver"
    );
    let backend = storage_operation(
        config.storage_timeout(),
        "storage startup",
        Backend::connect(&config),
    )
    .await?;
    let map_ids: Vec<&str> = config.maps.iter().map(|map| map.id.as_str()).collect();
    storage_operation(
        config.storage_timeout(),
        "initial storage dependency check",
        backend.validate(&map_ids),
    )
    .await?;
    let state = AppState::new(config, backend)?;
    let listener = TcpListener::bind(state.config.bind)
        .await
        .map_err(AppError::Bind)?;

    let mut health_task = spawn_dependency_monitor(state.clone());
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    let shutdown_token = tokio::sync::watch::channel(false);
    let mut shutdown_rx = shutdown_token.1;
    let server = axum::serve(listener, router(state.clone())).with_graceful_shutdown(async move {
        while !*shutdown_rx.borrow_and_update() {
            if shutdown_rx.changed().await.is_err() {
                break;
            }
        }
    });
    let mut server_task = tokio::spawn(async move { server.await });

    tokio::select! {
        _ = &mut shutdown => {}
        result = &mut server_task => {
            let shutdown_deadline = Instant::now() + state.config.shutdown_grace();
            stop_dependency_monitor(&state, &mut health_task, shutdown_deadline).await;
            close_backend_before(&state.backend, shutdown_deadline).await;
            return result
                .map_err(|error| AppError::InvalidConfig(format!("server task failed: {error}")))?
                .map_err(AppError::Serve);
        }
    }
    let shutdown_deadline = Instant::now() + state.config.shutdown_grace();
    tracing::info!("shutdown requested; stopping storage health checks");
    stop_dependency_monitor(&state, &mut health_task, shutdown_deadline).await;
    let _ = shutdown_token.0.send(true);

    match time::timeout_at(shutdown_deadline, &mut server_task).await {
        Ok(joined) => {
            joined
                .map_err(|error| AppError::InvalidConfig(format!("server task failed: {error}")))?
                .map_err(AppError::Serve)?;
        }
        Err(_) => {
            tracing::warn!("graceful drain timed out");
            server_task.abort();
        }
    }
    close_backend_before(&state.backend, shutdown_deadline).await;
    tracing::info!("BlueMap Rust webserver stopped");
    Ok(())
}

fn spawn_dependency_monitor(state: AppState) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticker = time::interval(state.config.dependency_check_interval());
        ticker.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
        loop {
            ticker.tick().await;
            if state.stopping.load(Ordering::Acquire) {
                break;
            }
            let result = storage_operation(
                state.config.storage_timeout(),
                "storage dependency check",
                state.backend.probe(),
            )
            .await;
            if state.stopping.load(Ordering::Acquire) {
                break;
            }
            let ready = result.is_ok();
            let previous = state.ready.swap(ready, Ordering::AcqRel);
            if previous != ready {
                if ready {
                    tracing::info!("storage dependency recovered; server is ready");
                } else {
                    tracing::warn!("storage dependency check failed; server is unready");
                }
            }
            if let Err(error) = result {
                tracing::warn!(error = %error, "storage dependency check failed");
            }
        }
    })
}

async fn stop_dependency_monitor(
    state: &AppState,
    health_task: &mut JoinHandle<()>,
    deadline: Instant,
) {
    state.stopping.store(true, Ordering::Release);
    health_task.abort();
    match time::timeout_at(deadline, health_task).await {
        Ok(Ok(())) => {}
        Ok(Err(error)) if error.is_cancelled() => {}
        Ok(Err(error)) => tracing::warn!(error = %error, "storage health task failed"),
        Err(_) => tracing::warn!("storage health task did not stop within the shutdown budget"),
    }
    // This store happens only after the monitor has been aborted and awaited.
    // The stopping flag also prevents a late probe from publishing ready=true.
    state.ready.store(false, Ordering::Release);
}

async fn storage_operation<T>(
    timeout: std::time::Duration,
    name: &'static str,
    operation: impl Future<Output = Result<T>>,
) -> Result<T> {
    time::timeout(timeout, operation)
        .await
        .map_err(|_| AppError::StorageTimeout(name))?
}

async fn close_backend_before(backend: &Backend, deadline: Instant) {
    if !completes_before(deadline, backend.close()).await {
        tracing::warn!("storage shutdown exceeded the graceful-shutdown budget");
    }
}

async fn completes_before(deadline: Instant, operation: impl Future<Output = ()>) -> bool {
    time::timeout_at(deadline, operation).await.is_ok()
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut terminate =
            signal(SignalKind::terminate()).expect("SIGTERM handler should install");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = terminate.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        sync::{Arc, Condvar, Mutex},
        time::Duration,
    };

    use axum::{
        body::{Body, HttpBody as _},
        http::{Request, StatusCode, header},
    };
    use http_body_util::BodyExt;
    use tempfile::TempDir;
    use tower::ServiceExt;

    use super::*;

    struct ReadGate {
        entered: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
        released: Mutex<bool>,
        condition: Condvar,
    }

    impl ReadGate {
        fn new() -> (Arc<Self>, tokio::sync::oneshot::Receiver<()>) {
            let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
            (
                Arc::new(Self {
                    entered: Mutex::new(Some(entered_tx)),
                    released: Mutex::new(false),
                    condition: Condvar::new(),
                }),
                entered_rx,
            )
        }

        fn hook(self: &Arc<Self>) -> crate::storage::FileReadHook {
            let gate = self.clone();
            Arc::new(move || {
                if let Some(entered) = gate.entered.lock().unwrap().take() {
                    let _ = entered.send(());
                }
                let mut released = gate.released.lock().unwrap();
                while !*released {
                    released = gate.condition.wait(released).unwrap();
                }
            })
        }

        fn release(&self) {
            *self.released.lock().unwrap() = true;
            self.condition.notify_all();
        }
    }

    async fn fixture() -> (TempDir, AppState) {
        fixture_with_limit(8).await
    }

    async fn fixture_with_limit(max_in_flight_requests: usize) -> (TempDir, AppState) {
        fixture_with_encoding(max_in_flight_requests, crate::config::StoredEncoding::Gzip).await
    }

    async fn fixture_with_encoding(
        max_in_flight_requests: usize,
        compression: crate::config::StoredEncoding,
    ) -> (TempDir, AppState) {
        fixture_with_limits(max_in_flight_requests, 64 * 1024 * 1024, compression).await
    }

    async fn fixture_with_limits(
        max_in_flight_requests: usize,
        max_object_bytes: u64,
        compression: crate::config::StoredEncoding,
    ) -> (TempDir, AppState) {
        let temp = TempDir::new().unwrap();
        let web = temp.path().join("web");
        let maps = temp.path().join("maps");
        fs::create_dir_all(&web).unwrap();
        fs::create_dir_all(maps.join("world")).unwrap();
        fs::write(web.join("index.html"), "<html>BlueMap</html>").unwrap();
        fs::write(maps.join("world/settings.json"), b"{\"name\":\"World\"}").unwrap();
        let tile_suffix = format!(".prbm{}", compression.file_suffix().unwrap());
        let tile = crate::storage::grid_item_path(&maps.join("world/tiles/0"), 3, 4, &tile_suffix);
        fs::create_dir_all(tile.parent().unwrap()).unwrap();
        fs::write(tile, b"stored-compressed").unwrap();
        let lowres = crate::storage::grid_item_path(&maps.join("world/tiles/10"), 0, 0, ".png");
        fs::create_dir_all(lowres.parent().unwrap()).unwrap();
        fs::write(lowres, b"png").unwrap();
        let config = Config {
            bind: "127.0.0.1:0".parse().unwrap(),
            web_root: web,
            shutdown_grace_seconds: 1,
            runtime_shutdown_seconds: 1,
            dependency_check_seconds: 1,
            storage_timeout_seconds: 1,
            max_in_flight_requests,
            max_object_bytes,
            tile_cache_max_age_seconds: 60,
            webapp: Default::default(),
            maps: vec![crate::config::MapConfig {
                id: "world".to_owned(),
                sorting: 0,
            }],
            storage: crate::config::StorageConfig::File {
                root: maps.clone(),
                compression,
            },
        };
        let backend = Backend::File(
            crate::storage::FileBackend::open(
                maps,
                compression,
                max_in_flight_requests,
                max_object_bytes,
            )
            .await
            .unwrap(),
        );
        let state = AppState::new(config, backend).unwrap();
        (temp, state)
    }

    #[tokio::test]
    async fn golden_file_routes_and_required_encoding() {
        let (_temp, state) = fixture().await;
        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let app = router(state);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/tiles/0/x3/z4.prbm")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_ENCODING).unwrap(),
            "gzip"
        );

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/tiles/0/x3/z4.prbm")
                    .header(header::ACCEPT_ENCODING, "gzip;q=0, *;q=1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_ACCEPTABLE);
        assert!(response.headers().get(header::ACCEPT_ENCODING).is_none());
        assert_eq!(
            response.headers().get(header::CONTENT_TYPE).unwrap(),
            "application/problem+json"
        );
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-store,no-transform"
        );
        assert_eq!(
            response.headers().get(header::VARY).unwrap(),
            "Accept-Encoding"
        );
        assert_eq!(
            response
                .headers()
                .get("x-bluemap-required-content-encoding")
                .unwrap(),
            "gzip"
        );
        assert_eq!(backend.body_read_count(), 0);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
            serde_json::json!({
                "code": "bluemap_required_content_encoding",
                "requiredEncoding": "gzip",
            })
        );

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/tiles/0/x3/z4.prbm")
                    .header(header::ACCEPT_ENCODING, "gzip")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_TYPE).unwrap(),
            "application/octet-stream"
        );

        let etag = response.headers().get(header::ETAG).unwrap().clone();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(body, "stored-compressed");
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/tiles/0/x3/z4.prbm")
                    .header(header::ACCEPT_ENCODING, "gzip")
                    .header(header::IF_NONE_MATCH, etag)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_MODIFIED);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/tiles/0/x9/z9.prbm")
                    .header(header::ACCEPT_ENCODING, "gzip")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NO_CONTENT);
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-store,no-transform"
        );

        let response = app
            .clone()
            .oneshot(
                Request::get("/maps/world/tiles/10/x0/z0.png")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_TYPE).unwrap(),
            "image/png"
        );
        assert_eq!(
            response.headers().get(header::VARY).unwrap(),
            "Accept-Encoding"
        );
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "public,max-age=60,must-revalidate,no-transform"
        );

        let response = app
            .clone()
            .oneshot(
                Request::get("/maps/world/tiles/10/x0/z0.png")
                    .header(header::ACCEPT_ENCODING, "identity;q=0")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_ACCEPTABLE);
        assert_eq!(
            response
                .headers()
                .get("x-bluemap-required-content-encoding")
                .unwrap(),
            "identity"
        );

        let response = app
            .clone()
            .oneshot(
                Request::head("/maps/world/tiles/0/x3/z4.prbm")
                    .header(header::ACCEPT_ENCODING, "gzip;q=0")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_ACCEPTABLE);
        assert!(
            response
                .into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes()
                .is_empty()
        );

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/live/sse")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);

        let response = app
            .oneshot(
                Request::get("/missing-static-file")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-store,no-transform"
        );
    }

    #[tokio::test]
    async fn static_not_modified_response_keeps_its_cache_policy() {
        let (_temp, state) = fixture().await;
        let app = router(state);

        let response = app
            .clone()
            .oneshot(Request::get("/").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-cache"
        );
        let last_modified = response
            .headers()
            .get(header::LAST_MODIFIED)
            .unwrap()
            .clone();

        let response = app
            .oneshot(
                Request::get("/")
                    .header(header::IF_MODIFIED_SINCE, last_modified)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_MODIFIED);
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-cache"
        );
    }

    #[tokio::test]
    async fn root_settings_preserve_map_order_and_health_state() {
        let (_temp, mut state) = fixture().await;
        Arc::get_mut(&mut state.config).unwrap().maps.insert(
            0,
            crate::config::MapConfig {
                id: "first".to_owned(),
                sorting: -1,
            },
        );
        // Rebuild because settings and allowed map ids are intentionally immutable.
        let state = AppState::new((*state.config).clone(), state.backend).unwrap();
        let ready = state.ready.clone();
        let app = router(state);

        let response = app
            .clone()
            .oneshot(Request::get("/settings.json").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let settings: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(settings["maps"], serde_json::json!(["first", "world"]));
        assert_eq!(settings["mapDataRoot"], "maps");
        ready.store(false, Ordering::Release);
        let response = app
            .oneshot(Request::get("/health/ready").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn head_and_not_modified_do_not_read_object_bodies() {
        let (_temp, state) = fixture().await;
        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let app = router(state);

        let response = app
            .clone()
            .oneshot(
                Request::head("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_LENGTH).unwrap(),
            "16"
        );
        assert!(
            response
                .into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes()
                .is_empty()
        );
        assert_eq!(backend.body_read_count(), 0);

        let head = app
            .clone()
            .oneshot(
                Request::head("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let etag = head.headers().get(header::ETAG).unwrap().clone();
        let response = app
            .clone()
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .header(header::IF_NONE_MATCH, etag)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_MODIFIED);
        assert_eq!(backend.body_read_count(), 0);

        let response = app
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(backend.body_read_count(), 0);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(body, "{\"name\":\"World\"}");
        assert_eq!(backend.body_read_count(), 1);
        assert_eq!(backend.metadata_read_count(), 0);
    }

    #[tokio::test]
    async fn tiny_nested_asset_returns_headers_before_its_gated_descriptor_read() {
        let (temporary, state) = fixture().await;
        let asset = temporary
            .path()
            .join("maps/world/assets/playerheads/player.png");
        fs::create_dir_all(asset.parent().unwrap()).unwrap();
        let expected = vec![0x5a; 391];
        fs::write(&asset, &expected).unwrap();

        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let (gate, entered) = ReadGate::new();
        backend.set_read_hook(Some(gate.hook()));
        let app = router(state);

        let response = tokio::time::timeout(
            Duration::from_millis(500),
            app.oneshot(
                Request::get("/maps/world/assets/playerheads/player.png")
                    .body(Body::empty())
                    .unwrap(),
            ),
        )
        .await
        .expect("response headers waited for the gated body read")
        .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_LENGTH).unwrap(),
            "391"
        );
        assert_eq!(
            response.headers().get(header::CONTENT_TYPE).unwrap(),
            "image/png"
        );
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "public,no-cache,no-transform"
        );
        assert!(response.headers().contains_key(header::ETAG));
        assert!(response.headers().contains_key(header::LAST_MODIFIED));
        assert_eq!(response.body().size_hint().exact(), Some(391));
        assert_eq!(backend.object_open_count(), 1);
        assert_eq!(backend.metadata_read_count(), 0);
        assert_eq!(backend.body_read_count(), 0);

        let body_task =
            tokio::spawn(async move { response.into_body().collect().await.unwrap().to_bytes() });
        entered.await.unwrap();
        assert_eq!(backend.body_read_count(), 1);
        assert!(!body_task.is_finished());
        gate.release();
        let body = body_task.await.unwrap();
        assert_eq!(body.as_ref(), expected.as_slice());
        assert_eq!(backend.object_open_count(), 1);
    }

    #[tokio::test]
    async fn lz4_rejection_uses_metadata_without_reading_the_object_body() {
        let (_temp, state) = fixture_with_encoding(8, crate::config::StoredEncoding::Lz4).await;
        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let app = router(state);

        let response = app
            .clone()
            .oneshot(
                Request::get("/maps/world/tiles/0/x3/z4.prbm")
                    .header(header::ACCEPT_ENCODING, "gzip")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_ACCEPTABLE);
        assert_eq!(
            response
                .headers()
                .get("x-bluemap-required-content-encoding")
                .unwrap(),
            "lz4"
        );
        assert_eq!(backend.body_read_count(), 0);

        let response = app
            .oneshot(
                Request::get("/maps/world/tiles/0/x3/z4.prbm")
                    .header(header::ACCEPT_ENCODING, "lz4")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(backend.body_read_count(), 0);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(body, "stored-compressed");
        assert_eq!(backend.body_read_count(), 1);
    }

    #[tokio::test]
    async fn oversized_metadata_rejects_get_without_reading_the_object_body() {
        let (_temp, state) = fixture_with_limits(8, 8, crate::config::StoredEncoding::Gzip).await;
        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let app = router(state);

        let response = app
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-store,no-transform"
        );
        let problem = response.into_body().collect().await.unwrap().to_bytes();
        assert!(
            String::from_utf8_lossy(&problem)
                .contains("stored map object exceeds server response limit")
        );
        assert_eq!(backend.body_read_count(), 0);
        assert_eq!(backend.object_open_count(), 1);
        assert_eq!(backend.metadata_read_count(), 0);
    }

    #[tokio::test]
    async fn in_place_growth_makes_the_fixed_length_stream_incomplete() {
        let (temporary, state) = fixture().await;
        let settings = temporary.path().join("maps/world/settings.json");
        let advertised = 64 * 1024 + 128;
        fs::write(&settings, vec![0x41; advertised]).unwrap();

        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let hook_calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let hook_counter = hook_calls.clone();
        let mutation_path = settings.clone();
        backend.set_read_hook(Some(Arc::new(move || {
            if hook_counter.fetch_add(1, Ordering::AcqRel) == 1 {
                use std::io::Write;

                let mut file = std::fs::OpenOptions::new()
                    .append(true)
                    .open(&mutation_path)
                    .unwrap();
                file.write_all(b"growth-must-not-be-read").unwrap();
                file.sync_all().unwrap();
            }
        })));
        let app = router(state);

        let response = app
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_LENGTH).unwrap(),
            advertised.to_string().as_str()
        );

        let mut body = response.into_body();
        let mut emitted = 0;
        let mut failed = false;
        while let Some(frame) = body.frame().await {
            match frame {
                Ok(frame) => {
                    if let Ok(data) = frame.into_data() {
                        emitted += data.len();
                    }
                }
                Err(_) => {
                    failed = true;
                    break;
                }
            }
        }
        backend.set_read_hook(None);

        assert!(failed, "a changed descriptor completed successfully");
        assert!(
            emitted > 0,
            "the stream did not exercise incremental delivery"
        );
        assert!(
            emitted < advertised,
            "the final chunk was emitted despite failed metadata validation"
        );
        assert_eq!(backend.object_open_count(), 1);
        assert!(backend.body_read_count() >= 2);
    }

    #[tokio::test]
    async fn shrink_to_early_eof_cannot_complete_the_fixed_length_stream() {
        let (temporary, state) = fixture().await;
        let settings = temporary.path().join("maps/world/settings.json");
        let first_chunk = 64 * 1024;
        let advertised = first_chunk + 128;
        fs::write(&settings, vec![0x42; advertised]).unwrap();

        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let hook_calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let hook_counter = hook_calls.clone();
        let mutation_path = settings.clone();
        backend.set_read_hook(Some(Arc::new(move || {
            if hook_counter.fetch_add(1, Ordering::AcqRel) == 1 {
                let file = std::fs::OpenOptions::new()
                    .write(true)
                    .open(&mutation_path)
                    .unwrap();
                file.set_len(first_chunk as u64).unwrap();
                file.sync_all().unwrap();
            }
        })));
        let app = router(state);

        let response = app
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_LENGTH).unwrap(),
            advertised.to_string().as_str()
        );

        let mut body = response.into_body();
        let mut emitted = 0;
        let mut failed = false;
        while let Some(frame) = body.frame().await {
            match frame {
                Ok(frame) => {
                    if let Ok(data) = frame.into_data() {
                        emitted += data.len();
                    }
                }
                Err(_) => {
                    failed = true;
                    break;
                }
            }
        }
        backend.set_read_hook(None);

        assert!(failed, "an early EOF completed successfully");
        assert!(emitted > 0);
        assert!(emitted < advertised);
        assert!(backend.body_read_count() >= 2);
    }

    #[tokio::test]
    async fn timed_out_body_read_truncates_after_200_and_retains_worker_capacity() {
        let (_temporary, state) = fixture_with_limit(1).await;
        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let (gate, entered) = ReadGate::new();
        backend.set_read_hook(Some(gate.hook()));
        let object = backend
            .open_object("world", crate::storage::ObjectRequest::Settings)
            .await
            .unwrap()
            .unwrap();
        let admission = Arc::new(tokio::sync::Semaphore::new(1));
        let permit = admission.clone().acquire_owned().await.unwrap();
        let response = crate::web::file_object_response(
            object,
            "settings.json",
            &axum::http::HeaderMap::new(),
            &axum::http::Method::GET,
            permit,
            Duration::from_millis(20),
            60,
        );

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_LENGTH).unwrap(),
            "16"
        );
        assert_eq!(response.body().size_hint().exact(), Some(16));
        assert_eq!(backend.body_read_count(), 0);

        let result = tokio::time::timeout(Duration::from_secs(1), response.into_body().collect())
            .await
            .expect("body timeout did not terminate the response");
        assert!(result.is_err(), "timed-out fixed-length body completed");
        entered.await.unwrap();
        assert_eq!(backend.body_read_count(), 1);
        assert_eq!(admission.available_permits(), 1);
        assert_eq!(
            backend.available_worker_permits(),
            0,
            "body timeout released a still-blocked filesystem syscall"
        );

        gate.release();
        tokio::time::timeout(Duration::from_secs(1), async {
            while backend.available_worker_permits() == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(backend.available_worker_permits(), 1);
    }

    #[tokio::test]
    async fn dropping_a_blocked_body_releases_http_but_not_worker_capacity() {
        let (_temporary, state) = fixture_with_limit(1).await;
        let backend = match &state.backend {
            Backend::File(backend) => backend.clone(),
            _ => unreachable!(),
        };
        let admission = state.clone();
        let (gate, entered) = ReadGate::new();
        backend.set_read_hook(Some(gate.hook()));
        let app = router(state);

        let response = app
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(admission.available_in_flight_permits(), 0);
        assert_eq!(backend.available_worker_permits(), 1);

        let body_task = tokio::spawn(async move { response.into_body().collect().await.map(drop) });
        entered.await.unwrap();
        assert_eq!(backend.available_worker_permits(), 0);
        assert_eq!(admission.available_in_flight_permits(), 0);

        body_task.abort();
        assert!(body_task.await.unwrap_err().is_cancelled());
        assert_eq!(admission.available_in_flight_permits(), 1);
        assert_eq!(
            backend.available_worker_permits(),
            0,
            "dropping the async body released a still-blocked filesystem syscall"
        );

        gate.release();
        tokio::time::timeout(Duration::from_secs(1), async {
            while backend.available_worker_permits() == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(backend.available_worker_permits(), 1);
    }

    #[tokio::test]
    async fn in_flight_limit_rejects_overload_until_response_body_is_dropped() {
        let (_temp, state) = fixture_with_limit(1).await;
        let app = router(state);

        let held_response = app
            .clone()
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(held_response.status(), StatusCode::OK);
        assert_eq!(held_response.body().size_hint().exact(), Some(16));

        let overloaded = app
            .clone()
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(overloaded.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(overloaded.headers().get(header::RETRY_AFTER).unwrap(), "1");

        drop(held_response);
        let recovered = app
            .oneshot(
                Request::get("/maps/world/settings.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(recovered.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn stalled_dependency_and_query_operations_time_out() {
        let ready = std::sync::atomic::AtomicBool::new(true);
        let result = storage_operation(
            std::time::Duration::from_millis(10),
            "test query",
            std::future::pending::<Result<()>>(),
        )
        .await;
        ready.store(result.is_ok(), Ordering::Release);
        assert!(matches!(
            result,
            Err(AppError::StorageTimeout("test query"))
        ));
        assert!(!ready.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn shutdown_deadline_bounds_stalled_cleanup() {
        let completed = completes_before(
            Instant::now() + std::time::Duration::from_millis(10),
            std::future::pending(),
        )
        .await;
        assert!(!completed);
    }

    #[tokio::test]
    async fn stopping_health_monitor_is_final_before_backend_shutdown() {
        let (_temp, state) = fixture().await;
        let mut monitor = spawn_dependency_monitor(state.clone());
        stop_dependency_monitor(
            &state,
            &mut monitor,
            Instant::now() + std::time::Duration::from_secs(1),
        )
        .await;

        assert!(state.stopping.load(Ordering::Acquire));
        assert!(!state.ready.load(Ordering::Acquire));
        assert!(monitor.is_finished());
        tokio::task::yield_now().await;
        assert!(!state.ready.load(Ordering::Acquire));
    }
}
