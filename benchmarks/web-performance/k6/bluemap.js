import http from "k6/http";
import { check } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";
import { SharedArray } from "k6/data";
import exec from "k6/execution";

const BASE_URL = requiredEnv("BASE_URL").replace(/\/+$/, "");
const PROFILE = __ENV.PROFILE || "map-data-mixed";
const RATE = positiveInteger(__ENV.RATE || "100", "RATE");
const DURATION = __ENV.DURATION || "5m";
const DURATION_SECONDS = durationSeconds(DURATION, "DURATION");
const PRE_ALLOCATED_VUS = positiveInteger(
  __ENV.PRE_ALLOCATED_VUS || "256",
  "PRE_ALLOCATED_VUS",
);
const MAX_VUS = positiveInteger(__ENV.MAX_VUS || "512", "MAX_VUS");
const VIEWERS = positiveInteger(__ENV.VIEWERS || "100", "VIEWERS");
const MARKER_INTERVAL_SECONDS = positiveInteger(
  __ENV.MARKER_INTERVAL_SECONDS || "10",
  "MARKER_INTERVAL_SECONDS",
);
const MIN_ACHIEVED_RATE_RATIO = ratio(
  __ENV.MIN_ACHIEVED_RATE_RATIO || "0.99",
  "MIN_ACHIEVED_RATE_RATIO",
);
const TRACE_SEED = __ENV.TRACE_SEED || "bluemap-web-performance-v1";
const LATENCY_P95_MS = positiveNumber(
  __ENV.LATENCY_P95_MS || "500",
  "LATENCY_P95_MS",
);
const LATENCY_P99_MS = positiveNumber(
  __ENV.LATENCY_P99_MS || "1000",
  "LATENCY_P99_MS",
);
const LARGE_OBJECT_LATENCY_P95_MS = optionalPositiveNumber(
  __ENV.LARGE_OBJECT_LATENCY_P95_MS,
  "LARGE_OBJECT_LATENCY_P95_MS",
);
const LARGE_OBJECT_LATENCY_P99_MS = optionalPositiveNumber(
  __ENV.LARGE_OBJECT_LATENCY_P99_MS,
  "LARGE_OBJECT_LATENCY_P99_MS",
);
const ENFORCE_LATENCY_GATES = booleanValue(
  __ENV.ENFORCE_LATENCY_GATES || "true",
  "ENFORCE_LATENCY_GATES",
);
const FORMAL_RUN_ID = __ENV.FORMAL_RUN_ID || "";
const REQUIRE_EDGE_BYPASS = booleanValue(
  __ENV.REQUIRE_EDGE_BYPASS || "false",
  "REQUIRE_EDGE_BYPASS",
);
const TRAFFIC_MODE = __ENV.TRAFFIC_MODE || "cloudflare-https";
const EXPERIMENT_ID = requiredEnv("EXPERIMENT_ID");
const ACCEPT_ENCODING = __ENV.ACCEPT_ENCODING || "zstd";
const STORED_ENCODING = __ENV.STORED_ENCODING || "zstd";
const CONTRACT_MODE = __ENV.CONTRACT_MODE || "enhanced";

