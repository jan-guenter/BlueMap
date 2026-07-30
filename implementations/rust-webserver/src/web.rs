use std::{
    collections::HashSet,
    convert::Infallible,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    Json, Router,
    body::Body,
    extract::{Path, State},
    http::{
        HeaderMap, HeaderName, HeaderValue, Method, StatusCode,
        header::{
            ACCEPT_ENCODING, CACHE_CONTROL, CONTENT_ENCODING, CONTENT_LENGTH, CONTENT_TYPE, ETAG,
            IF_MODIFIED_SINCE, IF_NONE_MATCH, LAST_MODIFIED, RETRY_AFTER, VARY,
        },
    },
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::get,
};
use bytes::Bytes;
use http_body::{Frame, SizeHint};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tower_http::{services::ServeDir, trace::TraceLayer};

use crate::{
    AppError, Result, VERSION,
    config::{Config, StoredEncoding},
    storage::{Backend, ObjectClass, ObjectMetadata, ObjectRequest, StoredObject, grid_item_path},
    storage_operation,
};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub backend: Backend,
    pub ready: Arc<AtomicBool>,
    pub stopping: Arc<AtomicBool>,
    pub map_ids: Arc<HashSet<String>>,
    in_flight: Arc<Semaphore>,
    settings: Arc<serde_json::Value>,
}

impl AppState {
    pub fn new(config: Config, backend: Backend) -> Result<Self> {
        let settings = serde_json::to_value(config.web_settings(VERSION))
            .map_err(AppError::SettingsSerialization)?;
        let map_ids = config.maps.iter().map(|map| map.id.clone()).collect();
        let max_in_flight_requests = config.max_in_flight_requests;
        Ok(Self {
            config: Arc::new(config),
            backend,
            ready: Arc::new(AtomicBool::new(true)),
            stopping: Arc::new(AtomicBool::new(false)),
            map_ids: Arc::new(map_ids),
            in_flight: Arc::new(Semaphore::new(max_in_flight_requests)),
            settings: Arc::new(settings),
        })
    }
}

pub fn router(state: AppState) -> Router {
    let static_files = ServeDir::new(&state.config.web_root).append_index_html_on_directories(true);
    Router::new()
        .route("/settings.json", get(settings))
        .route("/maps/{map_id}/{*path}", get(map_data))
        .route("/health/live", get(live))
        .route("/health/ready", get(ready))
        .fallback_service(static_files)
        .layer(middleware::from_fn(cache_static_responses))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn settings(State(state): State<AppState>) -> impl IntoResponse {
    let mut response = Json(state.settings.as_ref()).into_response();
    response.headers_mut().insert(
        CACHE_CONTROL,
        HeaderValue::from_static("no-cache,no-transform"),
    );
    response
}

async fn live() -> impl IntoResponse {
    (
        [(CACHE_CONTROL, HeaderValue::from_static("no-store"))],
        "ok\n",
    )
}

async fn ready(State(state): State<AppState>) -> Response {
    if !state.stopping.load(Ordering::Acquire) && state.ready.load(Ordering::Acquire) {
        (
            StatusCode::OK,
            [(CACHE_CONTROL, HeaderValue::from_static("no-store"))],
            "ready\n",
        )
            .into_response()
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            [(CACHE_CONTROL, HeaderValue::from_static("no-store"))],
            "not ready\n",
        )
            .into_response()
    }
}

