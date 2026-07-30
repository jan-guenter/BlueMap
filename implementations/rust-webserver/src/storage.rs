use std::{
    collections::HashSet,
    fs::File,
    io::{self, Read},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use bytes::Bytes;
use sqlx::{
    ConnectOptions, MySqlPool, PgPool, Row,
    mysql::{MySqlConnectOptions, MySqlPoolOptions, MySqlRow, MySqlSslMode},
    postgres::{PgConnectOptions, PgPoolOptions, PgRow, PgSslMode},
};
use tokio::sync::Semaphore;

use crate::{
    AppError, Result,
    config::{Config, StorageConfig, StoredEncoding, TlsMode},
};

const REQUIRED_TABLES: [&str; 6] = [
    "bluemap_map",
    "bluemap_compression",
    "bluemap_item_storage",
    "bluemap_item_storage_data",
    "bluemap_grid_storage",
    "bluemap_grid_storage_data",
];

const MAX_BLOCKING_FILE_OPERATIONS: usize = 8;

const POSTGRES_SCHEMA_METADATA_QUERY: &str = "\
SELECT c.relname AS table_name, a.attname AS column_name
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
WHERE c.oid = ANY(ARRAY[
    pg_catalog.to_regclass('bluemap_map'),
    pg_catalog.to_regclass('bluemap_compression'),
    pg_catalog.to_regclass('bluemap_item_storage'),
    pg_catalog.to_regclass('bluemap_item_storage_data'),
    pg_catalog.to_regclass('bluemap_grid_storage'),
    pg_catalog.to_regclass('bluemap_grid_storage_data')
]::oid[])
AND a.attnum > 0 AND NOT a.attisdropped";

#[derive(Clone)]
pub enum Backend {
    File(FileBackend),
    MariaDb(SqlBackend<MySqlPool>),
    PostgreSql(SqlBackend<PgPool>),
}

#[derive(Clone)]
pub struct FileBackend {
    pub(crate) root: PathBuf,
    root_directory: Arc<File>,
    pub(crate) compression: StoredEncoding,
    workers: BlockingFileWorkers,
    max_object_bytes: u64,
    #[cfg(test)]
    body_reads: Arc<std::sync::atomic::AtomicUsize>,
    #[cfg(test)]
    metadata_reads: Arc<std::sync::atomic::AtomicUsize>,
}

#[derive(Clone)]
struct BlockingFileWorkers {
    permits: Arc<Semaphore>,
}

#[derive(Clone)]
pub struct SqlBackend<P> {
    pool: P,
    metadata: SqlMetadata,
    max_object_bytes: i64,
}

#[derive(Clone, Copy, Debug, Default)]
struct SqlMetadata {
    item_content_hash: bool,
    item_updated_at: bool,
    grid_content_hash: bool,
    grid_updated_at: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ObjectClass {
    HiresTile,
    LowresTile,
    Asset,
    Textures,
    Settings,
    Markers,
    Players,
}

#[derive(Clone, Debug)]
pub struct StoredObject {
    pub data: Bytes,
    pub metadata: ObjectMetadata,
}

#[derive(Clone, Debug)]
pub struct ObjectMetadata {
    pub encoding: StoredEncoding,
    pub content_hash: Option<String>,
    pub updated_at: Option<SystemTime>,
    pub content_length: Option<u64>,
    pub class: ObjectClass,
}

#[derive(Clone, Debug)]
pub enum ObjectRequest {
    Tile { lod: i32, x: i32, z: i32 },
    Settings,
    Textures,
    Markers,
    Players,
    Asset(String),
}

impl Backend {
    pub async fn connect(config: &Config) -> Result<Self> {
        match &config.storage {
            StorageConfig::File { root, compression } => Ok(Self::File(
                FileBackend::open(
                    root.clone(),
                    *compression,
                    config.max_in_flight_requests,
                    config.max_object_bytes,
                )
                .await?,
            )),
            StorageConfig::Mariadb {
                host,
                port,
                database,
                credentials,
                max_connections,
                connect_timeout_seconds,
                tls,
            } => {
                let username = credentials.username()?;
                let password = credentials.password()?;
                let mut options = MySqlConnectOptions::new()
                    .host(host)
                    .port(*port)
                    .database(database)
                    .username(&username)
                    .password(&password)
                    .ssl_mode(mysql_tls_mode(tls.mode));
                if let Some(ca) = &tls.ca {
                    options = options.ssl_ca(ca);
                }
                if let Some(cert) = &tls.client_cert {
                    options = options.ssl_client_cert(cert);
                }
                if let Some(key) = &tls.client_key {
                    options = options.ssl_client_key(key);
                }
                options = options.disable_statement_logging();

                let pool = MySqlPoolOptions::new()
                    .min_connections(0)
                    .max_connections((*max_connections).max(1))
                    .acquire_timeout(Duration::from_secs(*connect_timeout_seconds))
                    .connect_with(options)
                    .await
                    .map_err(AppError::Database)?;
                let metadata = mysql_schema_metadata(&pool).await?;
                Ok(Self::MariaDb(SqlBackend {
                    pool,
                    metadata,
                    max_object_bytes: config.max_object_bytes as i64,
                }))
            }
            StorageConfig::Postgresql {
                host,
                port,
                database,
                credentials,
                max_connections,
                connect_timeout_seconds,
                tls,
            } => {
                let username = credentials.username()?;
                let password = credentials.password()?;
                let mut options = PgConnectOptions::new()
                    .host(host)
                    .port(*port)
                    .database(database)
                    .username(&username)
                    .password(&password)
                    .ssl_mode(postgres_tls_mode(tls.mode));
                if let Some(ca) = &tls.ca {
                    options = options.ssl_root_cert(ca);
                }
                if let Some(cert) = &tls.client_cert {
                    options = options.ssl_client_cert(cert);
                }
                if let Some(key) = &tls.client_key {
                    options = options.ssl_client_key(key);
                }
                options = options.disable_statement_logging();

                let pool = PgPoolOptions::new()
                    .min_connections(0)
                    .max_connections((*max_connections).max(1))
                    .acquire_timeout(Duration::from_secs(*connect_timeout_seconds))
                    .connect_with(options)
                    .await
                    .map_err(AppError::Database)?;
                let metadata = postgres_schema_metadata(&pool).await?;
                Ok(Self::PostgreSql(SqlBackend {
                    pool,
                    metadata,
                    max_object_bytes: config.max_object_bytes as i64,
                }))
            }
        }
    }

    pub async fn metadata(
        &self,
        map_id: &str,
        request: ObjectRequest,
    ) -> Result<Option<ObjectMetadata>> {
        match self {
            Self::File(backend) => backend.metadata(map_id, request).await,
            Self::MariaDb(backend) => backend.metadata_mysql(map_id, request).await,
            Self::PostgreSql(backend) => backend.metadata_postgres(map_id, request).await,
        }
    }

    pub async fn read(&self, map_id: &str, request: ObjectRequest) -> Result<Option<StoredObject>> {
        match self {
            Self::File(backend) => backend.read(map_id, request).await,
            Self::MariaDb(backend) => backend.read_mysql(map_id, request).await,
            Self::PostgreSql(backend) => backend.read_postgres(map_id, request).await,
        }
    }

    pub async fn validate(&self, map_ids: &[&str]) -> Result<()> {
        self.probe().await?;
        for map_id in map_ids {
            if self
                .metadata(map_id, ObjectRequest::Settings)
                .await?
                .is_none()
            {
                return Err(AppError::InvalidSchema(format!(
                    "configured map {map_id:?} has no settings object"
                )));
            }
        }
        Ok(())
    }

    pub async fn probe(&self) -> Result<()> {
        match self {
            Self::File(backend) => backend.probe().await,
            Self::MariaDb(backend) => {
                sqlx::query("SELECT 1")
                    .execute(&backend.pool)
                    .await
                    .map_err(AppError::Database)?;
                Ok(())
            }
            Self::PostgreSql(backend) => {
                sqlx::query("SELECT 1")
                    .execute(&backend.pool)
                    .await
                    .map_err(AppError::Database)?;
                Ok(())
            }
        }
    }

    pub async fn close(&self) {
        match self {
            Self::File(_) => {}
            Self::MariaDb(backend) => backend.pool.close().await,
            Self::PostgreSql(backend) => backend.pool.close().await,
        }
    }
}

impl FileBackend {
    pub(crate) async fn open(
        root: PathBuf,
        compression: StoredEncoding,
        worker_limit: usize,
        max_object_bytes: u64,
    ) -> Result<Self> {
        let workers = BlockingFileWorkers::new(worker_limit);
        let open_path = root.clone();
        let root_directory = workers
            .run("file-storage startup", move || {
                let root_directory = File::open(&open_path)
                    .map_err(|source| AppError::StorageIo(open_path.clone(), source))?;
                let metadata = root_directory
                    .metadata()
                    .map_err(|source| AppError::StorageIo(open_path.clone(), source))?;
                if !metadata.is_dir() {
                    return Err(AppError::InvalidConfig(format!(
                        "file storage root {} is not a directory",
                        open_path.display()
                    )));
                }
                Ok(Arc::new(root_directory))
            })
            .await?;
        Ok(Self {
            root,
            root_directory,
            compression,
            workers,
            max_object_bytes,
            #[cfg(test)]
            body_reads: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
            #[cfg(test)]
            metadata_reads: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        })
    }

    async fn probe(&self) -> Result<()> {
        let root = self.root_directory.clone();
        let display_path = self.root.clone();
        self.workers
            .run("file-storage probe", move || {
                probe_root_directory(&root)
                    .map_err(|source| AppError::StorageIo(display_path, source))
            })
            .await
    }

    async fn metadata(
        &self,
        map_id: &str,
        request: ObjectRequest,
    ) -> Result<Option<ObjectMetadata>> {
        #[cfg(test)]
        self.metadata_reads
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let (relative_path, encoding, class) = self.object_path(map_id, request)?;
        let root = self.root_directory.clone();
        let display_path = self.root.join(&relative_path);
        let root_path = self.root.clone();
        self.workers
            .run("file-storage metadata", move || {
                let file = match open_beneath(&root, &root_path, &relative_path) {
                    Ok(file) => file,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
                    Err(source) => return Err(AppError::StorageIo(display_path.clone(), source)),
                };
                let metadata = file
                    .metadata()
                    .map_err(|source| AppError::StorageIo(display_path, source))?;
                if !metadata.is_file() {
                    return Ok(None);
                }
                Ok(Some(file_metadata(&metadata, encoding, class)))
            })
            .await
    }

    async fn read(&self, map_id: &str, request: ObjectRequest) -> Result<Option<StoredObject>> {
        #[cfg(test)]
        self.body_reads
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let (relative_path, encoding, class) = self.object_path(map_id, request)?;
        let root = self.root_directory.clone();
        let display_path = self.root.join(&relative_path);
        let root_path = self.root.clone();
        let max_object_bytes = self.max_object_bytes;
        self.workers
            .run("file-storage read", move || {
                let mut file = match open_beneath(&root, &root_path, &relative_path) {
                    Ok(file) => file,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
                    Err(source) => return Err(AppError::StorageIo(display_path.clone(), source)),
                };
                let stat = file
                    .metadata()
                    .map_err(|source| AppError::StorageIo(display_path.clone(), source))?;
                if !stat.is_file() {
                    return Ok(None);
                }
                ensure_object_size(stat.len(), max_object_bytes)?;
                let capacity = usize::try_from(stat.len()).unwrap_or(0);
                let mut data = Vec::with_capacity(capacity);
                (&mut file)
                    .take(max_object_bytes + 1)
                    .read_to_end(&mut data)
                    .map_err(|source| AppError::StorageIo(display_path.clone(), source))?;
                ensure_object_size(data.len() as u64, max_object_bytes)?;
                let final_stat = file
                    .metadata()
                    .map_err(|source| AppError::StorageIo(display_path.clone(), source))?;
                if !file_metadata_matches(&stat, &final_stat)
                    || final_stat.len() != data.len() as u64
                {
                    return Err(AppError::StorageIo(
                        display_path,
                        io::Error::other("file changed while it was being read"),
                    ));
                }
                let mut metadata = file_metadata(&final_stat, encoding, class);
                metadata.content_length = Some(data.len() as u64);
                Ok(Some(StoredObject {
                    data: Bytes::from(data),
                    metadata,
                }))
            })
            .await
    }

    #[cfg(test)]
    pub(crate) fn body_read_count(&self) -> usize {
        self.body_reads.load(std::sync::atomic::Ordering::Relaxed)
    }

    #[cfg(test)]
    fn metadata_read_count(&self) -> usize {
        self.metadata_reads
            .load(std::sync::atomic::Ordering::Relaxed)
    }

    fn object_path(
        &self,
        map_id: &str,
        request: ObjectRequest,
    ) -> Result<(PathBuf, StoredEncoding, ObjectClass)> {
        let map_root = PathBuf::from(map_id);
        let object = match request {
            ObjectRequest::Tile { lod, x, z } => {
                if lod < 0 {
                    return Err(AppError::InvalidPath);
                }
                let (suffix, encoding) = if lod == 0 {
                    (
                        format!(".prbm{}", self.compression.file_suffix()?),
                        self.compression,
                    )
                } else {
                    (".png".to_owned(), StoredEncoding::None)
                };
                (
                    grid_item_path(&map_root.join("tiles").join(lod.to_string()), x, z, &suffix),
                    encoding,
                    if lod == 0 {
                        ObjectClass::HiresTile
                    } else {
                        ObjectClass::LowresTile
                    },
                )
            }
            ObjectRequest::Settings => (
                map_root.join("settings.json"),
                StoredEncoding::None,
                ObjectClass::Settings,
            ),
            ObjectRequest::Textures => (
                map_root.join(format!("textures.json{}", self.compression.file_suffix()?)),
                self.compression,
                ObjectClass::Textures,
            ),
            ObjectRequest::Markers => (
                map_root.join("live/markers.json"),
                StoredEncoding::None,
                ObjectClass::Markers,
            ),
            ObjectRequest::Players => (
                map_root.join("live/players.json"),
                StoredEncoding::None,
                ObjectClass::Players,
            ),
            ObjectRequest::Asset(name) => (
                map_root.join("assets").join(escape_asset_name(&name)),
                StoredEncoding::None,
                ObjectClass::Asset,
            ),
        };
        if object
            .0
            .components()
            .any(|component| !matches!(component, std::path::Component::Normal(_)))
        {
            return Err(AppError::InvalidPath);
        }
        Ok(object)
    }
}

impl BlockingFileWorkers {
    fn new(configured_limit: usize) -> Self {
        Self {
            permits: Arc::new(Semaphore::new(
                configured_limit.clamp(1, MAX_BLOCKING_FILE_OPERATIONS),
            )),
        }
    }

    async fn run<T>(
        &self,
        operation_name: &'static str,
        operation: impl FnOnce() -> Result<T> + Send + 'static,
    ) -> Result<T>
    where
        T: Send + 'static,
    {
        let permit = self.permits.clone().acquire_owned().await.map_err(|_| {
            AppError::InvalidConfig("file-storage worker limiter is closed".to_owned())
        })?;
        tokio::task::spawn_blocking(move || {
            // The worker owns the permit. Cancelling or timing out its async
            // caller cannot admit another blocking syscall until this one ends.
            let _permit = permit;
            operation()
        })
        .await
        .map_err(|error| {
            AppError::InvalidConfig(format!("{operation_name} worker failed: {error}"))
        })?
    }

    #[cfg(test)]
    fn available_permits(&self) -> usize {
        self.permits.available_permits()
    }
}

fn ensure_object_size(actual: u64, limit: u64) -> Result<()> {
    if actual > limit {
        Err(AppError::ObjectTooLarge { actual, limit })
    } else {
        Ok(())
    }
}

fn file_metadata(
    metadata: &std::fs::Metadata,
    encoding: StoredEncoding,
    class: ObjectClass,
) -> ObjectMetadata {
    let updated_at = metadata.modified().ok();
    ObjectMetadata {
        encoding,
        content_hash: file_content_hash(metadata),
        updated_at,
        content_length: Some(metadata.len()),
        class,
    }
}

#[cfg(unix)]
fn file_metadata_matches(before: &std::fs::Metadata, after: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    before.len() == after.len()
        && before.dev() == after.dev()
        && before.ino() == after.ino()
        && before.mtime() == after.mtime()
        && before.mtime_nsec() == after.mtime_nsec()
        && before.ctime() == after.ctime()
        && before.ctime_nsec() == after.ctime_nsec()
}

#[cfg(not(unix))]
fn file_metadata_matches(before: &std::fs::Metadata, after: &std::fs::Metadata) -> bool {
    before.len() == after.len() && before.modified().ok() == after.modified().ok()
}

#[cfg(unix)]
fn file_content_hash(metadata: &std::fs::Metadata) -> Option<String> {
    use std::os::unix::fs::MetadataExt;

    Some(format!(
        "W/{:x}-{:x}-{:x}-{:x}-{:x}-{:x}-{:x}",
        metadata.len(),
        metadata.dev(),
        metadata.ino(),
        metadata.mtime(),
        metadata.mtime_nsec(),
        metadata.ctime(),
        metadata.ctime_nsec()
    ))
}

#[cfg(not(unix))]
fn file_content_hash(metadata: &std::fs::Metadata) -> Option<String> {
    metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|mtime| format!("W/{:x}-{:x}", metadata.len(), mtime.as_nanos()))
}

#[cfg(unix)]
fn probe_root_directory(root: &File) -> io::Result<()> {
    use rustix::fs::{Mode, OFlags, openat};

    let reopened = openat(
        root,
        ".",
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::DIRECTORY,
        Mode::empty(),
    )
    .map_err(io::Error::from)?;
    let metadata = File::from(reopened).metadata()?;
    if !metadata.is_dir() {
        return Err(io::Error::other("storage root is no longer a directory"));
    }
    Ok(())
}

#[cfg(not(unix))]
fn probe_root_directory(root: &File) -> io::Result<()> {
    let metadata = root.try_clone()?.metadata()?;
    if !metadata.is_dir() {
        return Err(io::Error::other("storage root is no longer a directory"));
    }
    Ok(())
}

#[cfg(unix)]
fn open_beneath(root: &File, _root_path: &Path, relative_path: &Path) -> io::Result<File> {
    use rustix::fs::{Mode, OFlags, openat};

    let components: Vec<_> = relative_path
        .components()
        .map(|component| match component {
            std::path::Component::Normal(name) => Ok(name.to_owned()),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage path is not relative",
            )),
        })
        .collect::<io::Result<_>>()?;
    if components.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "storage path is empty",
        ));
    }

    let mut directory = rustix::io::dup(root).map_err(io::Error::from)?;
    for (index, component) in components.iter().enumerate() {
        let is_final = index + 1 == components.len();
        let mut flags = OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        if !is_final {
            flags |= OFlags::DIRECTORY;
        }
        directory = openat(&directory, component, flags, Mode::empty()).map_err(io::Error::from)?;
    }
    Ok(File::from(directory))
}