if (TRACE_SEED.length > 128 || /[\r\n\0]/.test(TRACE_SEED)) {
  throw new Error(
    "TRACE_SEED must be at most 128 characters without control line breaks",
  );
}
if (
  FORMAL_RUN_ID.length > 0 &&
  !/^[a-z0-9][a-z0-9-]{0,62}$/.test(FORMAL_RUN_ID)
) {
  throw new Error(
    "FORMAL_RUN_ID must contain 1-63 lowercase letters, digits, or hyphens",
  );
}
if (!["cloudflare-https", "ssh-l4-traefik"].includes(TRAFFIC_MODE)) {
  throw new Error(
    "TRAFFIC_MODE must be 'cloudflare-https' or 'ssh-l4-traefik'",
  );
}
if (TRAFFIC_MODE === "ssh-l4-traefik") {
  if (BASE_URL !== "http://bluemap-test.guenter.cloud") {
    throw new Error(
      "ssh-l4-traefik requires BASE_URL=http://bluemap-test.guenter.cloud",
    );
  }
  if (REQUIRE_EDGE_BYPASS) {
    throw new Error("ssh-l4-traefik forbids REQUIRE_EDGE_BYPASS=true");
  }
} else if (FORMAL_RUN_ID.length > 0) {
  if (BASE_URL !== "https://bluemap-test.guenter.cloud") {
    throw new Error(
      "formal cloudflare-https traffic requires the exact HTTPS benchmark URL",
    );
  }
  if (!REQUIRE_EDGE_BYPASS) {
    throw new Error(
      "formal cloudflare-https traffic requires REQUIRE_EDGE_BYPASS=true",
    );
  }
}
if (!["enhanced", "legacy"].includes(CONTRACT_MODE)) {
  throw new Error("CONTRACT_MODE must be 'enhanced' or 'legacy'");
}
if (!["gzip", "zstd", "deflate", "identity"].includes(STORED_ENCODING)) {
  throw new Error(
    "STORED_ENCODING must be 'gzip', 'zstd', 'deflate', or 'identity'",
  );
}
if (PROFILE === "conditional" && CONTRACT_MODE !== "enhanced") {
  throw new Error("The conditional profile requires CONTRACT_MODE=enhanced");
}

const manifest = new SharedArray("bluemap-request-manifest", () => {
  const parsed = JSON.parse(open(requiredEnv("MANIFEST")));
  for (const key of [
    "mapIds",
    "static",
    "tiles",
    "settings",
    "textures",
    "assets",
    "players",
    "markers",
  ]) {
    if (!Array.isArray(parsed[key])) {
      throw new Error(`Manifest field '${key}' must be an array`);
    }
    requireSortedUniqueStrings(parsed[key], `manifest.${key}`);
  }
  if (parsed.mapIds.length === 0)
    throw new Error("Manifest selects no map ids");
  if (new Set(parsed.mapIds).size !== parsed.mapIds.length) {
    throw new Error("Manifest mapIds contains duplicates");
  }
  if (parsed.tiles.length === 0) throw new Error("Manifest contains no tiles");
  for (const key of ["hotTile", "largeTile", "largeObject", "missingTile"]) {
    if (typeof parsed[key] !== "string" || parsed[key].length === 0) {
      throw new Error(`Manifest field '${key}' must be a non-empty string`);
    }
  }
  validateMapRoutes(parsed);
  return [parsed];
})[0];

requireProfileInputs();

const unexpectedStatus = new Rate("bluemap_unexpected_status");
const status200 = new Counter("bluemap_status_200");
const status204 = new Counter("bluemap_status_204");
const status304 = new Counter("bluemap_status_304");
const status406 = new Counter("bluemap_status_406");
const requestTtfb = new Trend("bluemap_ttfb", true);
const edgeCacheViolation = new Rate("bluemap_edge_cache_violation");
const edgeMitigation = new Rate("bluemap_edge_mitigation");
const prohibitedEdgeHeader = new Rate("bluemap_prohibited_edge_header");
const storedContentEncodingViolation = new Rate(
  "bluemap_stored_content_encoding_violation",
);
const originServedCacheStatuses = new Set([
  "BYPASS",
  "DYNAMIC",
  "EXPIRED",
  "MISS",
]);
const performanceUserAgent =
  FORMAL_RUN_ID.length > 0
    ? `BlueMap-Performance/${FORMAL_RUN_ID}/${EXPERIMENT_ID}`
    : `BlueMap-Performance/${EXPERIMENT_ID}`;

const requestParams = {
  headers: {
    "Accept-Encoding": ACCEPT_ENCODING,
    "User-Agent": performanceUserAgent,
  },
  responseType: "none",
};

export const options = buildOptions();

export function setup() {
  if (PROFILE !== "conditional") return {};

  const path = manifest.hotTile;
  const response = timedGet(path, "conditional-seed", requestParams, "setup");
  const etag =
    response.headers.ETag || response.headers.Etag || response.headers.etag;
  if (response.status !== 200 || !etag) {
    throw new Error(
      `${path}: conditional pre-seed requires a 200 response with an ETag`,
    );
  }
  return { conditionalEtag: etag };
}

