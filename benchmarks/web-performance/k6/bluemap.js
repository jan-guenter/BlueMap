import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";
import { SharedArray } from "k6/data";

const BASE_URL = requiredEnv("BASE_URL").replace(/\/+$/, "");
const PROFILE = __ENV.PROFILE || "browser-mixed";
const RATE = positiveInteger(__ENV.RATE || "100", "RATE");
const DURATION = __ENV.DURATION || "5m";
const PRE_ALLOCATED_VUS = positiveInteger(__ENV.PRE_ALLOCATED_VUS || "32", "PRE_ALLOCATED_VUS");
const MAX_VUS = positiveInteger(__ENV.MAX_VUS || "512", "MAX_VUS");
const VIEWERS = positiveInteger(__ENV.VIEWERS || "100", "VIEWERS");
const EXPERIMENT_ID = requiredEnv("EXPERIMENT_ID");
const ACCEPT_ENCODING = __ENV.ACCEPT_ENCODING || "zstd";

const manifest = new SharedArray("bluemap-request-manifest", () => {
  const parsed = JSON.parse(open(requiredEnv("MANIFEST")));
  for (const key of ["static", "tiles", "metadata", "assets", "players", "markers"]) {
    if (!Array.isArray(parsed[key])) {
      throw new Error(`Manifest field '${key}' must be an array`);
    }
  }
  if (parsed.tiles.length === 0) throw new Error("Manifest contains no tiles");
  return [parsed];
})[0];

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

let conditionalEtag = null;
let liveIteration = 0;

export default function () {
  switch (PROFILE) {
    case "static":
      request(randomEntry(manifest.static), "static", [200, 304]);
      break;
    case "hot-tile":
      request(manifest.hotTile || manifest.tiles[0], "tile-hot", [200, 304]);
      break;
    case "random-tiles":
      request(randomEntry(manifest.tiles), "tile-random", [200, 204, 304]);
      break;
    case "large-tile":
      request(manifest.largeTile || manifest.tiles[0], "tile-large", [200, 304]);
      break;
    case "conditional":
      conditionalRequest(manifest.hotTile || manifest.tiles[0]);
      break;
    case "live-viewers":
      liveViewerIteration();
      break;
    case "browser-mixed":
      browserMixedIteration();
      break;
    default:
      throw new Error(`Unknown PROFILE '${PROFILE}'`);
  }
}

function buildOptions() {
  const commonThresholds = {
    bluemap_unexpected_status: ["rate==0"],
    http_req_failed: ["rate<0.001"],
  };

  if (PROFILE === "live-viewers") {
    return {
      discardResponseBodies: true,
      scenarios: {
        workload: {
          executor: "constant-vus",
          vus: VIEWERS,
          duration: DURATION,
          gracefulStop: "30s",
        },
      },
      thresholds: commonThresholds,
      summaryTrendStats: ["min", "avg", "med", "p(90)", "p(95)", "p(99)", "p(99.9)", "max"],
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
    thresholds: commonThresholds,
    summaryTrendStats: ["min", "avg", "med", "p(90)", "p(95)", "p(99)", "p(99.9)", "max"],
  };
}

function browserMixedIteration() {
  const sample = Math.random();
  if (sample < 0.15 && manifest.static.length > 0) {
    request(randomEntry(manifest.static), "static", [200, 304]);
  } else if (sample < 0.70) {
    request(randomEntry(manifest.tiles), "tile-mixed", [200, 204, 304]);
  } else if (sample < 0.82 && manifest.metadata.length > 0) {
    request(randomEntry(manifest.metadata), "metadata", [200, 304]);
  } else if (sample < 0.90 && manifest.assets.length > 0) {
    request(randomEntry(manifest.assets), "map-asset", [200, 304]);
  } else if (sample < 0.97 && manifest.players.length > 0) {
    request(randomEntry(manifest.players), "players", [200]);
  } else if (manifest.markers.length > 0) {
    request(randomEntry(manifest.markers), "markers", [200, 304]);
  } else {
    request(randomEntry(manifest.tiles), "tile-mixed", [200, 204, 304]);
  }
}

function liveViewerIteration() {
  if (manifest.players.length > 0) {
    request(randomEntry(manifest.players), "players", [200]);
  }
  if (liveIteration % 10 === 0 && manifest.markers.length > 0) {
    request(randomEntry(manifest.markers), "markers", [200, 304]);
  }
  liveIteration += 1;
  sleep(1);
}

function conditionalRequest(path) {
  const headers = { ...requestParams.headers };
  if (conditionalEtag !== null) headers["If-None-Match"] = conditionalEtag;

  const response = timedGet(path, "conditional", { ...requestParams, headers });
  recordStatus(response, conditionalEtag === null ? [200] : [304]);
  const responseEtag = response.headers.ETag || response.headers.Etag || response.headers.etag;
  if (response.status === 200 && responseEtag) {
    conditionalEtag = responseEtag;
  }
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
  unexpectedStatus.add(!accepted, { status: String(response.status), profile: PROFILE });

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
    "status is expected": () => accepted,
  });
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
