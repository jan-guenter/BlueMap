<?php

// !!! SET YOUR SQL-CONNECTION SETTINGS HERE: !!!
// Environment variables override these defaults when they are present.

function envOrDefault($name, $fallback) {
    $value = getenv($name);
    return $value === false ? $fallback : $value;
}

$driver   = envOrDefault('BLUEMAP_SQL_PDO_DRIVER', 'mysql'); // 'mysql' (MySQL/MariaDB) or 'pgsql' (PostgreSQL)
$hostname = envOrDefault('BLUEMAP_SQL_HOST', '127.0.0.1');
$port     = intval(envOrDefault('BLUEMAP_SQL_PORT', '3306'));
$username = envOrDefault('BLUEMAP_SQL_USERNAME', 'root');
$password = envOrDefault('BLUEMAP_SQL_PASSWORD', '');
$database = envOrDefault('BLUEMAP_SQL_DATABASE', 'bluemap');
$tileCacheMaxAge = max(0, intval(envOrDefault('BLUEMAP_TILE_CACHE_MAX_AGE', '60')));

// !!! END - DONT CHANGE ANYTHING AFTER THIS LINE !!!




// compression
$compressionHeaderMap = [
    "bluemap:none" => null,
    "bluemap:gzip" => "gzip",
    "bluemap:deflate" => "deflate",
    "bluemap:zstd" => "zstd",
    "bluemap:lz4" => "lz4"
];

// meta files
$metaFileKeys = [
    "settings.json" => "bluemap:settings",
    "textures.json" => "bluemap:textures",
    "live/markers.json" => "bluemap:markers",
    "live/players.json" => "bluemap:players",
];

// mime-types for meta-files
$mimeDefault = "application/octet-stream";
$mimeTypes = [
    "txt" => "text/plain",
    "css" => "text/css",
    "csv" => "text/csv",
    "htm" => "text/html",
    "html" => "text/html",
    "js" => "text/javascript",
    "xml" => "text/xml",

    "png" => "image/png",
    "jpg" => "image/jpeg",
    "jpeg" => "image/jpeg",
    "gif" => "image/gif",
    "webp" => "image/webp",
    "tif" => "image/tiff",
    "tiff" => "image/tiff",
    "svg" => "image/svg+xml",

    "json" => "application/json",

    "mp3" => "audio/mpeg",
    "oga" => "audio/ogg",
    "wav" => "audio/wav",
    "weba" => "audio/webm",

    "mp4" => "video/mp4",
    "mpeg" => "video/mpeg",
    "webm" => "video/webm",

    "ttf" => "font/ttf",
    "woff" => "font/woff",
    "woff2" => "font/woff2"
];

// some helper functions
function error($code, $message = null) {
    global $path;

    http_response_code($code);
    header("Content-Type: text/plain");
    $body = "BlueMap php-script - $code\n";
    if ($message != null) $body .= $message."\n";
    $body .= "Requested Path: $path";
    send($body);
    exit;
}

function startsWith($haystack, $needle) {
    return substr($haystack, 0, strlen($needle)) === $needle;
}

function issetOrElse(& $var, $fallback) {
    return isset($var) ? $var : $fallback;
}

function compressionHeader($compressionKey) {
    global $compressionHeaderMap;

    $compressionHeader = issetOrElse($compressionHeaderMap[$compressionKey], null);
    if ($compressionHeader)
        header("Content-Encoding: ".$compressionHeader);
}

function getMimeType($path) {
    global $mimeDefault, $mimeTypes;

    $i = strrpos($path, ".");
    if ($i === false) return $mimeDefault;

    $s = strrpos($path, "/");
    if ($s !== false && $i < $s) return $mimeDefault;

    $suffix = substr($path, $i + 1);
    if (isset($mimeTypes[$suffix]))
        return $mimeTypes[$suffix];

    return $mimeDefault;
}

function send($data) {
    if ($_SERVER['REQUEST_METHOD'] === 'HEAD') return;
    if (is_resource($data)) {
        fpassthru($data);
    } else {
        echo $data;
    }
}