export default function (setupData) {
  switch (PROFILE) {
    case "static":
      request(deterministicEntry(manifest.static, "static"), "static", [200]);
      break;
    case "hot-tile":
      request(manifest.hotTile, "tile-hot", [200]);
      break;
    case "random-tiles":
      request(
        deterministicEntry(manifest.tiles, "random-tile"),
        "tile-random",
        [200],
      );
      break;
    case "large-tile":
      request(manifest.largeTile, "tile-large", [200]);
      break;
    case "settings":
      request(deterministicEntry(manifest.settings, "settings"), "settings", [
        200,
      ]);
      break;
    case "textures":
      request(deterministicEntry(manifest.textures, "textures"), "textures", [
        200,
      ]);
      break;
    case "large-object":
      request(manifest.largeObject, "object-large", [200]);
      break;
    case "missing-tile":
      request(manifest.missingTile, "tile-missing", [204]);
      break;
    case "conditional":
      conditionalRequest(manifest.hotTile, setupData.conditionalEtag);
      break;
    case "map-data-mixed":
      mapDataMixedIteration();
      break;
    case "browser-mixed":
      browserMixedIteration();
      break;
    default:
      throw new Error(`Unknown PROFILE '${PROFILE}'`);
  }
}

export function pollPlayers() {
  request(deterministicEntry(manifest.players, "players"), "players", [200]);
}

export function pollMarkers() {
  request(deterministicEntry(manifest.markers, "markers"), "markers", [200]);
}

function buildOptions() {
  const summaryTrendStats = [
    "min",
    "avg",
    "med",
    "p(90)",
    "p(95)",
    "p(99)",
    "p(99.9)",
    "max",
  ];
  const commonThresholds = {
    bluemap_unexpected_status: ["rate==0"],
    "http_req_failed{traffic:workload}": ["rate<0.001"],
    dropped_iterations: ["count==0"],
  };
  const networkOptions =
    TRAFFIC_MODE === "ssh-l4-traefik"
      ? {
          maxRedirects: 0,
          hosts: {
            "bluemap-test.guenter.cloud:80": "127.0.0.1:18080",
          },
        }
      : {};
  if (REQUIRE_EDGE_BYPASS) {
    commonThresholds.bluemap_edge_cache_violation = ["rate==0"];
    commonThresholds.bluemap_edge_mitigation = ["rate==0"];
  }
  if (TRAFFIC_MODE === "ssh-l4-traefik") {
    commonThresholds.bluemap_prohibited_edge_header = ["rate==0"];
    if (CONTRACT_MODE === "enhanced" && storedCompressionProofApplicable()) {
      commonThresholds.bluemap_stored_content_encoding_violation = ["rate==0"];
    }
  }
  if (ENFORCE_LATENCY_GATES) {
    const latency = effectiveLatencyGates();
    commonThresholds["http_req_duration{traffic:workload}"] = [
      `p(95)<${latency.p95}`,
      `p(99)<${latency.p99}`,
    ];
  }

  if (PROFILE === "live-viewers") {
    const scenarios = {
      playerPolling: {
        executor: "constant-arrival-rate",
        exec: "pollPlayers",
        rate: VIEWERS,
        timeUnit: "1s",
        duration: DURATION,
        preAllocatedVUs: PRE_ALLOCATED_VUS,
        maxVUs: MAX_VUS,
        gracefulStop: "30s",
      },
    };
    const iterationThresholds = {
      "iterations{scenario:playerPolling}": [
        minimumIterationCountThreshold(VIEWERS),
      ],
    };
    if (manifest.markers.length > 0) {
      scenarios.markerPolling = {
        executor: "constant-arrival-rate",
        exec: "pollMarkers",
        rate: VIEWERS,
        timeUnit: `${MARKER_INTERVAL_SECONDS}s`,
        startTime: "500ms",
        duration: DURATION,
        preAllocatedVUs: PRE_ALLOCATED_VUS,
        maxVUs: MAX_VUS,
        gracefulStop: "30s",
      };
      iterationThresholds["iterations{scenario:markerPolling}"] = [
        minimumIterationCountThreshold(VIEWERS / MARKER_INTERVAL_SECONDS),
      ];
    }
    return {
      ...networkOptions,
      discardResponseBodies: true,
      scenarios,
      thresholds: {
        ...commonThresholds,
        ...iterationThresholds,
      },
      summaryTrendStats,
    };
  }

  return {
    ...networkOptions,
    discardResponseBodies: true,
    scenarios: {
      workload: {
        executor: "constant-arrival-rate",
        rate: RATE,
        timeUnit: "1s",
        duration: DURATION,
        preAllocatedVUs: PRE_ALLOCATED_VUS,
        maxVUs: MAX_VUS,
        gracefulStop: "30s",
      },
    },
    thresholds: {
      ...commonThresholds,
      "iterations{scenario:workload}": [minimumIterationCountThreshold(RATE)],
    },
    summaryTrendStats,
  };
}