async fn map_data(
    State(state): State<AppState>,
    Path((map_id, path)): Path<(String, String)>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    if !state.map_ids.contains(&map_id) {
        return error_response(StatusCode::NOT_FOUND, "unknown map");
    }
    if path == "live/sse" {
        return error_response(StatusCode::NOT_FOUND, "SSE is not available");
    }
    let Some(request) = parse_map_path(&path) else {
        return error_response(StatusCode::NOT_FOUND, "unknown map object");
    };
    let permit = match state.in_flight.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => return overload_response(),
    };

    let metadata = match storage_operation(
        state.config.storage_timeout(),
        "map metadata query",
        state.backend.metadata(&map_id, request.clone()),
    )
    .await
    {
        Ok(Some(metadata)) => metadata,
        Ok(None) if path.starts_with("tiles/") => return missing_tile_response(),
        Ok(None) => return error_response(StatusCode::NOT_FOUND, "map object not found"),
        Err(error) => return storage_error_response(error, &map_id, &path),
    };
    if let Some(actual) = metadata.content_length
        && actual > state.config.max_object_bytes
    {
        return object_too_large_response(actual, state.config.max_object_bytes);
    }
    if let Some(response) = metadata_only_response(
        &metadata,
        &path,
        &headers,
        &method,
        state.config.tile_cache_max_age_seconds,
    ) {
        return response;
    }

    match storage_operation(
        state.config.storage_timeout(),
        "map body query",
        state.backend.read(&map_id, request),
    )
    .await
    {
        Ok(Some(object)) => object_response(
            object,
            &path,
            &headers,
            &method,
            permit,
            state.config.tile_cache_max_age_seconds,
        ),
        Ok(None) if path.starts_with("tiles/") => missing_tile_response(),
        Ok(None) => error_response(StatusCode::NOT_FOUND, "map object not found"),
        Err(error) => storage_error_response(error, &map_id, &path),
    }
}

fn storage_error_response(error: AppError, map_id: &str, path: &str) -> Response {
    match error {
        AppError::StorageTimeout(_) => storage_timeout_response(map_id, path),
        AppError::UnsupportedEncoding(encoding) => error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            &format!("unsupported stored encoding: {encoding}"),
        ),
        AppError::ObjectTooLarge { actual, limit } => object_too_large_response(actual, limit),
        error => {
            tracing::error!(error = %error, map_id, path, "failed to read map object");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to read map object",
            )
        }
    }
}

fn object_too_large_response(actual: u64, limit: u64) -> Response {
    tracing::error!(
        actual_bytes = actual,
        limit_bytes = limit,
        "stored map object exceeds configured response limit"
    );
    error_response(
        StatusCode::INTERNAL_SERVER_ERROR,
        "stored map object exceeds server response limit",
    )
}

fn storage_timeout_response(map_id: &str, path: &str) -> Response {
    tracing::warn!(map_id, path, "storage operation timed out");
    error_response(StatusCode::GATEWAY_TIMEOUT, "storage operation timed out")
}

fn missing_tile_response() -> Response {
    (
        StatusCode::NO_CONTENT,
        [(
            CACHE_CONTROL,
            HeaderValue::from_static("no-store,no-transform"),
        )],
    )
        .into_response()
}

fn overload_response() -> Response {
    let mut response = error_response(
        StatusCode::SERVICE_UNAVAILABLE,
        "too many in-flight map requests",
    );
    response
        .headers_mut()
        .insert(RETRY_AFTER, HeaderValue::from_static("1"));
    response
}

pub fn parse_map_path(path: &str) -> Option<ObjectRequest> {
    match path {
        "settings.json" => return Some(ObjectRequest::Settings),
        "textures.json" => return Some(ObjectRequest::Textures),
        "live/markers.json" => return Some(ObjectRequest::Markers),
        "live/players.json" => return Some(ObjectRequest::Players),
        _ => {}
    }
    if let Some(asset) = path.strip_prefix("assets/") {
        if asset.is_empty() {
            return None;
        }
        return Some(ObjectRequest::Asset(asset.to_owned()));
    }
    let tile = path.strip_prefix("tiles/")?;
    let (without_suffix, extension) = if let Some(tile) = tile.strip_suffix(".prbm") {
        (tile, ".prbm")
    } else {
        (tile.strip_suffix(".png")?, ".png")
    };
    let (lod_raw, coordinates) = without_suffix.split_once("/x")?;
    let lod: i32 = lod_raw.parse().ok()?;
    if lod < 0 || lod.to_string() != lod_raw {
        return None;
    }
    if (lod == 0 && extension != ".prbm") || (lod > 0 && extension != ".png") {
        return None;
    }
    let compact_coordinates = format!("x{}", coordinates.replace('/', ""));
    let (x, z) = compact_coordinates.strip_prefix('x')?.split_once('z')?;
    let x: i32 = x.parse().ok()?;
    let z: i32 = z.parse().ok()?;
    let tile_root = format!("tiles/{lod}");
    let canonical = grid_item_path(std::path::Path::new(&tile_root), x, z, extension);
    if canonical.to_str()? != path {
        return None;
    }
    Some(ObjectRequest::Tile { lod, x, z })
}