#[cfg(not(unix))]
fn open_beneath(_root: &File, root_path: &Path, relative_path: &Path) -> io::Result<File> {
    let canonical_root = root_path.canonicalize()?;
    let canonical_target = canonical_root.join(relative_path).canonicalize()?;
    if !canonical_target.starts_with(&canonical_root) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "storage path escapes its root",
        ));
    }
    File::open(canonical_target)
}

impl SqlBackend<MySqlPool> {
    async fn metadata_mysql(
        &self,
        map_id: &str,
        request: ObjectRequest,
    ) -> Result<Option<ObjectMetadata>> {
        self.fetch_mysql(map_id, request, false)
            .await?
            .map(|row| decode_metadata(&row))
            .transpose()
    }

    async fn read_mysql(
        &self,
        map_id: &str,
        request: ObjectRequest,
    ) -> Result<Option<StoredObject>> {
        self.fetch_mysql(map_id, request, true)
            .await?
            .map(|row| decode_row(&row, self.max_object_bytes as u64))
            .transpose()
    }

    async fn fetch_mysql(
        &self,
        map_id: &str,
        request: ObjectRequest,
        include_data: bool,
    ) -> Result<Option<MySqlRow>> {
        match request {
            ObjectRequest::Tile { lod, x, z } if lod >= 0 => {
                let storage = if lod == 0 {
                    "bluemap:hires".to_owned()
                } else {
                    format!("bluemap:lowres/{lod}")
                };
                let class = if lod == 0 { "hires" } else { "lowres" };
                let statement = mysql_grid_statement(include_data, self.metadata, class);
                let mut query = sqlx::query(&statement);
                if include_data {
                    query = query.bind(self.max_object_bytes);
                }
                let row = query
                    .bind(map_id)
                    .bind(storage)
                    .bind(x)
                    .bind(z)
                    .fetch_optional(&self.pool)
                    .await
                    .map_err(AppError::Database)?;
                Ok(row)
            }
            ObjectRequest::Tile { .. } => Ok(None),
            request => {
                let (storage, class) = item_key(request);
                let class = class.database_name();
                let statement = mysql_item_statement(include_data, self.metadata, class);
                let mut query = sqlx::query(&statement);
                if include_data {
                    query = query.bind(self.max_object_bytes);
                }
                let row = query
                    .bind(map_id)
                    .bind(storage)
                    .fetch_optional(&self.pool)
                    .await
                    .map_err(AppError::Database)?;
                Ok(row)
            }
        }
    }
}

