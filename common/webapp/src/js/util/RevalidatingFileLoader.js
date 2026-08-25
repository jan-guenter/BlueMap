// based on https://github.com/mrdoob/three.js/blob/a58e9ecf225b50e4a28a934442e854878bc2a959/src/loaders/FileLoader.js

import {Loader, Cache} from "three";
/** @import {LoadingManager} from "three" */

/**
 * @typedef {{
 *     onLoad: function,
 *     onProgress: function,
 *     onError: function,
 *     abortListeners: Array<{signal: AbortSignal, listener: function}>
 * }} LoadingCallback
 */

/**
 * @typedef {{
 *     revalidatedUrls: Set<string> | undefined,
 *     signature: string,
 *     callbacks: Array<LoadingCallback>,
 *     abortController: AbortController
 * }} LoadingEntry
 */

/** @type {Record<string, LoadingEntry>} */
const loading = Object.create(null);
const shownRequiredEncodings = new Set();

const REQUIRED_ENCODING_HEADER = "X-BlueMap-Required-Content-Encoding";
const OVERLOAD_HEADER = "X-BlueMap-Overload";
const BASE_RETRY_DELAY_MILLIS = 250;
const MAX_RETRY_DELAY_MILLIS = 5000;
const MAX_PROBLEM_BODY_LENGTH = 1024;
const MAX_RETRY_AFTER_SECONDS = MAX_RETRY_DELAY_MILLIS / 1000;

const warn = console.warn;

function problemContentType(response) {
    return response.headers.get("Content-Type")
        ?.split(";", 1)[0]
        .trim()
        .toLowerCase() === "application/problem+json";
}

async function problemDetails(response) {
    if (!problemContentType(response)) return null;

    const declaredLength = response.headers.get("Content-Length")?.trim();
    if (
        declaredLength &&
        (!/^\d+$/.test(declaredLength) ||
            Number(declaredLength) > MAX_PROBLEM_BODY_LENGTH)
    ) {
        cancelBody(response.body);
        return null;
    }

    const reader = response.body?.getReader?.();
    if (!reader) return null;

    try {
        const chunks = [];
        let length = 0;

        while (length <= MAX_PROBLEM_BODY_LENGTH) {
            const {done, value} = await reader.read();
            if (done) break;

            const remaining = MAX_PROBLEM_BODY_LENGTH + 1 - length;
            const chunk = value.subarray(0, remaining);
            chunks.push(chunk);
            length += chunk.byteLength;

            if (value.byteLength > remaining || length > MAX_PROBLEM_BODY_LENGTH) {
                cancelReader(reader);
                return null;
            }
        }

        const bytes = new Uint8Array(length);
        let offset = 0;
        for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.byteLength;
        }

        const body = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
        const details = JSON.parse(body);
        if (!details || Array.isArray(details) || typeof details !== "object") {
            return null;
        }
        return details;
    } catch {
        cancelReader(reader);
        return null;
    }
}

function cancelBody(body) {
    try {
        body?.cancel()?.catch?.(() => {});
    } catch {
        // The body may already be locked or cancelled. It is no longer read here.
    }
}

function cancelReader(reader) {
    try {
        reader.cancel()?.catch?.(() => {});
    } catch {
        // The reader may already be closed. It is no longer read here.
    }
}

function abortError() {
    if (typeof DOMException === "function") {
        return new DOMException("The operation was aborted.", "AbortError");
    }

    const error = new Error("The operation was aborted.");
    error.name = "AbortError";
    return error;
}

function removeAbortListeners(callback) {
    for (const {signal, listener} of callback.abortListeners) {
        signal.removeEventListener("abort", listener);
    }
    callback.abortListeners.length = 0;
}

function invokeCallback(callback, value) {
    if (!callback) return;
    try {
        callback(value);
    } catch (error) {
        setTimeout(() => {
            throw error;
        }, 0);
    }
}

function abortLoadingCallback(url, loadingEntry, callback) {
    const index = loadingEntry.callbacks.indexOf(callback);
    if (index < 0) return;

    loadingEntry.callbacks.splice(index, 1);
    removeAbortListeners(callback);

    if (loadingEntry.callbacks.length === 0) {
        if (loading[url] === loadingEntry) delete loading[url];
        loadingEntry.abortController.abort();
    }

    invokeCallback(callback.onError, abortError());
}