fn object_response(
    object: StoredObject,
    path: &str,
    request_headers: &HeaderMap,
    method: &Method,
    permit: OwnedSemaphorePermit,
    tile_cache_max_age_seconds: u64,
) -> Response {
    representation_response(
        object.metadata,
        Some(object.data),
        path,
        request_headers,
        method,
        Some(permit),
        tile_cache_max_age_seconds,
    )
}

fn metadata_only_response(
    metadata: &ObjectMetadata,
    path: &str,
    request_headers: &HeaderMap,
    method: &Method,
    tile_cache_max_age_seconds: u64,
) -> Option<Response> {
    if !accepts_encoding(
        request_headers,
        required_content_encoding(metadata.encoding),
    ) || *method == Method::HEAD
        || is_not_modified(request_headers, metadata)
    {
        return Some(representation_response(
            metadata.clone(),
            None,
            path,
            request_headers,
            method,
            None,
            tile_cache_max_age_seconds,
        ));
    }
    None
}

fn representation_response(
    metadata: ObjectMetadata,
    data: Option<Bytes>,
    path: &str,
    request_headers: &HeaderMap,
    method: &Method,
    mut permit: Option<OwnedSemaphorePermit>,
    tile_cache_max_age_seconds: u64,
) -> Response {
    let required = required_content_encoding(metadata.encoding);
    if !accepts_encoding(request_headers, required) {
        return encoding_not_acceptable_response(required, method);
    }

    let etag = metadata.content_hash.as_deref().and_then(etag_header);
    let last_modified = metadata
        .updated_at
        .and_then(|time| HeaderValue::from_str(&httpdate::fmt_http_date(time)).ok());
    let not_modified = is_not_modified(request_headers, &metadata);

    let mut response = if not_modified {
        let mut response = Response::new(Body::empty());
        *response.status_mut() = StatusCode::NOT_MODIFIED;
        response
    } else {
        Response::new(match data {
            Some(data) if *method != Method::HEAD => Body::new(LimitedResponseBody {
                data,
                _permit: permit.take().expect("map bodies hold an in-flight permit"),
            }),
            _ => Body::empty(),
        })
    };
    let not_modified_status = response.status() == StatusCode::NOT_MODIFIED;
    let headers = response.headers_mut();
    headers.insert(CONTENT_TYPE, content_type(path, metadata.class));
    if !not_modified_status && let Some(content_length) = metadata.content_length {
        headers.insert(
            CONTENT_LENGTH,
            HeaderValue::from_str(&content_length.to_string())
                .expect("a decimal content length is a valid header"),
        );
    }
    if let Some(encoding) = metadata.encoding.http_name() {
        headers.insert(
            CONTENT_ENCODING,
            HeaderValue::from_str(encoding).expect("known encoding is a valid header"),
        );
    }
    headers.insert(VARY, HeaderValue::from_static("Accept-Encoding"));

    headers.insert(
        CACHE_CONTROL,
        map_cache_control(metadata.class, tile_cache_max_age_seconds),
    );

    if let Some(value) = etag {
        headers.insert(ETAG, value);
    }
    if let Some(value) = last_modified {
        headers.insert(LAST_MODIFIED, value);
    }
    response
}

fn required_content_encoding(encoding: StoredEncoding) -> &'static str {
    encoding.http_name().unwrap_or("identity")
}