impl SqlBackend<PgPool> {
    async fn metadata_postgres(
        &self,
        map_id: &str,
        request: ObjectRequest,
    ) -> Result<Option<ObjectMetadata>> {
        self.fetch_postgres(map_id, request, false)
            .await?
            .map(|row| decode_metadata(&row))
            .transpose()
    }

    async fn read_postgres(
        &self,
        map_id: &str,
        request: ObjectRequest,
    ) -> Result<Option<StoredObject>> {
        self.fetch_postgres(map_id, request, true)
            .await?
            .map(|row| decode_row(&row, self.max_object_bytes as u64))
            .transpose()
    }

    async fn fetch_postgres(
        &self,
        map_id: &str,
        request: ObjectRequest,
        include_data: bool,
    ) -> Result<Option<PgRow>> {
        match request {
            ObjectRequest::Tile { lod, x, z } if lod >= 0 => {
                let storage = if lod == 0 {
                    "bluemap:hires".to_owned()
                } else {
                    format!("bluemap:lowres/{lod}")
                };
                let hash = postgres_content_hash_projection(self.metadata.grid_content_hash);
                let updated = postgres_updated_at_projection(self.metadata.grid_updated_at);
                let data = postgres_data_projection(include_data, 5);
                let class = if lod == 0 { "hires" } else { "lowres" };
                let statement = format!(
                    "SELECT {data} c.key AS compression, {hash} AS content_hash, \
                     {updated} AS updated_at_millis, '{class}'::text AS object_class \
                     FROM bluemap_grid_storage_data d \
                     JOIN bluemap_map m ON d.map = m.id \
                     JOIN bluemap_grid_storage s ON d.storage = s.id \
                     JOIN bluemap_compression c ON d.compression = c.id \
                     WHERE m.map_id = $1 AND s.key = $2 AND d.x = $3 AND d.z = $4"
                );
                let mut query = sqlx::query(&statement)
                    .bind(map_id)
                    .bind(storage)
                    .bind(x)
                    .bind(z);
                if include_data {
                    query = query.bind(self.max_object_bytes);
                }
                let row = query
                    .fetch_optional(&self.pool)
                    .await
                    .map_err(AppError::Database)?;
                Ok(row)
            }
            ObjectRequest::Tile { .. } => Ok(None),
            request => {
                let (storage, class) = item_key(request);
                let hash = postgres_content_hash_projection(self.metadata.item_content_hash);
                let updated = postgres_updated_at_projection(self.metadata.item_updated_at);
                let data = postgres_data_projection(include_data, 3);
                let class = class.database_name();
                let statement = format!(
                    "SELECT {data} c.key AS compression, {hash} AS content_hash, \
                     {updated} AS updated_at_millis, '{class}'::text AS object_class \
                     FROM bluemap_item_storage_data d \
                     JOIN bluemap_map m ON d.map = m.id \
                     JOIN bluemap_item_storage s ON d.storage = s.id \
                     JOIN bluemap_compression c ON d.compression = c.id \
                     WHERE m.map_id = $1 AND s.key = $2"
                );
                let mut query = sqlx::query(&statement).bind(map_id).bind(storage);
                if include_data {
                    query = query.bind(self.max_object_bytes);
                }
                let row = query
                    .fetch_optional(&self.pool)
                    .await
                    .map_err(AppError::Database)?;
                Ok(row)
            }
        }
    }
}