function addLoadingCallback(url, loadingEntry, loader, onLoad, onProgress, onError) {
    const callback = {onLoad, onProgress, onError, abortListeners: []};
    loadingEntry.callbacks.push(callback);

    const signals = [loader._abortController.signal];
    const managerSignal = loader.manager.abortController?.signal;
    if (managerSignal && managerSignal !== signals[0]) signals.push(managerSignal);

    const listener = () => abortLoadingCallback(url, loadingEntry, callback);
    for (const signal of signals) {
        callback.abortListeners.push({signal, listener});
        signal.addEventListener("abort", listener, {once: true});
        if (signal.aborted) {
            listener();
            return false;
        }
    }

    return true;
}

function takeLoadingCallbacks(loadingEntry) {
    const callbacks = loadingEntry.callbacks.splice(0);
    for (const callback of callbacks) removeAbortListeners(callback);
    return callbacks;
}

function representationSignature(loader) {
    return JSON.stringify({
        clientDecompression: loader.clientDecompression,
        credentials: loader.withCredentials ? "include" : "same-origin",
        headers: [...new Headers(loader.requestHeader).entries()],
        mimeType: loader.mimeType,
        responseType: loader.responseType
    });
}

function loadingSignature(representation, forceNoCacheRequest) {
    return JSON.stringify({
        cache: forceNoCacheRequest ? "no-cache" : "default",
        representation
    });
}

function retryAfterMillis(response) {
    const value = response.headers.get("Retry-After")?.trim();
    if (!value || !/^\d+$/.test(value)) return null;
    const seconds = Number(value);
    if (!Number.isSafeInteger(seconds) || seconds > MAX_RETRY_AFTER_SECONDS) {
        return null;
    }
    return seconds * 1000;
}

export function retryDelayMillis(error, retry, random = Math.random) {
    const baseDelay = error?.code === "bluemap_overload"
        ? error.retryAfterMillis
        : BASE_RETRY_DELAY_MILLIS * (2 ** retry);
    const boundedDelay = Math.min(
        Math.max(baseDelay ?? BASE_RETRY_DELAY_MILLIS, 0),
        MAX_RETRY_DELAY_MILLIS
    );
    const jitter = Math.floor(random() * Math.max(1, boundedDelay / 4));
    return Math.min(boundedDelay + jitter, MAX_RETRY_DELAY_MILLIS);
}

function networkError(error) {
    if (error?.name === "AbortError") return error;
    const wrapped = new Error(error?.message || "Network request failed");
    wrapped.name = "NetworkError";
    wrapped.code = "bluemap_network_error";
    wrapped.cause = error;
    return wrapped;
}

class HttpError extends Error {
    constructor(message, response, code, details = {}) {
        super(message);
        this.name = "HttpError";
        this.response = response;
        this.status = response.status;
        this.code = code;
        Object.assign(this, details);
    }
}

async function httpError(response) {
    const genericMessage =
        `fetch for "${response.url}" responded with ${response.status}: ${response.statusText}`;
    const details = await problemDetails(response);

    if (response.status === 406 && details) {
        const requiredEncoding = response.headers
            .get(REQUIRED_ENCODING_HEADER)?.trim();
        if (
            requiredEncoding &&
            details.code === "bluemap_required_content_encoding" &&
            details.requiredEncoding === requiredEncoding
        ) {
            return new HttpError(
                `This BlueMap server stores map data with '${requiredEncoding}' encoding, ` +
                    "but this browser did not advertise support for it. " +
                    "Ask the server administrator to choose a browser-supported map-data encoding.",
                response,
                "bluemap_required_content_encoding",
                {requiredEncoding}
            );
        }
    }

    if (
        response.status === 503 &&
        response.headers.get(OVERLOAD_HEADER)?.trim() === "capacity" &&
        details?.code === "bluemap_overloaded" &&
        details.status === 503
    ) {
        const delay = retryAfterMillis(response);
        if (delay !== null) {
            return new HttpError(
                genericMessage,
                response,
                "bluemap_overload",
                {retryAfterMillis: delay}
            );
        }
    }

    return new HttpError(genericMessage, response);
}

export function isRequiredEncodingError(error) {
    return error?.code === "bluemap_required_content_encoding";
}

export function isRetryableLoadError(error) {
    return error?.code === "bluemap_overload" ||
        error?.code === "bluemap_network_error";
}

export function rethrowRequiredEncodingError(error) {
    if (isRequiredEncodingError(error)) throw error;
}

export function showRequiredEncodingError(error) {
    if (!isRequiredEncodingError(error)) return false;

    const key = error.requiredEncoding || error.message;
    if (shownRequiredEncodings.has(key)) return false;
    shownRequiredEncodings.add(key);
    return true;
}

/**
 * A FileLoader that, if passed a Set of URLs, will be put into a mode where it
 * revalidates files by setting the Request cache option to "no-cache" for URLs
 * that have not previously been revalidated.
 *
 * This loader supports caching. If you want to use it, add `THREE.Cache.enabled = true;`
 * once to your application.
 *
 * ```js
 * const loader = new THREE.FileLoader();
 * const data = await loader.loadAsync( 'example.txt' );
 * ```
 *
 * @augments Loader
 */