function binaryValue($value) {
    if (is_resource($value)) $value = stream_get_contents($value);
    if (is_string($value) && startsWith($value, '\\x')) {
        $hex = substr($value, 2);
        if (ctype_xdigit($hex)) return hex2bin($hex);
    }
    return $value;
}

function parseQuality($value) {
    $value = trim($value);
    if (!preg_match('/^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/D', $value))
        return 0.0;
    return floatval($value);
}

function encodingAccepted($requiredEncoding) {
    $requiredEncoding = strtolower($requiredEncoding);
    $header = issetOrElse($_SERVER['HTTP_ACCEPT_ENCODING'], null);
    if ($header === null) return true;
    if (trim($header) === '') return $requiredEncoding === 'identity';

    $requiredQuality = null;
    $wildcardQuality = null;
    foreach (explode(',', $header) as $entry) {
        $parts = explode(';', trim($entry));
        $encoding = strtolower(trim(array_shift($parts)));
        if ($encoding === '') continue;

        $quality = 1.0;
        $qualitySeen = false;
        foreach ($parts as $parameter) {
            $pair = explode('=', trim($parameter), 2);
            if (count($pair) === 2 && strtolower(trim($pair[0])) === 'q') {
                $quality = $qualitySeen ? 0.0 : parseQuality($pair[1]);
                $qualitySeen = true;
            }
        }

        if ($encoding === $requiredEncoding)
            $requiredQuality = $requiredQuality === null
                ? $quality
                : max($requiredQuality, $quality);
        if ($encoding === '*')
            $wildcardQuality = $wildcardQuality === null
                ? $quality
                : max($wildcardQuality, $quality);
    }

    if ($requiredQuality !== null) return $requiredQuality > 0.0;
    if ($requiredEncoding === 'identity')
        return $wildcardQuality === null || $wildcardQuality > 0.0;
    return $wildcardQuality !== null && $wildcardQuality > 0.0;
}

function notAcceptable($requiredEncoding) {
    http_response_code(406);
    header('Cache-Control: no-store');
    header('Vary: Accept-Encoding');
    header('Content-Type: application/problem+json');
    header('X-BlueMap-Required-Content-Encoding: '.$requiredEncoding);
    send(json_encode([
        'code' => 'bluemap_required_content_encoding',
        'requiredEncoding' => $requiredEncoding
    ]));
    exit;
}

function etagMatches($header, $etag) {
    foreach (explode(',', $header) as $candidate) {
        $candidate = trim($candidate);
        if (startsWith(strtoupper($candidate), 'W/')) $candidate = substr($candidate, 2);
        if ($candidate === '*') return true;
        if ($etag !== null && $candidate === $etag) return true;
    }
    return false;
}

function parseHttpDate($value) {
    $timezone = new DateTimeZone('UTC');
    $formats = [
        ['!D, d M Y H:i:s \G\M\T', trim($value)],
        ['!l, d-M-y H:i:s \G\M\T', trim($value)],
        ['!D M j H:i:s Y', preg_replace('/\s+/', ' ', trim($value))]
    ];

    foreach ($formats as [$format, $candidate]) {
        $date = DateTimeImmutable::createFromFormat($format, $candidate, $timezone);
        $errors = DateTimeImmutable::getLastErrors();
        if ($date !== false && ($errors === false
                || ($errors['warning_count'] === 0 && $errors['error_count'] === 0)))
            return $date->getTimestamp();
    }

    return null;
}

function isMissingCacheMetadataColumn($exception) {
    $sqlState = strtoupper(strval($exception->getCode()));
    if ($sqlState === '42S22' || $sqlState === '42703') return true;
    if (isset($exception->errorInfo[1]) && intval($exception->errorInfo[1]) === 1054)
        return true;

    $message = strtolower($exception->getMessage());
    $mentionsMetadata = strpos($message, 'content_hash') !== false
        || strpos($message, 'updated_at') !== false;
    $isUnknownColumn = strpos($message, 'unknown column') !== false
        || strpos($message, 'does not exist') !== false
        || strpos($message, 'no such column') !== false;
    return $mentionsMetadata && $isUnknownColumn;
}