function browserMixedIteration() {
  if (
    deterministicUnitInterval("browser-class") < 0.15 &&
    manifest.static.length > 0
  ) {
    request(deterministicEntry(manifest.static, "browser-static"), "static", [
      200,
    ]);
    return;
  }
  mapDataMixedIteration();
}

function mapDataMixedIteration() {
  const available = [];
  addWeighted(available, manifest.tiles, "tile-mixed", 80);
  addWeighted(available, manifest.settings, "settings", 8);
  addWeighted(available, manifest.textures, "textures", 1);
  addWeighted(available, manifest.assets, "map-asset", 5);
  addWeighted(available, manifest.players, "players", 4);
  addWeighted(available, manifest.markers, "markers", 2);

  const totalWeight = available.reduce((sum, entry) => sum + entry.weight, 0);
  let sample = deterministicUnitInterval("map-data-class") * totalWeight;
  for (const entry of available) {
    sample -= entry.weight;
    if (sample < 0) {
      request(
        deterministicEntry(entry.paths, `map-data-path:${entry.endpointClass}`),
        entry.endpointClass,
        [200],
      );
      return;
    }
  }
  throw new Error("Map-data workload has no selectable routes");
}

function addWeighted(target, paths, endpointClass, weight) {
  if (paths.length > 0) {
    target.push({ paths, endpointClass, weight });
  }
}

function conditionalRequest(path, etag) {
  if (!etag)
    throw new Error("Conditional workload did not receive its pre-seeded ETag");
  const headers = { ...requestParams.headers, "If-None-Match": etag };
  const response = timedGet(path, "conditional", { ...requestParams, headers });
  recordStatus(response, [304]);
}

function request(path, endpointClass, expectedStatuses) {
  const response = timedGet(path, endpointClass, requestParams);
  recordStatus(response, expectedStatuses);
}

function timedGet(path, endpointClass, params, traffic = "workload") {
  const response = http.get(`${BASE_URL}${normalizePath(path)}`, {
    ...params,
    tags: {
      ...(params.tags || {}),
      endpoint_class: endpointClass,
      profile: PROFILE,
      contract_mode: CONTRACT_MODE,
      experiment_id: EXPERIMENT_ID,
      traffic,
    },
  });
  if (
    traffic === "workload" &&
    response.timings &&
    Number.isFinite(response.timings.waiting)
  ) {
    requestTtfb.add(response.timings.waiting, {
      endpoint_class: endpointClass,
      profile: PROFILE,
    });
  }
  if (traffic === "workload" && REQUIRE_EDGE_BYPASS) {
    recordEdgeState(response, endpointClass);
  }
  if (TRAFFIC_MODE === "ssh-l4-traefik") {
    recordProhibitedEdgeHeaders(response, endpointClass, traffic);
  }
  recordStoredContentEncoding(response, path, endpointClass, traffic);
  return response;
}

