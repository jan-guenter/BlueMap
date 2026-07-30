mod config;
mod storage;
mod web;

use std::{future::Future, path::PathBuf, process::ExitCode, sync::atomic::Ordering};

use clap::Parser;
use thiserror::Error;
use tokio::{
    net::TcpListener,
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
    #[error("invalid storage path")]
    InvalidPath,
    #[error("failed to serialize web settings: {0}")]
    SettingsSerialization(#[source] serde_json::Error),
    #[error("failed to bind HTTP listener: {0}")]
    Bind(#[source] std::io::Error),
    #[error("HTTP server failed: {0}")]
    Serve(#[source] std::io::Error),
}

#[derive(Debug, Parser)]
#[command(name = "bluemap-rust-webserver", version = VERSION)]
struct Cli {
    #[arg(short, long, default_value = "/etc/bluemap-web/config.toml")]
    config: PathBuf,
}

#[tokio::main]
async fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .compact()
        .init();

    match run(Cli::parse()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            tracing::error!(error = %error, "BlueMap Rust webserver stopped");
            ExitCode::FAILURE
        }
    }
}

async fn run(cli: Cli) -> Result<()> {
    let config = Config::load(&cli.config)?;
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

    let health_task = spawn_dependency_monitor(state.clone());
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
            health_task.abort();
            state.ready.store(false, Ordering::Release);
            close_backend_before(
                &state.backend,
                Instant::now() + state.config.shutdown_grace(),
            )
            .await;
            return result
                .map_err(|error| AppError::InvalidConfig(format!("server task failed: {error}")))?
                .map_err(AppError::Serve);
        }
    }
    tracing::info!("shutdown requested; marking server unready");
    state.ready.store(false, Ordering::Release);
    health_task.abort();
    let _ = shutdown_token.0.send(true);
    let shutdown_deadline = Instant::now() + state.config.shutdown_grace();

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
            let result = storage_operation(
                state.config.storage_timeout(),
                "storage dependency check",
                state.backend.probe(),
            )
            .await;
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
    use std::{fs, sync::Arc};

    use axum::{
        body::{Body, HttpBody as _},
        http::{Request, StatusCode, header},
    };
    use http_body_util::BodyExt;
    use tempfile::TempDir;
    use tower::ServiceExt;

    use super::*;

    fn fixture() -> (TempDir, AppState) {
        fixture_with_limit(32)
    }

    fn fixture_with_limit(max_in_flight_requests: usize) -> (TempDir, AppState) {
        let temp = TempDir::new().unwrap();
        let web = temp.path().join("web");
        let maps = temp.path().join("maps");
        fs::create_dir_all(&web).unwrap();
        fs::create_dir_all(maps.join("world")).unwrap();
        fs::write(web.join("index.html"), "<html>BlueMap</html>").unwrap();
        fs::write(maps.join("world/settings.json"), b"{\"name\":\"World\"}").unwrap();
        let tile = crate::storage::grid_item_path(&maps.join("world/tiles/0"), 3, 4, ".prbm.gz");
        fs::create_dir_all(tile.parent().unwrap()).unwrap();
        fs::write(tile, b"stored-gzip").unwrap();
        let lowres = crate::storage::grid_item_path(&maps.join("world/tiles/10"), 0, 0, ".png");
        fs::create_dir_all(lowres.parent().unwrap()).unwrap();
        fs::write(lowres, b"png").unwrap();
        let config = Config {
            bind: "127.0.0.1:0".parse().unwrap(),
            web_root: web,
            shutdown_grace_seconds: 1,
            dependency_check_seconds: 1,
            storage_timeout_seconds: 1,
            max_in_flight_requests,
            tile_cache_max_age_seconds: 60,
            webapp: Default::default(),
            maps: vec![crate::config::MapConfig {
                id: "world".to_owned(),
                sorting: 0,
            }],
            storage: crate::config::StorageConfig::File {
                root: maps.clone(),
                compression: crate::config::StoredEncoding::Gzip,
            },
        };
        let backend = Backend::File(
            crate::storage::FileBackend::open(maps, crate::config::StoredEncoding::Gzip).unwrap(),
        );
        let state = AppState::new(config, backend).unwrap();
        (temp, state)
    }

    #[tokio::test]
    async fn golden_file_routes_and_required_encoding() {
        let (_temp, state) = fixture();
        let app = router(state);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/tiles/0/x3z4.prbm.gz")
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
                    .uri("/maps/world/tiles/0/x3z4.prbm.gz")
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
                    .uri("/maps/world/tiles/0/x3z4.prbm.gz")
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
        assert_eq!(body, "stored-gzip");
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/maps/world/tiles/0/x3z4.prbm.gz")
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
                    .uri("/maps/world/tiles/0/x99z99.prbm.gz")
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
                Request::get("/maps/world/tiles/1/0/x0z0.png")
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
                Request::get("/maps/world/tiles/1/0/x0z0.png")
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
                Request::head("/maps/world/tiles/0/x3z4.prbm.gz")
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
    async fn root_settings_preserve_map_order_and_health_state() {
        let (_temp, mut state) = fixture();
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
        let (_temp, state) = fixture();
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
        assert_eq!(backend.body_read_count(), 1);
    }

    #[tokio::test]
    async fn in_flight_limit_rejects_overload_until_response_body_is_dropped() {
        let (_temp, state) = fixture_with_limit(1);
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
}