function fetchStoredRow($sql, $query, $legacyQuery, $parameters) {
    foreach ([$query, $legacyQuery] as $attempt => $statementSql) {
        try {
            $statement = $sql->prepare($statementSql);
            foreach ($parameters as $name => [$value, $type])
                $statement->bindValue($name, $value, $type);
            $statement->setFetchMode(PDO::FETCH_ASSOC);
            $statement->execute();
            $line = $statement->fetch();
            return $line === false ? null : $line;
        } catch (PDOException $exception) {
            if ($attempt > 0 || !isMissingCacheMetadataColumn($exception))
                throw $exception;
        }
    }

    return null;
}

function sendStored($line, $contentType, $cacheControl) {
    $compression = $line['key'];
    $requiredEncoding = $compression === 'bluemap:none'
        ? 'identity'
        : issetOrElse($GLOBALS['compressionHeaderMap'][$compression], null);
    if ($requiredEncoding === null)
        error(500, 'Unsupported storage compression');
    if (!encodingAccepted($requiredEncoding))
        notAcceptable($requiredEncoding);

    $contentHash = binaryValue($line['content_hash']);
    $etag = $contentHash === null ? null : '"'.bin2hex($contentHash).'"';
    $updatedAt = $line['updated_at'] === null ? 0 : intval($line['updated_at']);
    $lastModified = $updatedAt <= 0
        ? null
        : gmdate('D, d M Y H:i:s', intdiv($updatedAt, 1000)).' GMT';

    $notModified = false;
    $ifNoneMatch = issetOrElse($_SERVER['HTTP_IF_NONE_MATCH'], null);
    if ($ifNoneMatch !== null) {
        $notModified = etagMatches($ifNoneMatch, $etag);
    } else {
        $ifModifiedSince = issetOrElse($_SERVER['HTTP_IF_MODIFIED_SINCE'], null);
        if ($ifModifiedSince !== null && $updatedAt > 0) {
            $since = parseHttpDate($ifModifiedSince);
            $notModified = $since !== null
                && $since >= intdiv($updatedAt, 1000);
        }
    }

    header('Cache-Control: '.$cacheControl);
    header('Vary: Accept-Encoding');
    header('Content-Type: '.$contentType);
    compressionHeader($compression);
    if ($etag !== null) header('ETag: '.$etag);
    if ($lastModified !== null) header('Last-Modified: '.$lastModified);

    if ($notModified) {
        http_response_code(304);
        exit;
    }

    send($line['data']);
    exit;
}

// determine relative request-path
$root = dirname($_SERVER['PHP_SELF']);
if ($root === "/" || $root === "\\") $root = "";
$uriPath = $_SERVER['REQUEST_URI'];
$path = substr($uriPath, strlen($root));

// add /
if ($path === "") {
    header("Location: $uriPath/");
    exit;
}

// root => index.html
if ($path === "/") {
    header("Content-Type: text/html");
    send(file_get_contents("index.html"));
    exit;
}