fn mysql_grid_statement(include_data: bool, metadata: SqlMetadata, class: &str) -> String {
    let hash = mysql_content_hash_projection(metadata.grid_content_hash);
    let updated = mysql_updated_at_projection(metadata.grid_updated_at);
    let data = mysql_data_projection(include_data);
    format!(
        "SELECT {data} {MYSQL_COMPRESSION_PROJECTION} AS compression, \
         {hash} AS content_hash, {updated} AS updated_at_millis, \
         '{class}' AS object_class \
         FROM bluemap_grid_storage_data d \
         JOIN bluemap_map m ON d.map = m.id \
         JOIN bluemap_grid_storage s ON d.storage = s.id \
         JOIN bluemap_compression c ON d.compression = c.id \
         WHERE m.map_id = ? AND s.`key` = ? AND d.x = ? AND d.z = ?"
    )
}

fn mysql_item_statement(include_data: bool, metadata: SqlMetadata, class: &str) -> String {
    let hash = mysql_content_hash_projection(metadata.item_content_hash);
    let updated = mysql_updated_at_projection(metadata.item_updated_at);
    let data = mysql_data_projection(include_data);
    format!(
        "SELECT {data} {MYSQL_COMPRESSION_PROJECTION} AS compression, \
         {hash} AS content_hash, {updated} AS updated_at_millis, \
         '{class}' AS object_class \
         FROM bluemap_item_storage_data d \
         JOIN bluemap_map m ON d.map = m.id \
         JOIN bluemap_item_storage s ON d.storage = s.id \
         JOIN bluemap_compression c ON d.compression = c.id \
         WHERE m.map_id = ? AND s.`key` = ?"
    )
}