export class RevalidatingFileLoader extends Loader {
    /**
     * Constructs a new file loader.
     *
     * @param {LoadingManager} [manager] - The loading manager.
     */
    constructor(manager) {
        super(manager);

        /**
         * The expected mime type. Valid values can be found
         * [here](hhttps://developer.mozilla.org/en-US/docs/Web/API/DOMParser/parseFromString#mimetype)
         *
         * @type {string}
         */
        this.mimeType = "";

        /**
         * The expected response type.
         *
         * @type {('arraybuffer'|'blob'|'document'|'json'|'')}
         * @default ''
         */
        this.responseType = "";

        /**
         * Whether client-side decompression is required.
         * 
         * @type {boolean}
         * @default false;
         */
        this.clientDecompression = false;

        /**
         * Used for aborting requests.
         *
         * @private
         * @type {AbortController}
         */
        this._abortController = new AbortController();

        /**
         * If set to a Set, this loader will revalidate URLs by setting the
         * Request cache option to "no-cache" for URLs not in the Set, adding
         * them to the Set once loaded.
         *
         * @type {Set<string> | undefined}
         */
        this._revalidatedUrls = undefined;
    }

    /**
     * @param {Set<string> | undefined} revalidatedUrls - If set to a Set, this
     *   loader will revalidate URLs by setting the Request cache option to
     *   "no-cache" for URLs not in the Set, adding them to the Set once loaded.
     */
    setRevalidatedUrls(revalidatedUrls) {
        this._revalidatedUrls = revalidatedUrls;
        return this;
    }

