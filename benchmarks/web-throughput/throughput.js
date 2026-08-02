import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";
import { SharedArray } from "k6/data";

const baseUrl = requiredEnvironment("BASE_URL").replace(/\/+$/, "");
const pathFile = requiredEnvironment("PATH_FILE");
const expectationsFile = requiredEnvironment("EXPECTATIONS_FILE");
const variant = requiredEnvironment("VARIANT");
const acceptEncoding = requiredEnvironment("ACCEPT_ENCODING");
const requiredContentEncoding = requiredEnvironment(
  "REQUIRED_CONTENT_ENCODING",
).toLowerCase();
const targetIdentityHeader = optionalEnvironment("TARGET_IDENTITY_HEADER");
const targetRuntimeIdentity = optionalEnvironment("TARGET_RUNTIME_IDENTITY");
if ((targetIdentityHeader === "") !== (targetRuntimeIdentity === "")) {
  throw new Error(
    "TARGET_IDENTITY_HEADER and TARGET_RUNTIME_IDENTITY must be enabled together",
  );
}
const vus = positiveInteger(requiredEnvironment("VUS"), "VUS");
if (vus !== 12) {
  throw new Error("VUS must be exactly 12 for the approved comparison");
}
const duration = requiredEnvironment("DURATION");

const paths = new SharedArray("benchmark paths", () =>
  open(pathFile)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#")),
);
const expectations = JSON.parse(open(expectationsFile));

if (paths.length === 0) {
  throw new Error("PATH_FILE contains no request paths");
}
if (
  expectations === null ||
  typeof expectations !== "object" ||
  expectations.formatVersion !== 2 ||
  expectations.paths === null ||
  typeof expectations.paths !== "object"
) {
  throw new Error("EXPECTATIONS_FILE is malformed");
}
for (const path of paths) {
  const expected = expectations.paths[path];
  if (
    expected === undefined ||
    !Number.isSafeInteger(expected.storedRepresentationLength) ||
    expected.storedRepresentationLength <= 0 ||
    !Number.isSafeInteger(expected.decodedContentLength) ||
    expected.decodedContentLength <= 0 ||
    typeof expected.contentType !== "string" ||
    expected.contentType.length === 0 ||
    expected.targets === null ||
    typeof expected.targets !== "object" ||
    expected.targets[variant] === null ||
    typeof expected.targets[variant] !== "object" ||
    !validNullableContentLength(
      expected.targets[variant].declaredContentLength,
    )
  ) {
    throw new Error(`EXPECTATIONS_FILE has no valid entry for ${path}`);
  }
}

const benchmarkErrors = new Counter("benchmark_errors");
const httpErrors = new Counter("benchmark_http_errors");
const transportErrors = new Counter("benchmark_transport_errors");
const proxyHeaderErrors = new Counter(
  "benchmark_proxy_header_errors",
);
const encodingErrors = new Counter("benchmark_encoding_errors");
const contentTypeErrors = new Counter("benchmark_content_type_errors");
const contentLengthErrors = new Counter("benchmark_content_length_errors");
const bodyLengthErrors = new Counter("benchmark_body_length_errors");
const identityErrors = new Counter("benchmark_identity_errors");
const cacheValidatorErrors = new Counter("benchmark_cache_validator_errors");
const droppedIterations = new Counter("benchmark_dropped_iterations");
const observedResponses = new Counter("benchmark_observed_responses");
const storedRepresentationBytes = new Counter(
  "benchmark_stored_representation_bytes",
);

export const options = {
  scenarios: {
    benchmark: {
      executor: "constant-vus",
      vus,
      duration,
      gracefulStop: "30s",
    },
  },
  discardResponseBodies: false,
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
  thresholds: {
    benchmark_errors: ["count==0"],
    benchmark_http_errors: ["count==0"],
    benchmark_transport_errors: ["count==0"],
    benchmark_proxy_header_errors: ["count==0"],
    benchmark_encoding_errors: ["count==0"],
    benchmark_content_type_errors: ["count==0"],
    benchmark_content_length_errors: ["count==0"],
    benchmark_body_length_errors: ["count==0"],
    benchmark_identity_errors: ["count==0"],
    benchmark_cache_validator_errors: ["count==0"],
    benchmark_dropped_iterations: ["count==0"],
    benchmark_observed_responses: ["count>0"],
    benchmark_stored_representation_bytes: ["count>0"],
    checks: ["rate==1"],
    http_req_failed: ["rate==0"],
  },
};

const requestParameters = {
  headers: {
    "Accept-Encoding": acceptEncoding,
    "User-Agent": "BlueMap-Throughput/2",
  },
  redirects: 0,
  responseType: "binary",
  tags: { variant },
};

