use std::{
    env, fs,
    net::SocketAddr,
    path::{Path, PathBuf},
    time::Duration,
};

use serde::{Deserialize, Serialize};

use crate::{AppError, Result};

const MAX_IN_FLIGHT_REQUESTS: usize = 1024;

fn default_bind() -> SocketAddr {
    "0.0.0.0:8100".parse().expect("valid default bind address")
}

fn default_web_root() -> PathBuf {
    PathBuf::from("/usr/share/bluemap-web")
}

fn default_shutdown_grace_seconds() -> u64 {
    30
}

fn default_runtime_shutdown_seconds() -> u64 {
    5
}

fn default_dependency_check_seconds() -> u64 {
    5
}

fn default_storage_timeout_seconds() -> u64 {
    30
}

fn default_max_in_flight_requests() -> usize {
    8
}

fn default_max_object_bytes() -> u64 {
    64 * 1024 * 1024
}

fn default_tile_cache_max_age_seconds() -> u64 {
    60
}

fn default_max_connections() -> u32 {
    10
}

fn default_connect_timeout_seconds() -> u64 {
    10
}

fn default_tls_mode() -> TlsMode {
    TlsMode::VerifyFull
}

fn default_use_cookies() -> bool {
    true
}

fn default_resolution() -> f32 {
    1.0
}

fn default_min_zoom_distance() -> i32 {
    5
}

fn default_max_zoom_distance() -> i32 {
    100_000
}

fn default_hires_slider_max() -> i32 {
    500
}

fn default_hires_slider_default() -> i32 {
    100
}

fn default_hires_slider_min() -> i32 {
    0
}

fn default_lowres_slider_max() -> i32 {
    7_000
}

fn default_lowres_slider_default() -> i32 {
    2_000
}

fn default_lowres_slider_min() -> i32 {
    500
}

fn default_data_root() -> String {
    "maps".to_owned()
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default = "default_bind")]
    pub bind: SocketAddr,
    #[serde(default = "default_web_root")]
    pub web_root: PathBuf,
    #[serde(default = "default_shutdown_grace_seconds")]
    pub shutdown_grace_seconds: u64,
    #[serde(default = "default_runtime_shutdown_seconds")]
    pub runtime_shutdown_seconds: u64,
    #[serde(default = "default_dependency_check_seconds")]
    pub dependency_check_seconds: u64,
    #[serde(default = "default_storage_timeout_seconds")]
    pub storage_timeout_seconds: u64,
    #[serde(default = "default_max_in_flight_requests")]
    pub max_in_flight_requests: usize,
    #[serde(default = "default_max_object_bytes")]
    pub max_object_bytes: u64,
    #[serde(default = "default_tile_cache_max_age_seconds")]
    pub tile_cache_max_age_seconds: u64,
    #[serde(default)]
    pub webapp: WebAppConfig,
    pub maps: Vec<MapConfig>,
    pub storage: StorageConfig,
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = fs::read_to_string(path)
            .map_err(|source| AppError::ConfigIo(path.to_owned(), source))?;
        let mut config: Self = toml::from_str(&raw)
            .map_err(|source| AppError::ConfigParse(path.to_owned(), source))?;
        config.validate()?;
        config.maps.sort_by_key(|map| map.sorting);
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        if self.maps.is_empty() {
            return Err(AppError::InvalidConfig(
                "at least one [[maps]] entry is required".to_owned(),
            ));
        }
        if !(1..=MAX_IN_FLIGHT_REQUESTS).contains(&self.max_in_flight_requests) {
            return Err(AppError::InvalidConfig(format!(
                "max_in_flight_requests must be between 1 and {MAX_IN_FLIGHT_REQUESTS}"
            )));
        }
        if self.max_object_bytes == 0 || self.max_object_bytes > i64::MAX as u64 {
            return Err(AppError::InvalidConfig(
                "max_object_bytes must be between 1 and 9223372036854775807".to_owned(),
            ));
        }
        if self.runtime_shutdown_seconds == 0 {
            return Err(AppError::InvalidConfig(
                "runtime_shutdown_seconds must be greater than zero".to_owned(),
            ));
        }
        if self.webapp.map_data_root != "maps" || self.webapp.live_data_root != "maps" {
            return Err(AppError::InvalidConfig(
                "webapp map_data_root and live_data_root must both be \"maps\" because the server exposes map and live data below /maps"
                    .to_owned(),
            ));
        }

        let mut ids = std::collections::HashSet::new();
        for map in &self.maps {
            if map.id.is_empty()
                || !map
                    .id
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || character == '_')
            {
                return Err(AppError::InvalidConfig(format!(
                    "map id {:?} must contain only ASCII letters, digits, and underscores",
                    map.id
                )));
            }
            if !ids.insert(&map.id) {
                return Err(AppError::InvalidConfig(format!(
                    "duplicate map id {:?}",
                    map.id
                )));
            }
        }

        if let Some(tls) = self.storage.tls() {
            let cert = tls.client_cert.is_some();
            let key = tls.client_key.is_some();
            if cert != key {
                return Err(AppError::InvalidConfig(
                    "storage.tls.client_cert and client_key must be configured together".to_owned(),
                ));
            }
            if matches!(tls.mode, TlsMode::Disable) && (tls.ca.is_some() || cert) {
                return Err(AppError::InvalidConfig(
                    "storage.tls mode=disable cannot be combined with CA or client-certificate material"
                        .to_owned(),
                ));
            }
            if matches!(tls.mode, TlsMode::VerifyCa | TlsMode::VerifyFull) && tls.ca.is_none() {
                tracing::warn!(
                    "database TLS verification uses built-in public trust roots because no custom CA was configured"
                );
            }
        }

        if let Some(credentials) = self.storage.credentials() {
            credentials.validate()?;
        }

        if matches!(self.storage, StorageConfig::Postgresql { .. }) {
            let ambient = ambient_postgres_variables(|name| env::var_os(name).is_some());
            if !ambient.is_empty() {
                return Err(AppError::InvalidConfig(format!(
                    "ambient PostgreSQL variables are not allowed; configure these values in TOML or unset them: {}",
                    ambient.join(", ")
                )));
            }
        }

        Ok(())
    }

    pub fn shutdown_grace(&self) -> Duration {
        Duration::from_secs(self.shutdown_grace_seconds)
    }

    pub fn runtime_shutdown_timeout(&self) -> Duration {
        Duration::from_secs(self.runtime_shutdown_seconds)
    }

    pub fn dependency_check_interval(&self) -> Duration {
        Duration::from_secs(self.dependency_check_seconds.max(1))
    }

    pub fn storage_timeout(&self) -> Duration {
        Duration::from_secs(self.storage_timeout_seconds.max(1))
    }
}