const MYSQL_COMPRESSION_PROJECTION: &str = "\
CAST(c.`key` AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_general_ci";

fn item_key(request: ObjectRequest) -> (String, ObjectClass) {
    match request {
        ObjectRequest::Settings => ("bluemap:settings".to_owned(), ObjectClass::Settings),
        ObjectRequest::Textures => ("bluemap:textures".to_owned(), ObjectClass::Textures),
        ObjectRequest::Markers => ("bluemap:markers".to_owned(), ObjectClass::Markers),
        ObjectRequest::Players => ("bluemap:players".to_owned(), ObjectClass::Players),
        ObjectRequest::Asset(name) => (
            format!("bluemap:asset/{}", escape_asset_name(&name)),
            ObjectClass::Asset,
        ),
        ObjectRequest::Tile { .. } => unreachable!("tiles use grid storage"),
    }
}

fn mysql_content_hash_projection(available: bool) -> &'static str {
    if available {
        "LOWER(HEX(d.content_hash))"
    } else {
        "NULL"
    }
}

fn mysql_updated_at_projection(available: bool) -> &'static str {
    if available { "d.updated_at" } else { "NULL" }
}

fn postgres_content_hash_projection(available: bool) -> &'static str {
    if available {
        "encode(d.content_hash, 'hex')"
    } else {
        "NULL::text"
    }
}

fn postgres_updated_at_projection(available: bool) -> &'static str {
    if available {
        "d.updated_at"
    } else {
        "NULL::bigint"
    }
}

fn mysql_data_projection(include_data: bool) -> String {
    if include_data {
        "CASE WHEN OCTET_LENGTH(d.data) <= ? THEN d.data ELSE NULL END AS data, \
         CAST(OCTET_LENGTH(d.data) AS SIGNED) AS content_length,"
            .to_owned()
    } else {
        "CAST(OCTET_LENGTH(d.data) AS SIGNED) AS content_length,".to_owned()
    }
}

fn postgres_data_projection(include_data: bool, parameter: usize) -> String {
    if include_data {
        format!(
            "CASE WHEN OCTET_LENGTH(d.data) <= ${parameter} THEN d.data \
             ELSE NULL::bytea END AS data, \
             CAST(OCTET_LENGTH(d.data) AS BIGINT) AS content_length,"
        )
    } else {
        "CAST(OCTET_LENGTH(d.data) AS BIGINT) AS content_length,".to_owned()
    }
}