export default function () {
  // One iteration always requests the complete frozen profile. Fixed VUs do
  // not drop scheduled iterations; the always-present custom counter and any
  // unexpected built-in k6 dropped_iterations evidence are both checked by
  // the Python admission logic.
  droppedIterations.add(0);
  for (const path of paths) {
    const expected = expectations.paths[path];
    const response = http.get(`${baseUrl}${path}`, requestParameters);
    const contentEncoding = normalizeContentEncoding(
      headerValue(response.headers, "Content-Encoding"),
    );
    const contentType = normalizeContentType(
      headerValue(response.headers, "Content-Type"),
    );
    const bodyLength = response.body === null ? -1 : response.body.byteLength;
    const contentLengthHeader = headerValue(response.headers, "Content-Length");
    const declaredContentLength = parseContentLength(contentLengthHeader);
    const transportValid = response.status !== 0;
    const httpValid = response.status === 200;
    const proxyHeadersValid = !hasRejectedProxyHeader(response.headers);
    const encodingValid = contentEncoding === requiredContentEncoding;
    const contentTypeValid = contentType === expected.contentType;
    // k6 transparently decodes supported Content-Encoding values, including
    // zstd. Compare that decoded body with independently decoded preflight
    // evidence. Content-Length describes the stored representation and is
    // target-specific because the unchanged PHP endpoint uses chunked framing.
    const targetExpectation = expected.targets[variant];
    const expectedContentLength = targetExpectation.declaredContentLength;
    const contentLengthValid =
      expectedContentLength === null
        ? contentLengthHeader === ""
        : declaredContentLength === expectedContentLength;
    const bodyLengthValid = bodyLength === expected.decodedContentLength;
    const identityValid =
      targetIdentityHeader === "" ||
      headerValue(response.headers, targetIdentityHeader) ===
        targetRuntimeIdentity;
    const etagValid =
      targetExpectation.etag === null ||
      headerValue(response.headers, "ETag") === targetExpectation.etag;
    const lastModifiedValid =
      targetExpectation.lastModified === null ||
      headerValue(response.headers, "Last-Modified") ===
        targetExpectation.lastModified;
    const cacheValidatorsValid = etagValid && lastModifiedValid;
    const valid =
      transportValid &&
      httpValid &&
      proxyHeadersValid &&
      encodingValid &&
      contentTypeValid &&
      contentLengthValid &&
      bodyLengthValid &&
      identityValid &&
      cacheValidatorsValid;

    benchmarkErrors.add(valid ? 0 : 1);
    transportErrors.add(transportValid ? 0 : 1);
    httpErrors.add(httpValid ? 0 : 1);
    proxyHeaderErrors.add(proxyHeadersValid ? 0 : 1);
    encodingErrors.add(encodingValid ? 0 : 1);
    contentTypeErrors.add(contentTypeValid ? 0 : 1);
    contentLengthErrors.add(contentLengthValid ? 0 : 1);
    bodyLengthErrors.add(bodyLengthValid ? 0 : 1);
    identityErrors.add(identityValid ? 0 : 1);
    cacheValidatorErrors.add(cacheValidatorsValid ? 0 : 1);
    check(response, {
      "transport completed": () => transportValid,
      "status is 200": () => httpValid,
      "response has no proxy or CDN headers": () => proxyHeadersValid,
      "stored content encoding is unchanged": () => encodingValid,
      "content type matches preflight": () => contentTypeValid,
      "Content-Length framing matches preflight": () => contentLengthValid,
      "decoded body length matches preflight": () => bodyLengthValid,
      "runtime identity matches target": () => identityValid,
      "cache validators match preflight": () => cacheValidatorsValid,
    });
    // This counter gives comparable stored-payload throughput. k6's built-in
    // data_received is retained separately as socket-byte diagnostics and
    // also includes headers and chunk framing.
    storedRepresentationBytes.add(expected.storedRepresentationLength);
    // Keep this last: equality with http_reqs then proves that the complete
    // JavaScript validation path ran for every observed response.
    observedResponses.add(1);
  }
}

function headerValue(headers, requestedName) {
  const requested = requestedName.toLowerCase();
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() === requested) {
      return String(value).trim();
    }
  }
  return "";
}

function hasRejectedProxyHeader(headers) {
  const rejectedNames = new Set([
    "age",
    "via",
    "x-cache",
    "x-cache-hits",
    "x-proxy-cache",
    "x-served-by",
    "x-varnish",
  ]);
  for (const [name, value] of Object.entries(headers)) {
    const normalizedName = name.toLowerCase();
    if (
      normalizedName.startsWith("cf-") ||
      rejectedNames.has(normalizedName)
    ) {
      return true;
    }
    if (
      normalizedName === "server" &&
      String(value).toLowerCase().includes("cloudflare")
    ) {
      return true;
    }
  }
  return false;
}

function normalizeContentEncoding(value) {
  if (value === undefined || value === null || String(value).trim() === "") {
    return "identity";
  }
  return String(value).trim().toLowerCase();
}

function normalizeContentType(value) {
  return String(value)
    .split(";")
    .map((part) => part.trim().toLowerCase())
    .join(";");
}

function parseContentLength(value) {
  if (!/^[0-9]+$/.test(value)) {
    return -1;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : -1;
}

function validNullableContentLength(value) {
  return (
    value === null ||
    (Number.isSafeInteger(value) && value > 0)
  );
}

function requiredEnvironment(name) {
  const value = __ENV[name];
  if (value === undefined || value.trim() === "") {
    throw new Error(`${name} is required`);
  }
  return value.trim();
}

function optionalEnvironment(name) {
  const value = __ENV[name];
  return value === undefined ? "" : value.trim();
}

function positiveInteger(value, name) {
  if (!/^[1-9][0-9]*$/.test(value)) {
    throw new Error(`${name} must be a positive integer`);
  }
  return Number.parseInt(value, 10);
}