fn encoding_not_acceptable_response(required: &str, method: &Method) -> Response {
    let body = if *method == Method::HEAD {
        Body::empty()
    } else {
        Body::from(
            serde_json::json!({
                "code": "bluemap_required_content_encoding",
                "requiredEncoding": required,
            })
            .to_string(),
        )
    };
    let mut response = Response::new(body);
    *response.status_mut() = StatusCode::NOT_ACCEPTABLE;
    let headers = response.headers_mut();
    headers.insert(
        CONTENT_TYPE,
        HeaderValue::from_static("application/problem+json"),
    );
    headers.insert(
        CACHE_CONTROL,
        HeaderValue::from_static("no-store,no-transform"),
    );
    headers.insert(VARY, HeaderValue::from_static("Accept-Encoding"));
    headers.insert(
        HeaderName::from_static("x-bluemap-required-content-encoding"),
        HeaderValue::from_str(required).expect("known encoding is a valid header"),
    );
    response
}

fn map_cache_control(class: ObjectClass, tile_cache_max_age_seconds: u64) -> HeaderValue {
    match class {
        ObjectClass::Players => HeaderValue::from_static("private,no-store,no-transform"),
        ObjectClass::Markers
        | ObjectClass::Settings
        | ObjectClass::Textures
        | ObjectClass::Asset => HeaderValue::from_static("public,no-cache,no-transform"),
        ObjectClass::HiresTile | ObjectClass::LowresTile => HeaderValue::from_str(&format!(
            "public,max-age={tile_cache_max_age_seconds},must-revalidate,no-transform"
        ))
        .expect("a decimal cache age is a valid header"),
    }
}

const RESPONSE_CHUNK_SIZE: usize = 64 * 1024;

struct LimitedResponseBody {
    data: Bytes,
    _permit: OwnedSemaphorePermit,
}

impl axum::body::HttpBody for LimitedResponseBody {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        self: Pin<&mut Self>,
        _context: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        let body = self.get_mut();
        if body.data.is_empty() {
            return Poll::Ready(None);
        }
        let length = body.data.len().min(RESPONSE_CHUNK_SIZE);
        let chunk = body.data.split_to(length);
        Poll::Ready(Some(Ok(Frame::data(chunk))))
    }

    fn is_end_stream(&self) -> bool {
        self.data.is_empty()
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.data.len() as u64)
    }
}

fn accepts_encoding(headers: &HeaderMap, required: &str) -> bool {
    if headers.get_all(ACCEPT_ENCODING).iter().next().is_none() {
        return true;
    }

    let mut exact = None;
    let mut wildcard = None;
    for value in headers.get_all(ACCEPT_ENCODING) {
        let Ok(value) = value.to_str() else {
            continue;
        };
        for part in value.split(',') {
            let mut values = part.trim().split(';');
            let coding = values.next().unwrap_or_default().trim();
            if coding.is_empty() {
                continue;
            }
            let quality = values
                .filter_map(|parameter| parameter.trim().split_once('='))
                .find(|(name, _)| name.trim().eq_ignore_ascii_case("q"))
                .map_or(1000, |(_, value)| parse_quality(value.trim()));
            let target = if coding.eq_ignore_ascii_case(required) {
                &mut exact
            } else if coding == "*" {
                &mut wildcard
            } else {
                continue;
            };
            *target = Some(target.unwrap_or(0).max(quality));
        }
    }
    if let Some(exact) = exact {
        return exact > 0;
    }
    if required.eq_ignore_ascii_case("identity") {
        wildcard.is_none_or(|quality| quality > 0)
    } else {
        wildcard.is_some_and(|quality| quality > 0)
    }
}

fn parse_quality(value: &str) -> u16 {
    let (whole, fraction) = value.split_once('.').unwrap_or((value, ""));
    if fraction.len() > 3 || !fraction.chars().all(|character| character.is_ascii_digit()) {
        return 0;
    }
    match whole {
        "0" => {
            let padded = format!("{fraction:0<3}");
            padded.parse().unwrap_or(0)
        }
        "1" if fraction.chars().all(|character| character == '0') => 1000,
        _ => 0,
    }
}