impl ObjectClass {
    fn database_name(self) -> &'static str {
        match self {
            Self::HiresTile => "hires",
            Self::LowresTile => "lowres",
            Self::Asset => "asset",
            Self::Textures => "textures",
            Self::Settings => "settings",
            Self::Markers => "markers",
            Self::Players => "players",
        }
    }

    fn from_database_name(value: &str) -> Result<Self> {
        match value {
            "hires" => Ok(Self::HiresTile),
            "lowres" => Ok(Self::LowresTile),
            "asset" => Ok(Self::Asset),
            "textures" => Ok(Self::Textures),
            "settings" => Ok(Self::Settings),
            "markers" => Ok(Self::Markers),
            "players" => Ok(Self::Players),
            _ => Err(AppError::InvalidSchema(format!(
                "unknown internal object class {value:?}"
            ))),
        }
    }
}

fn decode_metadata<R: Row>(row: &R) -> Result<ObjectMetadata>
where
    for<'a> &'a str: sqlx::ColumnIndex<R>,
    String: for<'r> sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    i64: for<'r> sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
{
    let compression: String = row.try_get("compression").map_err(AppError::Database)?;
    let content_hash: Option<String> = row.try_get("content_hash").map_err(AppError::Database)?;
    let updated_at_millis: Option<i64> = row
        .try_get("updated_at_millis")
        .map_err(AppError::Database)?;
    let content_length: Option<i64> = row.try_get("content_length").map_err(AppError::Database)?;
    let object_class: String = row.try_get("object_class").map_err(AppError::Database)?;
    Ok(ObjectMetadata {
        encoding: StoredEncoding::from_database_key(&compression)?,
        content_hash,
        updated_at: updated_at_millis.and_then(system_time_from_epoch_millis),
        content_length: content_length
            .map(|length| {
                u64::try_from(length).map_err(|_| {
                    AppError::InvalidSchema(format!("negative object content length {length}"))
                })
            })
            .transpose()?,
        class: ObjectClass::from_database_name(&object_class)?,
    })
}

fn decode_row<R: Row>(row: &R, max_object_bytes: u64) -> Result<StoredObject>
where
    for<'a> &'a str: sqlx::ColumnIndex<R>,
    Vec<u8>: for<'r> sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    String: for<'r> sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    i64: for<'r> sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
{
    let metadata = decode_metadata(row)?;
    let content_length = metadata.content_length.ok_or_else(|| {
        AppError::InvalidSchema("SQL object body is missing its content length".to_owned())
    })?;
    let data = read_bounded_sql_body(content_length, max_object_bytes, || {
        let data: Option<Vec<u8>> = row.try_get("data").map_err(AppError::Database)?;
        data.ok_or_else(|| {
            AppError::InvalidSchema("bounded SQL object projection returned no body".to_owned())
        })
    })?;
    ensure_object_size(data.len() as u64, max_object_bytes)?;
    Ok(StoredObject {
        data: Bytes::from(data),
        metadata,
    })
}

fn read_bounded_sql_body<T>(
    content_length: u64,
    max_object_bytes: u64,
    read_body: impl FnOnce() -> Result<T>,
) -> Result<T> {
    ensure_object_size(content_length, max_object_bytes)?;
    read_body()
}

fn mysql_tls_mode(mode: TlsMode) -> MySqlSslMode {
    match mode {
        TlsMode::Disable => MySqlSslMode::Disabled,
        TlsMode::Required => MySqlSslMode::Required,
        TlsMode::VerifyCa => MySqlSslMode::VerifyCa,
        TlsMode::VerifyFull => MySqlSslMode::VerifyIdentity,
    }
}

fn postgres_tls_mode(mode: TlsMode) -> PgSslMode {
    match mode {
        TlsMode::Disable => PgSslMode::Disable,
        TlsMode::Required => PgSslMode::Require,
        TlsMode::VerifyCa => PgSslMode::VerifyCa,
        TlsMode::VerifyFull => PgSslMode::VerifyFull,
    }
}

async fn mysql_schema_metadata(pool: &MySqlPool) -> Result<SqlMetadata> {
    let rows = sqlx::query(
        "SELECT table_name, column_name FROM information_schema.columns \
         WHERE table_schema = DATABASE() AND table_name IN \
         ('bluemap_map','bluemap_compression','bluemap_item_storage',\
          'bluemap_item_storage_data','bluemap_grid_storage','bluemap_grid_storage_data')",
    )
    .fetch_all(pool)
    .await
    .map_err(AppError::Database)?;
    schema_metadata(rows.iter().map(|row| {
        (
            row.get::<String, _>("table_name"),
            row.get::<String, _>("column_name"),
        )
    }))
}

async fn postgres_schema_metadata(pool: &PgPool) -> Result<SqlMetadata> {
    let rows = sqlx::query(POSTGRES_SCHEMA_METADATA_QUERY)
        .fetch_all(pool)
        .await
        .map_err(AppError::Database)?;
    schema_metadata(rows.iter().map(|row| {
        (
            row.get::<String, _>("table_name"),
            row.get::<String, _>("column_name"),
        )
    }))
}

fn schema_metadata(columns: impl Iterator<Item = (String, String)>) -> Result<SqlMetadata> {
    let columns: HashSet<(String, String)> = columns.collect();
    let tables: HashSet<&str> = columns.iter().map(|(table, _)| table.as_str()).collect();
    let missing: Vec<&str> = REQUIRED_TABLES
        .iter()
        .copied()
        .filter(|table| !tables.contains(table))
        .collect();
    if !missing.is_empty() {
        return Err(AppError::InvalidSchema(format!(
            "missing BlueMap tables: {}",
            missing.join(", ")
        )));
    }
    Ok(SqlMetadata {
        item_content_hash: columns.contains(&(
            "bluemap_item_storage_data".to_owned(),
            "content_hash".to_owned(),
        )),
        item_updated_at: columns.contains(&(
            "bluemap_item_storage_data".to_owned(),
            "updated_at".to_owned(),
        )),
        grid_content_hash: columns.contains(&(
            "bluemap_grid_storage_data".to_owned(),
            "content_hash".to_owned(),
        )),
        grid_updated_at: columns.contains(&(
            "bluemap_grid_storage_data".to_owned(),
            "updated_at".to_owned(),
        )),
    })
}