    /**
     * Starts loading from the given URL and pass the loaded response to the `onLoad()` callback.
     *
     * @param {string} url - The path/URL of the file to be loaded. This can also be a data URI.
     * @param {function(any)} onLoad - Executed when the loading process has been finished.
     * @param {onProgressCallback} [onProgress] - Executed while the loading is in progress.
     * @param {onErrorCallback} [onError] - Executed when errors occur.
     * @return {any|undefined} The cached resource if available.
     */
    load(url, onLoad, onProgress, onError) {
        if (url === undefined) url = "";

        if (this.path !== undefined) url = this.path + url;

        url = this.manager.resolveURL(url);

        // copy reference at start of method in case it is changed while loading
        const revalidatedUrls = this._revalidatedUrls;
        const forceNoCacheRequest = revalidatedUrls
            ? !revalidatedUrls.has(url)
            : false;
        const representation = representationSignature(this);
        const signature = loadingSignature(representation, forceNoCacheRequest);
        const cacheKey = `file:${url}:${representation}`;

        if (!forceNoCacheRequest) {
            const cached = Cache.get(cacheKey);

            if (cached !== undefined) {
                this.manager.itemStart(url);

                setTimeout(() => {
                    if (onLoad) onLoad(cached);
                    this.manager.itemEnd(url);
                }, 0);

                return cached;
            }
        }

        // Check if request is duplicate

        let loadingEntry = loading[url];

        if (
            loadingEntry !== undefined &&
            loadingEntry.revalidatedUrls === revalidatedUrls &&
            loadingEntry.signature === signature
        ) {
            addLoadingCallback(
                url,
                loadingEntry,
                this,
                onLoad,
                onProgress,
                onError
            );
            return;
        }

        // Create new loading entry (replacing if duplicate with different revalidatedUrls)
        loadingEntry = loading[url] = {
            revalidatedUrls,
            signature,
            callbacks: [],
            abortController: new AbortController(),
        };
        if (!addLoadingCallback(
            url,
            loadingEntry,
            this,
            onLoad,
            onProgress,
            onError
        )) return;

        // create request
        const req = new Request(url, {
            headers: new Headers(this.requestHeader),
            cache: forceNoCacheRequest ? "no-cache" : undefined,
            credentials: this.withCredentials ? "include" : "same-origin",
            signal: loadingEntry.abortController.signal,
        });

        // record states ( avoid data race )
        const mimeType = this.mimeType;
        const responseType = this.responseType;

        // start the fetch
        fetch(req)
            .catch(error => {
                throw networkError(error);
            })
            .then(async (response) => {
                if (response.status === 200 || response.status === 0) {
                    // Some browsers return HTTP Status 0 when using non-http protocol
                    // e.g. 'file://' or 'data://'. Handle as success.

                    if (response.status === 0) {
                        warn("FileLoader: HTTP Status 0 received.");
                    }

                    // Workaround: Checking if response.body === undefined for Alipay browser #23548

                    if (
                        typeof ReadableStream === "undefined" ||
                        response.body === undefined ||
                        response.body.getReader === undefined
                    ) {
                        return response;
                    }

                    const reader = response.body.getReader();

                    // Nginx needs X-File-Size check
                    // https://serverfault.com/questions/482875/why-does-nginx-remove-content-length-header-for-chunked-content
                    const contentLength =
                        response.headers.get("X-File-Size") ||
                        response.headers.get("Content-Length");
                    const total = contentLength ? parseInt(contentLength) : 0;
                    const lengthComputable = total !== 0;
                    let loaded = 0;

                    // periodically read data into the new stream tracking while download progress
                    const stream = new ReadableStream({
                        start(controller) {
                            readData();

                            function readData() {
                                reader.read().then(
                                    ({done, value}) => {
                                        if (done) {
                                            controller.close();
                                        } else {
                                            loaded += value.byteLength;

                                            const event = new ProgressEvent(
                                                "progress",
                                                {
                                                    lengthComputable,
                                                    loaded,
                                                    total,
                                                }
                                            );
                                            for (const callback of [
                                                ...loadingEntry.callbacks
                                            ]) {
                                                invokeCallback(
                                                    callback.onProgress,
                                                    event
                                                );
                                            }

                                            controller.enqueue(value);
                                            readData();
                                        }
                                    },
                                    (e) => {
                                        controller.error(networkError(e));
                                    }
                                );
                            }
                        },
                    });

                    return new Response(stream);
                }

                throw await httpError(response);
            })
            .then(async (response) => {
                if (this.clientDecompression) {
                    const ds = new globalThis.DecompressionStream("gzip");
                    const decompressedStream = (await response.blob()).stream().pipeThrough(ds);
                    const decompressedResponse = new Response(decompressedStream);
                    return decompressedResponse;
                }
                return response;
            })
            .then((response) => {
                switch (responseType) {
                    case "arraybuffer":
                        return response.arrayBuffer();

                    case "blob":
                        return response.blob();

                    case "document":
                        return response.text().then((text) => {
                            const parser = new DOMParser();
                            return parser.parseFromString(text, mimeType);
                        });

                    case "json":
                        return response.json();

                    default:
                        if (mimeType === "") {
                            return response.text();
                        } else {
                            // sniff encoding
                            const re = /charset="?([^;"\s]*)"?/i;
                            const exec = re.exec(mimeType);
                            const label =
                                exec && exec[1]
                                    ? exec[1].toLowerCase()
                                    : undefined;
                            const decoder = new TextDecoder(label);
                            return response
                                .arrayBuffer()
                                .then((ab) => decoder.decode(ab));
                        }
                }
            })
            .then((data) => {
                const callbacks = takeLoadingCallbacks(loadingEntry);
                if (
                    loadingEntry.abortController.signal.aborted ||
                    callbacks.length === 0
                ) return;

                // Add to cache only on HTTP success, so that we do not cache
                // error response bodies as proper responses to requests.
                Cache.add(cacheKey, data);

                if (loading[url] === loadingEntry) {
                    delete loading[url];
                }

                for (const callback of callbacks) {
                    invokeCallback(callback.onLoad, data);
                }
            })
            .catch((err) => {
                // Abort errors and other errors are handled the same

                if (loading[url] === loadingEntry) {
                    delete loading[url];
                }

                for (const callback of takeLoadingCallbacks(loadingEntry)) {
                    invokeCallback(callback.onError, err);
                }
                this.manager.itemError(url);
            })
            .finally(() => {
                this.manager.itemEnd(url);
            });
        this.manager.itemStart(url);
    }

    /**
     * Sets the expected response type.
     *
     * @param {('arraybuffer'|'blob'|'document'|'json'|'')} value - The response type.
     * @return {FileLoader} A reference to this file loader.
     */
    setResponseType(value) {
        this.responseType = value;
        return this;
    }

    /**
     * Sets the expected mime type of the loaded file.
     *
     * @param {string} value - The mime type.
     * @return {FileLoader} A reference to this file loader.
     */
    setMimeType(value) {
        this.mimeType = value;
        return this;
    }

    /**
     * Sets whether client-side decompression is required.
     * @param {boolean} value - True if the client must decompress the loaded file
     * @returns {FileLoader} A reference to this file loader.
     */
    setClientDecompression(value) {
        this.clientDecompression = value;
        return this;
    }

    /**
     * Aborts ongoing fetch requests.
     *
     * @return {FileLoader} A reference to this instance.
     */
    abort() {
        this._abortController.abort();
        this._abortController = new AbortController();

        return this;
    }
}