if (startsWith($path, "/maps/")) {
    if ($_SERVER['REQUEST_METHOD'] !== 'GET' && $_SERVER['REQUEST_METHOD'] !== 'HEAD') {
        http_response_code(405);
        header('Allow: GET, HEAD');
        exit;
    }

    // determine map-path
    $pathParts = explode("/", substr($path, strlen("/maps/")), 2);
    $mapId = $pathParts[0];
    $mapPath = explode("?", $pathParts[1], 2)[0];

    // Initialize PDO
    try {
        $sql = new PDO("$driver:host=$hostname;port=$port;dbname=$database", $username, $password);
        $sql->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    } catch (PDOException $e ) { 
        error_log($e->getMessage(), 0); // Logs the detailed error message
        error(500, "Failed to connect to database");
    }

    // provide map-tiles
    if (startsWith($mapPath, "tiles/")) {

        // parse tile-coordinates
        preg_match_all("/tiles\/([\d\/]+)\/x(-?[\d\/]+)z(-?[\d\/]+).*/", $mapPath, $matches);
        $lod = intval($matches[1][0]);
        $storage = $lod === 0 ? "bluemap:hires" : "bluemap:lowres/".$lod;
        $tileX = intval(str_replace("/", "", $matches[2][0]));
        $tileZ = intval(str_replace("/", "", $matches[3][0]));

        // query for tile
        try {
            $query = "
                SELECT d.data, c.key, d.content_hash, d.updated_at
                FROM bluemap_grid_storage_data d
                INNER JOIN bluemap_map m
                 ON d.map = m.id
                INNER JOIN bluemap_grid_storage s
                 ON d.storage = s.id
                INNER JOIN bluemap_compression c
                 ON d.compression = c.id
                WHERE m.map_id = :map_id
                 AND s.key = :storage
                 AND d.x = :x
                 AND d.z = :z
            ";
            $legacyQuery = "
                SELECT d.data, c.key, NULL AS content_hash, NULL AS updated_at
                FROM bluemap_grid_storage_data d
                INNER JOIN bluemap_map m
                 ON d.map = m.id
                INNER JOIN bluemap_grid_storage s
                 ON d.storage = s.id
                INNER JOIN bluemap_compression c
                 ON d.compression = c.id
                WHERE m.map_id = :map_id
                 AND s.key = :storage
                 AND d.x = :x
                 AND d.z = :z
            ";
            $line = fetchStoredRow($sql, $query, $legacyQuery, [
                ':map_id' => [$mapId, PDO::PARAM_STR],
                ':storage' => [$storage, PDO::PARAM_STR],
                ':x' => [$tileX, PDO::PARAM_INT],
                ':z' => [$tileZ, PDO::PARAM_INT]
            ]);

            // return result
            if ($line !== null) {
                $contentType = $lod === 0 ? 'application/octet-stream' : 'image/png';
                sendStored(
                    $line,
                    $contentType,
                    'public,max-age='.$tileCacheMaxAge.',must-revalidate'
                );
            }

        } catch (PDOException $e) { 
            error_log($e->getMessage(), 0);
            error(500, "Failed to fetch data");
        }

        // no content if nothing found
        http_response_code(204);
        header('Cache-Control: no-store');
        exit;
    }

    // provide meta-files
    $storage = issetOrElse($metaFileKeys[$mapPath], null);
    if ($storage === null && startsWith($mapPath, "assets/"))
        $storage = "bluemap:asset/".substr($mapPath, strlen("assets/"));

    if ($storage !== null) {
        try {
            $query = "
                SELECT d.data, c.key, d.content_hash, d.updated_at
                FROM bluemap_item_storage_data d
                INNER JOIN bluemap_map m
                 ON d.map = m.id
                INNER JOIN bluemap_item_storage s
                 ON d.storage = s.id
                INNER JOIN bluemap_compression c
                 ON d.compression = c.id
                WHERE m.map_id = :map_id
                 AND s.key = :storage
            ";
            $legacyQuery = "
                SELECT d.data, c.key, NULL AS content_hash, NULL AS updated_at
                FROM bluemap_item_storage_data d
                INNER JOIN bluemap_map m
                 ON d.map = m.id
                INNER JOIN bluemap_item_storage s
                 ON d.storage = s.id
                INNER JOIN bluemap_compression c
                 ON d.compression = c.id
                WHERE m.map_id = :map_id
                 AND s.key = :storage
            ";
            $line = fetchStoredRow($sql, $query, $legacyQuery, [
                ':map_id' => [$mapId, PDO::PARAM_STR],
                ':storage' => [$storage, PDO::PARAM_STR]
            ]);

            if ($line !== null) {
                $cacheControl = $mapPath === 'live/players.json'
                    ? 'private,no-store'
                    : 'public,no-cache';
                sendStored($line, getMimeType($mapPath), $cacheControl);
            }
        } catch (PDOException $e) { 
            error_log($e->getMessage(), 0);
            error(500, "Failed to fetch data");
        }
    }

}

// no match => 404
error(404);
