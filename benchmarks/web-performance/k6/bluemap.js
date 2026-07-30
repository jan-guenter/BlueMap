import http from "k6/http";
import { check } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";
import { SharedArray } from "k6/data";

const BASE_URL = requiredEnv("BASE_URL").replace(/\/+$/, "");
const PROFILE = __ENV.PROFILE || "map-data-mixed";
const RATE = positiveInteger(__ENV.RATE || "100", "RATE");
const DURATION = __ENV.DURATION || "5m";
const PRE_ALLOCATED_VUS = positiveInteger(__ENV.PRE_ALLOCATED_VUS || "32", "PRE_ALLOCATED_VUS");
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
const EXPERIMENT_ID = requiredEnv("EXPERIMENT_ID");
const ACCEPT_ENCODING = __ENV.ACCEPT_ENCODING || "zstd";
const CONTRACT_MODE = __ENV.CONTRACT_MODE || "enhanced";

if (!["enhanced", "legacy"].includes(CONTRACT_MODE)) {
  throw new Error("CONTRACT_MODE must be 'enhanced' or 'legacy'");
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
  }
  if (parsed.mapIds.length === 0) throw new Error("Manifest selects no map ids");
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

const requestParams = {
  headers: {
    "Accept-Encoding": ACCEPT_ENCODING,
    "User-Agent": `BlueMap-Performance/${EXPERIMENT_ID}`,
  },
  responseType: "none",
};

export const options = buildOptions();

export function setup() {
  if (PROFILE !== "conditional") return {};

  const path = manifest.hotTile;
  const response = timedGet(path, "conditional-seed", requestParams);
  recordStatus(response, [200]);
  const etag = response.headers.ETag || response.headers.Etag || response.headers.etag;
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
      request(randomEntry(manifest.static), "static", [200]);
      break;
    case "hot-tile":
      request(manifest.hotTile, "tile-hot", [200]);
      break;
    case "random-tiles":
      request(randomEntry(manifest.tiles), "tile-random", [200]);
      break;
    case "large-tile":
      request(manifest.largeTile, "tile-large", [200]);
      break;
    case "settings":
      request(randomEntry(manifest.settings), "settings", [200]);
      break;
    case "textures":
      request(randomEntry(manifest.textures), "textures", [200]);
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
  request(randomEntry(manifest.players), "players", [200]);
}

export function pollMarkers() {
  request(randomEntry(manifest.markers), "markers", [200]);
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
    http_req_failed: ["rate<0.001"],
    dropped_iterations: ["count==0"],
  };

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
    let expectedIterationsPerSecond = VIEWERS;
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
      expectedIterationsPerSecond += VIEWERS / MARKER_INTERVAL_SECONDS;
    }
    return {
      discardResponseBodies: true,
      scenarios,
      thresholds: {
        ...commonThresholds,
        iterations: [
          `rate>=${expectedIterationsPerSecond * MIN_ACHIEVED_RATE_RATIO}`,
        ],
      },
      summaryTrendStats,
    };
  }

  return {
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
      iterations: [`rate>=${RATE * MIN_ACHIEVED_RATE_RATIO}`],
    },
    summaryTrendStats,
  };
}

function browserMixedIteration() {
  if (Math.random() < 0.15 && manifest.static.length > 0) {
    request(randomEntry(manifest.static), "static", [200]);
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
  let sample = Math.random() * totalWeight;
  for (const entry of available) {
    sample -= entry.weight;
    if (sample < 0) {
      request(randomEntry(entry.paths), entry.endpointClass, [200]);
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
  if (!etag) throw new Error("Conditional workload did not receive its pre-seeded ETag");
  const headers = { ...requestParams.headers, "If-None-Match": etag };
  const response = timedGet(path, "conditional", { ...requestParams, headers });
  recordStatus(response, [304]);
}

function request(path, endpointClass, expectedStatuses) {
  const response = timedGet(path, endpointClass, requestParams);
  recordStatus(response, expectedStatuses);
}

function timedGet(path, endpointClass, params) {
  const response = http.get(`${BASE_URL}${normalizePath(path)}`, {
    ...params,
    tags: {
      endpoint_class: endpointClass,
      profile: PROFILE,
      contract_mode: CONTRACT_MODE,
      experiment_id: EXPERIMENT_ID,
    },
  });
  if (response.timings && Number.isFinite(response.timings.waiting)) {
    requestTtfb.add(response.timings.waiting, {
      endpoint_class: endpointClass,
      profile: PROFILE,
    });
  }
  return response;
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
      throw new Error(`Profile '${PROFILE}' requires non-empty manifest.${key}`);
    }
  }
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
      throw new Error(`Manifest route '${path}' in ${field} does not belong to mapIds`);
    }
  }
}

function randomEntry(values) {
  if (values.length === 0) throw new Error("Cannot select from an empty manifest list");
  return values[Math.floor(Math.random() * values.length)];
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

function ratio(value, name) {
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 1) {
    throw new Error(`${name} must be greater than zero and at most one`);
  }
  return parsed;
}
