import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";
import { SharedArray } from "k6/data";

const baseUrl = requiredEnvironment("BASE_URL").replace(/\/+$/, "");
const pathFile = requiredEnvironment("PATH_FILE");
const variant = requiredEnvironment("VARIANT");
const acceptEncoding = requiredEnvironment("ACCEPT_ENCODING");
const requiredContentEncoding = requiredEnvironment(
  "REQUIRED_CONTENT_ENCODING",
).toLowerCase();
const vus = positiveInteger(requiredEnvironment("VUS"), "VUS");
const duration = requiredEnvironment("DURATION");

const paths = new SharedArray("benchmark paths", () =>
  open(pathFile)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#")),
);

if (paths.length === 0) {
  throw new Error("PATH_FILE contains no request paths");
}

const benchmarkErrors = new Counter("benchmark_errors");

export const options = {
  vus,
  duration,
  discardResponseBodies: true,
  gracefulStop: "30s",
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
  thresholds: {
    benchmark_errors: ["count==0"],
    checks: ["rate==1"],
    http_req_failed: ["rate==0"],
  },
};

const requestParameters = {
  headers: {
    "Accept-Encoding": acceptEncoding,
    "User-Agent": `BlueMap-Throughput/1 (${variant})`,
  },
  redirects: 0,
  responseType: "none",
  tags: { variant },
};

export default function () {
  // A completed iteration always requests the complete frozen path set. This
  // keeps the object mix stable even when one target completes more iterations.
  for (const path of paths) {
    const response = http.get(`${baseUrl}${path}`, requestParameters);
    const contentEncoding = normalizeContentEncoding(
      response.headers["Content-Encoding"],
    );
    const valid =
      response.status === 200 &&
      contentEncoding === requiredContentEncoding;

    // Emit a zero sample on valid responses so the custom counter and its
    // threshold are present even in an entirely clean run.
    benchmarkErrors.add(valid ? 0 : 1);
    check(response, {
      "status is 200": (candidate) => candidate.status === 200,
      "stored content encoding is unchanged": () =>
        contentEncoding === requiredContentEncoding,
    });
  }
}

function normalizeContentEncoding(value) {
  if (value === undefined || value === null || String(value).trim() === "") {
    return "identity";
  }
  return String(value).trim().toLowerCase();
}

function requiredEnvironment(name) {
  const value = __ENV[name];
  if (value === undefined || value.trim() === "") {
    throw new Error(`${name} is required`);
  }
  return value.trim();
}

function positiveInteger(value, name) {
  if (!/^[1-9][0-9]*$/.test(value)) {
    throw new Error(`${name} must be a positive integer`);
  }
  return Number.parseInt(value, 10);
}