fn etag_header(hash: &str) -> Option<HeaderValue> {
    let value = if hash.starts_with("W/") {
        format!("W/\"{}\"", hash.trim_start_matches("W/").replace('"', ""))
    } else {
        format!("\"{}\"", hash.replace('"', ""))
    };
    HeaderValue::from_str(&value).ok()
}

fn is_not_modified(headers: &HeaderMap, metadata: &ObjectMetadata) -> bool {
    if headers.get_all(IF_NONE_MATCH).iter().next().is_some() {
        if headers.get_all(IF_NONE_MATCH).iter().any(|value| {
            value.to_str().ok().is_some_and(|value| {
                split_etag_list(value).any(|candidate| candidate.trim() == "*")
            })
        }) {
            return true;
        }
        let Some(current) = metadata.content_hash.as_deref().and_then(etag_header) else {
            return false;
        };
        let Ok(current) = current.to_str() else {
            return false;
        };
        return headers.get_all(IF_NONE_MATCH).iter().any(|value| {
            value
                .to_str()
                .ok()
                .is_some_and(|value| etag_list_matches(value, current))
        });
    }

    let Some(updated_at) = metadata.updated_at else {
        return false;
    };
    headers
        .get(IF_MODIFIED_SINCE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| httpdate::parse_http_date(value).ok())
        .is_some_and(|condition| http_seconds(updated_at) <= http_seconds(condition))
}

fn etag_list_matches(value: &str, current: &str) -> bool {
    split_etag_list(value).any(|candidate| {
        let candidate = candidate.trim();
        candidate == "*" || weak_etag(candidate) == weak_etag(current)
    })
}

fn split_etag_list(value: &str) -> impl Iterator<Item = &str> {
    let mut quoted = false;
    value.split(move |character| {
        if character == '"' {
            quoted = !quoted;
            false
        } else {
            character == ',' && !quoted
        }
    })
}

fn weak_etag(value: &str) -> &str {
    value
        .strip_prefix("W/")
        .or_else(|| value.strip_prefix("w/"))
        .unwrap_or(value)
}

fn http_seconds(value: SystemTime) -> i128 {
    match value.duration_since(UNIX_EPOCH) {
        Ok(duration) => i128::from(duration.as_secs()),
        Err(error) => -i128::from(error.duration().as_secs()),
    }
}

fn content_type(path: &str, class: ObjectClass) -> HeaderValue {
    let mime = match class {
        ObjectClass::HiresTile => "application/octet-stream",
        ObjectClass::LowresTile => "image/png",
        _ => mime_guess::from_path(path)
            .first_raw()
            .unwrap_or("application/octet-stream"),
    };
    HeaderValue::from_static(mime)
}

async fn cache_static_responses(request: axum::extract::Request, next: Next) -> Response {
    let path = request.uri().path().to_owned();
    let mut response = next.run(request).await;
    if !response.headers().contains_key(CACHE_CONTROL) {
        let value = if response.status().is_success() {
            if is_fingerprinted_static(&path) {
                "public,max-age=31536000,immutable"
            } else {
                "no-cache"
            }
        } else {
            "no-store,no-transform"
        };
        response
            .headers_mut()
            .insert(CACHE_CONTROL, HeaderValue::from_static(value));
    }
    response.headers_mut().insert(
        axum::http::header::SERVER,
        HeaderValue::from_str(&format!("BlueMap/{VERSION}"))
            .unwrap_or_else(|_| HeaderValue::from_static("BlueMap")),
    );
    response
}

fn is_fingerprinted_static(path: &str) -> bool {
    let Some(asset) = path.strip_prefix("/assets/") else {
        return false;
    };
    let Some(file_name) = asset.rsplit('/').next() else {
        return false;
    };
    file_name.char_indices().any(|(index, character)| {
        if character != '-' {
            return false;
        }
        let Some((fingerprint, extension)) = file_name[index + 1..].split_once('.') else {
            return false;
        };
        fingerprint.len() == 8
            && !extension.is_empty()
            && fingerprint.chars().all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '_' | '-')
            })
    })
}