function recordStoredContentEncoding(response, path, endpointClass, traffic) {
  if (
    TRAFFIC_MODE !== "ssh-l4-traefik" ||
    CONTRACT_MODE !== "enhanced" ||
    response.status !== 200 ||
    !isStoredCompressedRoute(path)
  ) {
    return;
  }

  const actualEncoding =
    responseHeader(response, "content-encoding").trim().toLowerCase() ||
    "identity";
  const expectedEncoding = STORED_ENCODING.toLowerCase();
  const violation = actualEncoding !== expectedEncoding;
  const tags = {
    endpoint_class: endpointClass,
    profile: PROFILE,
    traffic,
    actual_encoding: actualEncoding,
    expected_encoding: expectedEncoding,
  };
  storedContentEncodingViolation.add(violation, tags);
  check(response, {
    "SSH L4 Traefik preserves stored Content-Encoding": () => !violation,
  });
}

function isStoredCompressedRoute(path) {
  const normalized = normalizePath(path);
  return (
    (manifest.tiles.includes(normalized) && /\/tiles\/0\//.test(normalized)) ||
    manifest.textures.includes(normalized)
  );
}

function storedCompressionProofApplicable() {
  switch (PROFILE) {
    case "hot-tile":
    case "conditional":
      return isStoredCompressedRoute(manifest.hotTile);
    case "random-tiles":
      return manifest.tiles.some((path) => isStoredCompressedRoute(path));
    case "large-tile":
      return isStoredCompressedRoute(manifest.largeTile);
    case "textures":
      return manifest.textures.some((path) => isStoredCompressedRoute(path));
    case "large-object":
      return isStoredCompressedRoute(manifest.largeObject);
    case "map-data-mixed":
    case "browser-mixed":
      return (
        manifest.tiles.some((path) => isStoredCompressedRoute(path)) ||
        manifest.textures.some((path) => isStoredCompressedRoute(path))
      );
    default:
      return false;
  }
}

function recordProhibitedEdgeHeaders(response, endpointClass, traffic) {
  const present = ["cf-ray", "cf-cache-status", "cf-mitigated"].filter(
    (name) => responseHeader(response, name).length > 0,
  );
  const violation = present.length > 0;
  const tags = {
    endpoint_class: endpointClass,
    profile: PROFILE,
    traffic,
    headers: present.length > 0 ? present.join(",") : "none",
  };
  prohibitedEdgeHeader.add(violation, tags);
  check(response, {
    "SSH L4 Traefik response has no Cloudflare headers": () => !violation,
  });
}

function recordEdgeState(response, endpointClass) {
  const cacheStatus = responseHeader(response, "cf-cache-status").toUpperCase();
  const cfRay = responseHeader(response, "cf-ray");
  const mitigation = responseHeader(response, "cf-mitigated").toLowerCase();
  const cacheViolation =
    cfRay.length === 0 || !originServedCacheStatuses.has(cacheStatus);
  const mitigationApplied = mitigation === "challenge";
  const tags = {
    endpoint_class: endpointClass,
    profile: PROFILE,
    cache_status: cacheStatus || "missing",
  };
  edgeCacheViolation.add(cacheViolation, tags);
  edgeMitigation.add(mitigationApplied, tags);
  check(response, {
    "Cloudflare reached and response was origin-served": () => !cacheViolation,
    "Cloudflare mitigation not applied": () => !mitigationApplied,
  });
}

function responseHeader(response, name) {
  const expected = name.toLowerCase();
  for (const [key, value] of Object.entries(response.headers || {})) {
    if (key.toLowerCase() === expected) return String(value || "");
  }
  return "";
}

function recordStatus(response, expectedStatuses) {
  const accepted = expectedStatuses.includes(response.status);
  unexpectedStatus.add(!accepted, {
    status: String(response.status),
    profile: PROFILE,
    contract_mode: CONTRACT_MODE,
  });

  switch (response.status) {
    case 200:
      status200.add(1);
      break;
    case 204:
      status204.add(1);
      break;
    case 304:
      status304.add(1);
      break;
    case 406:
      status406.add(1);
      break;
  }

  check(response, {
    "status is exactly expected": () => accepted,
  });
}

function requireProfileInputs() {
  const requiredLists = {
    static: ["static"],
    settings: ["settings"],
    textures: ["textures"],
    "live-viewers": ["players"],
  };
  for (const key of requiredLists[PROFILE] || []) {
    if (manifest[key].length === 0) {
      throw new Error(
        `Profile '${PROFILE}' requires non-empty manifest.${key}`,
      );
    }
  }
}

function effectiveLatencyGates() {
  if (PROFILE === "large-object") {
    return {
      p95: LARGE_OBJECT_LATENCY_P95_MS || LATENCY_P95_MS,
      p99: LARGE_OBJECT_LATENCY_P99_MS || LATENCY_P99_MS,
    };
  }
  return { p95: LATENCY_P95_MS, p99: LATENCY_P99_MS };
}

function minimumIterationCountThreshold(offeredRate) {
  return `count>=${offeredRate * DURATION_SECONDS * MIN_ACHIEVED_RATE_RATIO}`;
}

function validateMapRoutes(parsed) {
  const prefixes = parsed.mapIds.map((mapId) => `/maps/${mapId}/`);
  const mapRouteFields = [
    "tiles",
    "settings",
    "textures",
    "assets",
    "players",
    "markers",
  ];
  for (const field of mapRouteFields) {
    for (const path of parsed[field]) {
      if (
        typeof path !== "string" ||
        !prefixes.some((prefix) => path.startsWith(prefix))
      ) {
        throw new Error(
          `Manifest route '${path}' in ${field} does not belong to mapIds`,
        );
      }
    }
  }
  for (const field of ["hotTile", "largeTile", "largeObject", "missingTile"]) {
    const path = parsed[field];
    if (!prefixes.some((prefix) => path.startsWith(prefix))) {
      throw new Error(
        `Manifest route '${path}' in ${field} does not belong to mapIds`,
      );
    }
  }
}

function deterministicEntry(values, stream) {
  if (values.length === 0)
    throw new Error("Cannot select from an empty manifest list");
  return values[deterministicHash(stream) % values.length];
}

function deterministicUnitInterval(stream) {
  return deterministicHash(stream) / 0x100000000;
}

function deterministicHash(stream) {
  const input = [
    TRACE_SEED,
    PROFILE,
    exec.scenario.name,
    String(exec.scenario.iterationInTest),
    stream,
  ].join("\u001f");

  // FNV-1a with explicit 32-bit multiplication is stable across k6 VUs and
  // independent of VU scheduling, candidate variant, phase, and repetition.
  let hash = 0x811c9dc5;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function requireSortedUniqueStrings(values, name) {
  let previous;
  for (const value of values) {
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(`${name} must contain only non-empty strings`);
    }
    if (previous !== undefined && value <= previous) {
      throw new Error(`${name} must be sorted and contain no duplicates`);
    }
    previous = value;
  }
}

function normalizePath(path) {
  return path.startsWith("/") ? path : `/${path}`;
}

function requiredEnv(name) {
  const value = __ENV[name];
  if (!value) throw new Error(`Environment variable ${name} is required`);
  return value;
}

function positiveInteger(value, name) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

function positiveNumber(value, name) {
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive number`);
  }
  return parsed;
}

function durationSeconds(value, name) {
  const match = /^([1-9][0-9]*)(ms|s|m|h)$/.exec(value);
  if (!match) {
    throw new Error(
      `${name} must be a positive integer followed by ms, s, m, or h`,
    );
  }
  const multipliers = { ms: 0.001, s: 1, m: 60, h: 3600 };
  return Number.parseInt(match[1], 10) * multipliers[match[2]];
}

function optionalPositiveNumber(value, name) {
  if (value === undefined || value === "") return null;
  return positiveNumber(value, name);
}

function booleanValue(value, name) {
  if (value === "true") return true;
  if (value === "false") return false;
  throw new Error(`${name} must be true or false`);
}

function ratio(value, name) {
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 1) {
    throw new Error(`${name} must be greater than zero and at most one`);
  }
  return parsed;
}