pub fn grid_item_path(root: &Path, x: i32, z: i32, suffix: &str) -> PathBuf {
    let encoded = format!("x{x}z{z}");
    let mut parts = Vec::new();
    let mut part = String::new();
    for character in encoded.chars() {
        part.push(character);
        if character.is_ascii_digit() {
            parts.push(std::mem::take(&mut part));
        }
    }
    if let Some(last) = parts.last_mut() {
        last.push_str(suffix);
    }
    parts
        .into_iter()
        .fold(root.to_owned(), |path, part| path.join(part))
}

pub fn escape_asset_name(name: &str) -> String {
    let escaped: String = name
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '_' | '.' | '-' | '/') {
                character
            } else {
                '_'
            }
        })
        .collect();
    escaped.replace("..", "_.")
}

pub fn system_time_from_epoch_millis(epoch_millis: i64) -> Option<SystemTime> {
    if epoch_millis >= 0 {
        UNIX_EPOCH.checked_add(Duration::from_millis(epoch_millis as u64))
    } else {
        UNIX_EPOCH.checked_sub(Duration::from_millis(epoch_millis.unsigned_abs()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MYSQL_COMMAND_SET: &str = include_str!(
        "../../../core/src/main/java/de/bluecolored/bluemap/core/storage/sql/commandset/MySQLCommandSet.java"
    );
    const POSTGRES_COMMAND_SET: &str = include_str!(
        "../../../core/src/main/java/de/bluecolored/bluemap/core/storage/sql/commandset/PostgreSQLCommandSet.java"
    );

    #[test]
    fn file_grid_path_matches_java_character_sharding() {
        assert_eq!(
            grid_item_path(Path::new("/maps/world/tiles/0"), -12, 34, ".prbm.gz"),
            PathBuf::from("/maps/world/tiles/0/x-1/2/z3/4.prbm.gz")
        );
    }

    #[test]
    fn asset_escape_matches_java_rules() {
        assert_eq!(escape_asset_name("../heads/Jän.png"), "_./heads/J_n.png");
        assert_eq!(escape_asset_name("safe/path-1.png"), "safe/path-1.png");
    }

    #[test]
    fn lz4_is_a_pass_through_storage_encoding() {
        assert_eq!(
            StoredEncoding::from_database_key("bluemap:lz4").unwrap(),
            StoredEncoding::Lz4
        );
        assert_eq!(StoredEncoding::Lz4.file_suffix().unwrap(), ".lz4");
        assert_eq!(StoredEncoding::Lz4.http_name(), Some("lz4"));
    }

    #[test]
    fn postgres_metadata_uses_search_path_resolution() {
        assert!(POSTGRES_SCHEMA_METADATA_QUERY.contains("to_regclass('bluemap_map')"));
        assert!(POSTGRES_SCHEMA_METADATA_QUERY.contains("pg_catalog.pg_attribute"));
        assert!(!POSTGRES_SCHEMA_METADATA_QUERY.contains("current_schema()"));
    }

    #[test]
    fn sql_metadata_queries_measure_but_do_not_select_object_bodies() {
        let mysql_metadata = mysql_data_projection(false);
        assert_eq!(
            mysql_metadata,
            "CAST(OCTET_LENGTH(d.data) AS SIGNED) AS content_length,"
        );
        assert!(!mysql_metadata.starts_with("d.data,"));

        let postgres_metadata = postgres_data_projection(false, 0);
        assert_eq!(
            postgres_metadata,
            "CAST(OCTET_LENGTH(d.data) AS BIGINT) AS content_length,"
        );
        assert!(!postgres_metadata.starts_with("d.data,"));

        let mysql_body = mysql_data_projection(true);
        assert!(mysql_body.contains("CASE WHEN OCTET_LENGTH(d.data) <= ?"));
        assert!(mysql_body.contains("ELSE NULL END AS data"));
        let postgres_body = postgres_data_projection(true, 5);
        assert!(postgres_body.contains("CASE WHEN OCTET_LENGTH(d.data) <= $5"));
        assert!(postgres_body.contains("ELSE NULL::bytea END AS data"));
    }

    #[test]
    fn mysql_queries_project_binary_collated_keys_as_text() {
        let metadata = SqlMetadata {
            item_content_hash: true,
            item_updated_at: true,
            grid_content_hash: true,
            grid_updated_at: true,
        };
        let expected = format!("{MYSQL_COMPRESSION_PROJECTION} AS compression");
        assert_eq!(
            MYSQL_COMPRESSION_PROJECTION,
            "CAST(c.`key` AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_general_ci"
        );

        let grid = mysql_grid_statement(true, metadata, "hires");
        let item = mysql_item_statement(true, metadata, "settings");
        for statement in [&grid, &item] {
            assert!(statement.contains(&expected));
            assert!(!statement.contains("c.`key` AS compression"));
        }
        assert!(grid.contains("FROM bluemap_grid_storage_data d"));
        assert!(item.contains("FROM bluemap_item_storage_data d"));
    }

    #[test]
    fn oversized_sql_projection_is_rejected_before_body_decoding() {
        let body_decoded = std::cell::Cell::new(false);
        let error = read_bounded_sql_body(65, 64, || {
            body_decoded.set(true);
            Ok(Vec::<u8>::new())
        })
        .unwrap_err();
        assert!(matches!(
            error,
            AppError::ObjectTooLarge {
                actual: 65,
                limit: 64
            }
        ));
        assert!(!body_decoded.get());
    }

    #[test]
    fn sql_validator_projections_match_java_binary_hash_and_millisecond_schema() {
        assert!(MYSQL_COMMAND_SET.contains("content_hash` BINARY(32) NULL"));
        assert!(MYSQL_COMMAND_SET.contains("updated_at` BIGINT NULL"));
        assert!(POSTGRES_COMMAND_SET.contains("content_hash BYTEA NULL"));
        assert!(POSTGRES_COMMAND_SET.contains("updated_at BIGINT NULL"));

        assert_eq!(
            mysql_content_hash_projection(true),
            "LOWER(HEX(d.content_hash))"
        );
        assert_eq!(
            postgres_content_hash_projection(true),
            "encode(d.content_hash, 'hex')"
        );
        assert_eq!(mysql_updated_at_projection(true), "d.updated_at");
        assert_eq!(postgres_updated_at_projection(true), "d.updated_at");
        assert!(!mysql_updated_at_projection(true).contains("UNIX_TIMESTAMP"));
        assert!(!postgres_updated_at_projection(true).contains("EXTRACT"));
    }

    #[test]
    fn updated_at_is_converted_from_epoch_milliseconds() {
        assert_eq!(
            system_time_from_epoch_millis(1_234),
            Some(UNIX_EPOCH + Duration::from_millis(1_234))
        );
        assert_eq!(
            system_time_from_epoch_millis(-1_234),
            UNIX_EPOCH.checked_sub(Duration::from_millis(1_234))
        );
    }

    #[tokio::test]
    async fn recurring_file_probe_does_not_repeat_per_map_validation() {
        let temporary = tempfile::TempDir::new().unwrap();
        let maps = temporary.path().join("maps");
        let settings = maps.join("world/settings.json");
        std::fs::create_dir_all(settings.parent().unwrap()).unwrap();
        std::fs::write(&settings, b"{}").unwrap();

        let file = FileBackend::open(maps, StoredEncoding::None, 8, 64 * 1024 * 1024)
            .await
            .unwrap();
        let backend = Backend::File(file.clone());
        backend.validate(&["world"]).await.unwrap();
        assert_eq!(file.metadata_read_count(), 1);

        std::fs::remove_file(settings).unwrap();
        backend.probe().await.unwrap();
        assert_eq!(file.metadata_read_count(), 1);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn file_backend_rejects_symlink_escape() {
        use std::os::unix::fs::symlink;

        let temporary = tempfile::TempDir::new().unwrap();
        let maps = temporary.path().join("maps");
        let outside = temporary.path().join("outside");
        std::fs::create_dir_all(&maps).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(outside.join("settings.json"), b"container secret").unwrap();
        symlink(&outside, maps.join("world")).unwrap();

        let backend = FileBackend::open(maps, StoredEncoding::None, 8, 64 * 1024 * 1024)
            .await
            .unwrap();
        let error = backend
            .read("world", ObjectRequest::Settings)
            .await
            .unwrap_err();
        assert!(matches!(error, AppError::StorageIo(_, _)));
    }

    #[tokio::test]
    async fn file_body_limit_is_checked_inside_the_blocking_worker() {
        let temporary = tempfile::TempDir::new().unwrap();
        let maps = temporary.path().join("maps");
        let settings = maps.join("world/settings.json");
        std::fs::create_dir_all(settings.parent().unwrap()).unwrap();
        std::fs::write(settings, b"123456789").unwrap();

        let backend = FileBackend::open(maps, StoredEncoding::None, 8, 8)
            .await
            .unwrap();
        assert!(matches!(
            backend.read("world", ObjectRequest::Settings).await,
            Err(AppError::ObjectTooLarge {
                actual: 9,
                limit: 8
            })
        ));
    }

    #[cfg(unix)]
    #[test]
    fn opened_file_handle_is_stable_across_path_replacement() {
        use std::io::Read;

        let temporary = tempfile::TempDir::new().unwrap();
        let root_path = temporary.path().join("maps");
        std::fs::create_dir_all(root_path.join("world")).unwrap();
        let path = root_path.join("world/settings.json");
        std::fs::write(&path, b"original").unwrap();
        let root = File::open(&root_path).unwrap();
        let mut opened = open_beneath(&root, &root_path, Path::new("world/settings.json")).unwrap();

        std::fs::rename(&path, root_path.join("world/old-settings.json")).unwrap();
        std::fs::write(&path, b"replacement").unwrap();
        let mut data = String::new();
        opened.read_to_string(&mut data).unwrap();
        assert_eq!(data, "original");
    }

    #[cfg(unix)]
    #[test]
    fn file_metadata_detects_in_place_changes() {
        use std::io::Write;

        let temporary = tempfile::TempDir::new().unwrap();
        let path = temporary.path().join("settings.json");
        std::fs::write(&path, b"before").unwrap();
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        let before = file.metadata().unwrap();
        assert!(file_metadata_matches(&before, &file.metadata().unwrap()));

        file.write_all(b"-after").unwrap();
        file.sync_all().unwrap();
        let after = file.metadata().unwrap();
        assert!(!file_metadata_matches(&before, &after));
    }

    #[tokio::test]
    async fn blocking_worker_keeps_its_permit_after_async_cancellation() {
        let workers = BlockingFileWorkers::new(1);
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let release = Arc::new((std::sync::Mutex::new(false), std::sync::Condvar::new()));
        let worker_release = release.clone();
        let task_workers = workers.clone();
        let task = tokio::spawn(async move {
            task_workers
                .run("cancellation test", move || {
                    let _ = entered_tx.send(());
                    let (lock, condition) = &*worker_release;
                    let mut released = lock.lock().unwrap();
                    while !*released {
                        released = condition.wait(released).unwrap();
                    }
                    Ok(())
                })
                .await
        });

        entered_rx.await.unwrap();
        assert_eq!(workers.available_permits(), 0);
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert_eq!(workers.available_permits(), 0);

        let (lock, condition) = &*release;
        *lock.lock().unwrap() = true;
        condition.notify_all();
        tokio::time::timeout(Duration::from_secs(1), async {
            while workers.available_permits() == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(workers.available_permits(), 1);
    }

    #[test]
    fn blocking_worker_limit_is_small_and_config_bounded() {
        assert_eq!(BlockingFileWorkers::new(3).available_permits(), 3);
        assert_eq!(
            BlockingFileWorkers::new(usize::MAX).available_permits(),
            MAX_BLOCKING_FILE_OPERATIONS
        );
    }
}