fn error_response(status: StatusCode, message: &str) -> Response {
    let mut response = (status, format!("{message}\n")).into_response();
    response.headers_mut().insert(
        CACHE_CONTROL,
        HeaderValue::from_static("no-store,no-transform"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::HttpBody as _, http::HeaderValue};

    #[test]
    fn parses_canonical_and_sharded_tiles() {
        assert!(matches!(
            parse_map_path("tiles/0/x-1/2/z3/4.prbm"),
            Some(ObjectRequest::Tile {
                lod: 0,
                x: -12,
                z: 34
            })
        ));
        assert!(matches!(
            parse_map_path("tiles/12/x-1/2/z3/4.png"),
            Some(ObjectRequest::Tile {
                lod: 12,
                x: -12,
                z: 34
            })
        ));
        for invalid in [
            "tiles/0/x-1/2/z3/4.prbm.gz",
            "tiles/0/x-1/2/z3/4.prbm.zst",
            "tiles/0/x-1/2/z3/4.prbm.lz4",
            "tiles/0/x-1/2/z3/4.prbm.extra",
            "tiles/0/x12/z34.prbm",
            "tiles/00/x0/z0.prbm",
            "tiles/0/x0/z0.png",
            "tiles/1/x0/z0.prbm",
            "tiles/1/2/x-1/2/z3/4.png",
        ] {
            assert!(parse_map_path(invalid).is_none(), "{invalid} is an alias");
        }
    }

    #[test]
    fn accept_encoding_obeys_rfc_quality_and_precedence_rules() {
        let headers = HeaderMap::new();
        assert!(accepts_encoding(&headers, "gzip"));
        assert!(accepts_encoding(&headers, "identity"));

        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT_ENCODING, HeaderValue::from_static(""));
        assert!(!accepts_encoding(&headers, "gzip"));
        assert!(accepts_encoding(&headers, "identity"));

        let mut headers = HeaderMap::new();
        headers.append(ACCEPT_ENCODING, HeaderValue::from_static("*;q=1"));
        headers.append(
            ACCEPT_ENCODING,
            HeaderValue::from_static("gzip;Q=0, zstd;q=0.5"),
        );
        assert!(!accepts_encoding(&headers, "gzip"));
        assert!(accepts_encoding(&headers, "zstd"));

        let mut headers = HeaderMap::new();
        headers.insert(
            ACCEPT_ENCODING,
            HeaderValue::from_static("gzip;q=0.001, *;q=0"),
        );
        assert!(accepts_encoding(&headers, "gzip"));
        assert!(!accepts_encoding(&headers, "zstd"));

        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT_ENCODING, HeaderValue::from_static("gzip"));
        assert!(accepts_encoding(&headers, "identity"));

        headers.insert(ACCEPT_ENCODING, HeaderValue::from_static("*;q=0"));
        assert!(!accepts_encoding(&headers, "identity"));

        headers.insert(
            ACCEPT_ENCODING,
            HeaderValue::from_static("identity;q=0, *;q=1"),
        );
        assert!(!accepts_encoding(&headers, "identity"));
    }

    fn metadata() -> ObjectMetadata {
        ObjectMetadata {
            encoding: crate::config::StoredEncoding::None,
            content_hash: Some("current".to_owned()),
            updated_at: Some(UNIX_EPOCH + std::time::Duration::from_secs(100)),
            content_length: Some(10),
            class: ObjectClass::Settings,
        }
    }

    #[test]
    fn conditional_requests_use_weak_etag_lists_and_date_precedence() {
        let metadata = metadata();
        let mut headers = HeaderMap::new();
        headers.insert(
            IF_NONE_MATCH,
            HeaderValue::from_static("\"other\", W/\"current\""),
        );
        assert!(is_not_modified(&headers, &metadata));

        headers.insert(IF_NONE_MATCH, HeaderValue::from_static("*"));
        assert!(is_not_modified(&headers, &metadata));
        let mut metadata_without_validators = metadata.clone();
        metadata_without_validators.content_hash = None;
        assert!(is_not_modified(&headers, &metadata_without_validators));

        headers.insert(IF_NONE_MATCH, HeaderValue::from_static("\"other\""));
        headers.insert(
            IF_MODIFIED_SINCE,
            HeaderValue::from_static("Thu, 01 Jan 1970 00:03:20 GMT"),
        );
        assert!(!is_not_modified(&headers, &metadata));

        headers.remove(IF_NONE_MATCH);
        assert!(is_not_modified(&headers, &metadata));
        headers.insert(
            IF_MODIFIED_SINCE,
            HeaderValue::from_static("Thu, 01 Jan 1970 00:00:50 GMT"),
        );
        assert!(!is_not_modified(&headers, &metadata));
    }

    #[test]
    fn head_response_uses_projected_content_length_without_a_body() {
        let response = metadata_only_response(
            &metadata(),
            "settings.json",
            &HeaderMap::new(),
            &Method::HEAD,
            60,
        )
        .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers().get(CONTENT_LENGTH).unwrap(), "10");
        assert_eq!(response.body().size_hint().exact(), Some(0));
    }

    #[tokio::test]
    async fn materialized_body_encoding_is_rechecked_after_the_metadata_query() {
        let permit = Arc::new(Semaphore::new(1)).acquire_owned().await.unwrap();
        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT_ENCODING, HeaderValue::from_static("gzip"));
        let mut object_metadata = metadata();
        object_metadata.encoding = crate::config::StoredEncoding::Lz4;
        let response = object_response(
            StoredObject {
                data: Bytes::from_static(b"lz4"),
                metadata: object_metadata,
            },
            "tiles/0/x0/z0.prbm",
            &headers,
            &Method::GET,
            permit,
            60,
        );

        assert_eq!(response.status(), StatusCode::NOT_ACCEPTABLE);
        assert_eq!(
            response
                .headers()
                .get("x-bluemap-required-content-encoding")
                .unwrap(),
            "lz4"
        );
    }

    #[test]
    fn lowres_mime_does_not_depend_on_zero_path_segments() {
        assert_eq!(
            content_type("tiles/10/x0/z0.png", ObjectClass::LowresTile),
            HeaderValue::from_static("image/png")
        );
        assert_eq!(
            content_type("tiles/0/x0/z0.prbm", ObjectClass::HiresTile),
            HeaderValue::from_static("application/octet-stream")
        );
    }

    #[test]
    fn map_cache_policies_match_the_java_contract_and_prevent_transforms() {
        assert_eq!(
            map_cache_control(ObjectClass::Players, 60),
            "private,no-store,no-transform"
        );
        for class in [
            ObjectClass::Markers,
            ObjectClass::Settings,
            ObjectClass::Textures,
            ObjectClass::Asset,
        ] {
            assert_eq!(map_cache_control(class, 60), "public,no-cache,no-transform");
        }
        for class in [ObjectClass::HiresTile, ObjectClass::LowresTile] {
            assert_eq!(
                map_cache_control(class, 17),
                "public,max-age=17,must-revalidate,no-transform"
            );
        }
    }

    #[test]
    fn only_content_fingerprinted_static_assets_are_immutable() {
        assert!(is_fingerprinted_static("/assets/index-DiwrgTda.js"));
        assert!(is_fingerprinted_static("/assets/index-DiwrgTda.js.map"));
        assert!(is_fingerprinted_static("/assets/theme-0123abcd.css"));
        assert!(is_fingerprinted_static("/assets/Quicksand-BuVPtn-J.ttf"));
        assert!(!is_fingerprinted_static("/"));
        assert!(!is_fingerprinted_static("/index.html"));
        assert!(!is_fingerprinted_static("/settings.json"));
        assert!(!is_fingerprinted_static("/assets/logo.png"));
        assert!(!is_fingerprinted_static("/assets/unfingerprinted.js"));
        assert!(!is_fingerprinted_static("/assets/file-not-a-build-hash.js"));
    }
}