const AMBIENT_POSTGRES_VARIABLES: [&str; 5] = [
    "PGSSLROOTCERT",
    "PGSSLCERT",
    "PGSSLKEY",
    "PGOPTIONS",
    "PGAPPNAME",
];

fn ambient_postgres_variables(mut is_set: impl FnMut(&str) -> bool) -> Vec<&'static str> {
    AMBIENT_POSTGRES_VARIABLES
        .iter()
        .copied()
        .filter(|name| is_set(name))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal_config(storage: &str) -> String {
        format!(
            r#"
            [[maps]]
            id = "world"
            sorting = 0

            [storage]
            {storage}
            "#
        )
    }

    #[test]
    fn accepts_lz4_passthrough_and_rejects_duplicate_maps() {
        let raw = minimal_config(
            r#"
            type = "file"
            root = "/maps"
            compression = "lz4"
            "#,
        );
        let config: Config = toml::from_str(&raw).unwrap();
        config.validate().unwrap();

        let raw = format!(
            "{}\n[[maps]]\nid = \"world\"\nsorting = 1",
            minimal_config(
                r#"
                type = "file"
                root = "/maps"
                compression = "gzip"
                "#,
            )
        );
        let config: Config = toml::from_str(&raw).unwrap();
        assert!(
            config
                .validate()
                .unwrap_err()
                .to_string()
                .contains("duplicate")
        );
    }

    #[test]
    fn database_credentials_are_redacted() {
        let credentials = Credentials {
            username: Some("reader".to_owned()),
            username_env: None,
            password_env: "SECRET_PASSWORD".to_owned(),
        };
        let debug = format!("{credentials:?}");
        assert!(!debug.contains("reader"));
        assert!(debug.contains("<redacted>"));
        assert!(!debug.contains("secret-value"));
    }

    #[test]
    fn database_username_has_exactly_one_source() {
        let literal = Credentials {
            username: Some("reader".to_owned()),
            username_env: None,
            password_env: "PASSWORD".to_owned(),
        };
        assert_eq!(
            literal.username_with(|_| None).unwrap(),
            "reader".to_owned()
        );

        let from_environment = Credentials {
            username: None,
            username_env: Some("BLUEMAP_DATABASE_USERNAME".to_owned()),
            password_env: "PASSWORD".to_owned(),
        };
        assert_eq!(
            from_environment
                .username_with(|name| {
                    assert_eq!(name, "BLUEMAP_DATABASE_USERNAME");
                    Some("secret-reader".to_owned())
                })
                .unwrap(),
            "secret-reader"
        );

        for invalid in [
            Credentials {
                username: None,
                username_env: None,
                password_env: "PASSWORD".to_owned(),
            },
            Credentials {
                username: Some("reader".to_owned()),
                username_env: Some("USERNAME".to_owned()),
                password_env: "PASSWORD".to_owned(),
            },
        ] {
            let error = invalid.validate().unwrap_err().to_string();
            assert!(error.contains("exactly one"));
            assert!(!error.contains("reader"));
        }
    }

    #[test]
    fn rejects_path_like_map_ids() {
        for id in ["..", ".", "world/nether", "world-nether", "wörld"] {
            let raw = minimal_config("type = \"file\"\nroot = \"/maps\"\ncompression = \"gzip\"")
                .replace("id = \"world\"", &format!("id = {id:?}"));
            let config: Config = toml::from_str(&raw).unwrap();
            assert!(
                config
                    .validate()
                    .unwrap_err()
                    .to_string()
                    .contains("map id"),
                "{id:?} should be rejected"
            );
        }
    }

    #[test]
    fn detects_ambient_postgres_options_without_mutating_process_environment() {
        let present = ambient_postgres_variables(|name| matches!(name, "PGSSLKEY" | "PGOPTIONS"));
        assert_eq!(present, vec!["PGSSLKEY", "PGOPTIONS"]);
    }

    #[test]
    fn rejects_tls_material_when_database_tls_is_disabled() {
        for material in [
            "ca = \"/run/secrets/ca.crt\"",
            "client_cert = \"/run/secrets/tls.crt\"\nclient_key = \"/run/secrets/tls.key\"",
        ] {
            let raw = minimal_config(&format!(
                r#"
                type = "mariadb"
                host = "database"
                database = "bluemap"
                username = "reader"
                password_env = "PASSWORD"

                [storage.tls]
                mode = "disable"
                {material}
                "#
            ));
            let config: Config = toml::from_str(&raw).unwrap();
            assert!(
                config
                    .validate()
                    .unwrap_err()
                    .to_string()
                    .contains("mode=disable")
            );
        }
    }

    #[test]
    fn webapp_defaults_match_current_bluemap_defaults() {
        let defaults = WebAppConfig::default();
        assert_eq!(defaults.hires_slider_max, 500);
        assert_eq!(defaults.hires_slider_default, 100);
        assert_eq!(defaults.hires_slider_min, 0);
        assert_eq!(defaults.lowres_slider_max, 7_000);
        assert_eq!(defaults.lowres_slider_default, 2_000);
        assert_eq!(defaults.lowres_slider_min, 500);
    }

    #[test]
    fn in_flight_limit_defaults_conservatively_and_rejects_zero() {
        let raw = minimal_config("type = \"file\"\nroot = \"/maps\"\ncompression = \"gzip\"");
        let config: Config = toml::from_str(&raw).unwrap();
        assert_eq!(config.max_in_flight_requests, 8);
        assert_eq!(config.runtime_shutdown_seconds, 5);
        assert_eq!(config.max_object_bytes, 64 * 1024 * 1024);
        assert_eq!(config.tile_cache_max_age_seconds, 60);

        let mut config = config;
        config.max_in_flight_requests = 0;
        assert!(
            config
                .validate()
                .unwrap_err()
                .to_string()
                .contains("between 1 and 1024")
        );

        config.max_in_flight_requests = 1025;
        assert!(
            config
                .validate()
                .unwrap_err()
                .to_string()
                .contains("between 1 and 1024")
        );

        config.max_in_flight_requests = 8;
        config.max_object_bytes = 0;
        assert!(
            config
                .validate()
                .unwrap_err()
                .to_string()
                .contains("max_object_bytes")
        );

        config.max_object_bytes = i64::MAX as u64 + 1;
        assert!(
            config
                .validate()
                .unwrap_err()
                .to_string()
                .contains("max_object_bytes")
        );

        config.max_object_bytes = 64 * 1024 * 1024;
        config.runtime_shutdown_seconds = 0;
        assert!(
            config
                .validate()
                .unwrap_err()
                .to_string()
                .contains("runtime_shutdown_seconds")
        );
    }

    #[test]
    fn rejects_webapp_data_roots_that_do_not_match_the_http_routes() {
        for key in ["map_data_root", "live_data_root"] {
            let raw = format!(
                "{}\n[webapp]\n{key} = \"custom\"",
                minimal_config("type = \"file\"\nroot = \"/maps\"\ncompression = \"gzip\"")
            );
            let config: Config = toml::from_str(&raw).unwrap();
            assert!(
                config
                    .validate()
                    .unwrap_err()
                    .to_string()
                    .contains("must both be")
            );
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapConfig {
    pub id: String,
    #[serde(default)]
    pub sorting: i32,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WebAppConfig {
    #[serde(default = "default_use_cookies")]
    pub use_cookies: bool,
    #[serde(default)]
    pub default_to_flat_view: bool,
    #[serde(default)]
    pub start_location: Option<String>,
    #[serde(default = "default_resolution")]
    pub resolution_default: f32,
    #[serde(default = "default_min_zoom_distance")]
    pub min_zoom_distance: i32,
    #[serde(default = "default_max_zoom_distance")]
    pub max_zoom_distance: i32,
    #[serde(default = "default_hires_slider_max")]
    pub hires_slider_max: i32,
    #[serde(default = "default_hires_slider_default")]
    pub hires_slider_default: i32,
    #[serde(default = "default_hires_slider_min")]
    pub hires_slider_min: i32,
    #[serde(default = "default_lowres_slider_max")]
    pub lowres_slider_max: i32,
    #[serde(default = "default_lowres_slider_default")]
    pub lowres_slider_default: i32,
    #[serde(default = "default_lowres_slider_min")]
    pub lowres_slider_min: i32,
    #[serde(default = "default_data_root")]
    pub map_data_root: String,
    #[serde(default = "default_data_root")]
    pub live_data_root: String,
    #[serde(default)]
    pub scripts: Vec<String>,
    #[serde(default)]
    pub styles: Vec<String>,
}

impl Default for WebAppConfig {
    fn default() -> Self {
        Self {
            use_cookies: default_use_cookies(),
            default_to_flat_view: false,
            start_location: None,
            resolution_default: default_resolution(),
            min_zoom_distance: default_min_zoom_distance(),
            max_zoom_distance: default_max_zoom_distance(),
            hires_slider_max: default_hires_slider_max(),
            hires_slider_default: default_hires_slider_default(),
            hires_slider_min: default_hires_slider_min(),
            lowres_slider_max: default_lowres_slider_max(),
            lowres_slider_default: default_lowres_slider_default(),
            lowres_slider_min: default_lowres_slider_min(),
            map_data_root: default_data_root(),
            live_data_root: default_data_root(),
            scripts: Vec::new(),
            styles: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase", deny_unknown_fields)]
pub enum StorageConfig {
    File {
        root: PathBuf,
        #[serde(default)]
        compression: StoredEncoding,
    },
    Mariadb {
        host: String,
        #[serde(default = "default_mariadb_port")]
        port: u16,
        database: String,
        #[serde(flatten)]
        credentials: Credentials,
        #[serde(default = "default_max_connections")]
        max_connections: u32,
        #[serde(default = "default_connect_timeout_seconds")]
        connect_timeout_seconds: u64,
        #[serde(default)]
        tls: TlsConfig,
    },
    Postgresql {
        host: String,
        #[serde(default = "default_postgresql_port")]
        port: u16,
        database: String,
        #[serde(flatten)]
        credentials: Credentials,
        #[serde(default = "default_max_connections")]
        max_connections: u32,
        #[serde(default = "default_connect_timeout_seconds")]
        connect_timeout_seconds: u64,
        #[serde(default)]
        tls: TlsConfig,
    },
}

impl StorageConfig {
    pub fn tls(&self) -> Option<&TlsConfig> {
        match self {
            Self::File { .. } => None,
            Self::Mariadb { tls, .. } | Self::Postgresql { tls, .. } => Some(tls),
        }
    }

    fn credentials(&self) -> Option<&Credentials> {
        match self {
            Self::File { .. } => None,
            Self::Mariadb { credentials, .. } | Self::Postgresql { credentials, .. } => {
                Some(credentials)
            }
        }
    }
}

fn default_mariadb_port() -> u16 {
    3306
}

fn default_postgresql_port() -> u16 {
    5432
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Credentials {
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default)]
    pub username_env: Option<String>,
    pub password_env: String,
}

impl Credentials {
    fn validate(&self) -> Result<()> {
        match (&self.username, &self.username_env) {
            (Some(username), None) if !username.is_empty() => Ok(()),
            (None, Some(username_env)) if !username_env.is_empty() => Ok(()),
            _ => Err(AppError::InvalidConfig(
                "configure exactly one of storage.username or storage.username_env".to_owned(),
            )),
        }
    }

    pub fn username(&self) -> Result<String> {
        self.username_with(|name| env::var(name).ok())
    }

    fn username_with(&self, lookup: impl FnOnce(&str) -> Option<String>) -> Result<String> {
        self.validate()?;
        if let Some(username) = &self.username {
            return Ok(username.clone());
        }
        let username_env = self
            .username_env
            .as_deref()
            .expect("validated environment username");
        lookup(username_env).ok_or_else(|| {
            AppError::InvalidConfig(format!(
                "required username environment variable {username_env:?} is not set"
            ))
        })
    }

    pub fn password(&self) -> Result<String> {
        env::var(&self.password_env).map_err(|_| {
            AppError::InvalidConfig(format!(
                "required password environment variable {:?} is not set",
                self.password_env
            ))
        })
    }
}

impl std::fmt::Debug for Credentials {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Credentials")
            .field("username", &self.username.as_ref().map(|_| "<redacted>"))
            .field("username_env", &self.username_env)
            .field("password_env", &self.password_env)
            .field("password", &"<redacted>")
            .finish()
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TlsConfig {
    #[serde(default = "default_tls_mode")]
    pub mode: TlsMode,
    #[serde(default)]
    pub ca: Option<PathBuf>,
    #[serde(default)]
    pub client_cert: Option<PathBuf>,
    #[serde(default)]
    pub client_key: Option<PathBuf>,
}

#[derive(Clone, Copy, Debug, Default, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TlsMode {
    Disable,
    Required,
    VerifyCa,
    #[default]
    VerifyFull,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StoredEncoding {
    None,
    #[default]
    Gzip,
    Deflate,
    Zstd,
    Lz4,
}

impl StoredEncoding {
    pub fn from_database_key(key: &str) -> Result<Self> {
        match key {
            "bluemap:none" => Ok(Self::None),
            "bluemap:gzip" => Ok(Self::Gzip),
            "bluemap:deflate" => Ok(Self::Deflate),
            "bluemap:zstd" => Ok(Self::Zstd),
            "bluemap:lz4" => Ok(Self::Lz4),
            other => Err(AppError::UnsupportedEncoding(other.to_owned())),
        }
    }

    pub fn http_name(self) -> Option<&'static str> {
        match self {
            Self::None => None,
            Self::Gzip => Some("gzip"),
            Self::Deflate => Some("deflate"),
            Self::Zstd => Some("zstd"),
            Self::Lz4 => Some("lz4"),
        }
    }

    pub fn file_suffix(self) -> Result<&'static str> {
        match self {
            Self::None => Ok(""),
            Self::Gzip => Ok(".gz"),
            Self::Deflate => Ok(".deflate"),
            Self::Zstd => Ok(".zst"),
            Self::Lz4 => Ok(".lz4"),
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WebSettings<'a> {
    version: &'a str,
    use_cookies: bool,
    default_to_flat_view: bool,
    start_location: &'a Option<String>,
    resolution_default: f32,
    min_zoom_distance: i32,
    max_zoom_distance: i32,
    hires_slider_max: i32,
    hires_slider_default: i32,
    hires_slider_min: i32,
    lowres_slider_max: i32,
    lowres_slider_default: i32,
    lowres_slider_min: i32,
    map_data_root: &'a str,
    live_data_root: &'a str,
    maps: Vec<&'a str>,
    scripts: &'a [String],
    styles: &'a [String],
}

impl Config {
    pub fn web_settings<'a>(&'a self, version: &'a str) -> WebSettings<'a> {
        WebSettings {
            version,
            use_cookies: self.webapp.use_cookies,
            default_to_flat_view: self.webapp.default_to_flat_view,
            start_location: &self.webapp.start_location,
            resolution_default: self.webapp.resolution_default,
            min_zoom_distance: self.webapp.min_zoom_distance,
            max_zoom_distance: self.webapp.max_zoom_distance,
            hires_slider_max: self.webapp.hires_slider_max,
            hires_slider_default: self.webapp.hires_slider_default,
            hires_slider_min: self.webapp.hires_slider_min,
            lowres_slider_max: self.webapp.lowres_slider_max,
            lowres_slider_default: self.webapp.lowres_slider_default,
            lowres_slider_min: self.webapp.lowres_slider_min,
            map_data_root: &self.webapp.map_data_root,
            live_data_root: &self.webapp.live_data_root,
            maps: self.maps.iter().map(|map| map.id.as_str()).collect(),
            scripts: &self.webapp.scripts,
            styles: &self.webapp.styles,
        }
    }
}
