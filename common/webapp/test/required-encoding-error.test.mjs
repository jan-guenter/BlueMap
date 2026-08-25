import assert from "node:assert/strict";
import test from "node:test";
import {Cache} from "three";

import {
    RevalidatingFileLoader,
    retryDelayMillis,
    rethrowRequiredEncodingError,
    showRequiredEncodingError
} from "../src/js/util/RevalidatingFileLoader.js";

globalThis.ProgressEvent ??= class ProgressEvent {
    constructor(type, properties) {
        this.type = type;
        Object.assign(this, properties);
    }
};

function load(url) {
    return loadWith(new RevalidatingFileLoader(), url);
}

function loadWith(loader, url) {
    return new Promise((resolve, reject) => {
        loader.load(url, resolve, undefined, reject);
    });
}

async function withFetch(fetchImplementation, testFunction) {
    const previousFetch = globalThis.fetch;
    globalThis.fetch = fetchImplementation;
    try {
        await testFunction();
    } finally {
        globalThis.fetch = previousFetch;
    }
}

test("required-encoding startup errors propagate without being marked as shown", () => {
    const error = Object.assign(new Error("unsupported test encoding"), {
        code: "bluemap_required_content_encoding",
        requiredEncoding: "test-only-encoding"
    });

    assert.throws(
        () => rethrowRequiredEncodingError(error),
        thrown => thrown === error
    );
    assert.equal(showRequiredEncodingError(error), true);
    assert.equal(showRequiredEncodingError(error), false);
});

test("unrelated startup errors remain recoverable", () => {
    const error = new Error("recoverable");

    assert.doesNotThrow(() => rethrowRequiredEncodingError(error));
    assert.equal(showRequiredEncodingError(error), false);
});

test("the loader classifies only the BlueMap encoding response", async () => {
    await withFetch(
        async () => new Response("not acceptable", {status: 406}),
        async () => {
            await assert.rejects(
                load("https://example.test/unrelated-406"),
                error => error.status === 406 && error.code === undefined
            );
        }
    );

    await withFetch(
        async () => new Response(JSON.stringify({
            code: "bluemap_required_content_encoding",
            requiredEncoding: "zstd"
        }), {
            status: 406,
            headers: {
                "Content-Type": "application/problem+json",
                "X-BlueMap-Required-Content-Encoding": "zstd"
            }
        }),
        async () => {
            await assert.rejects(
                load("https://example.test/bluemap-406"),
                error => error.code === "bluemap_required_content_encoding" &&
                    error.requiredEncoding === "zstd"
            );
        }
    );
});

test("a header without the exact encoding problem remains an ordinary 406", async () => {
    await withFetch(
        async () => new Response("not acceptable", {
            status: 406,
            headers: {"X-BlueMap-Required-Content-Encoding": "zstd"}
        }),
        async () => {
            await assert.rejects(
                load("https://example.test/header-only-406"),
                error => error.status === 406 && error.code === undefined
            );
        }
    );
});

test("the loader identifies but does not internally retry exact overloads", async () => {
    let requests = 0;
    await withFetch(
        async () => {
            requests++;
            return new Response(JSON.stringify({
                type: "about:blank",
                title: "Service Unavailable",
                status: 503,
                code: "bluemap_overloaded"
            }), {
                status: 503,
                headers: {
                    "Content-Type": "application/problem+json",
                    "Retry-After": "1",
                    "X-BlueMap-Overload": "capacity"
                }
            });
        },
        async () => {
            await assert.rejects(
                load("https://example.test/exact-overload"),
                error => error.code === "bluemap_overload" &&
                    error.retryAfterMillis === 1000
            );
        }
    );
    assert.equal(requests, 1);
});

test("malformed overload responses are never treated as retryable", async () => {
    const validBody = {
        status: 503,
        code: "bluemap_overloaded"
    };
    const cases = [
        {
            name: "missing marker",
            headers: {"Content-Type": "application/problem+json", "Retry-After": "1"},
            body: validBody
        },
        {
            name: "wrong media type",
            headers: {"Content-Type": "text/plain", "Retry-After": "1", "X-BlueMap-Overload": "capacity"},
            body: validBody
        },
        {
            name: "missing retry delay",
            headers: {"Content-Type": "application/problem+json", "X-BlueMap-Overload": "capacity"},
            body: validBody
        },
        {
            name: "unbounded retry delay",
            headers: {"Content-Type": "application/problem+json", "Retry-After": "60", "X-BlueMap-Overload": "capacity"},
            body: validBody
        },
        {
            name: "wrong problem code",
            headers: {"Content-Type": "application/problem+json", "Retry-After": "1", "X-BlueMap-Overload": "capacity"},
            body: {status: 503, code: "different_problem"}
        },
        {
            name: "malformed problem json",
            headers: {"Content-Type": "application/problem+json", "Retry-After": "1", "X-BlueMap-Overload": "capacity"},
            body: "{"
        }
    ];

    for (const testCase of cases) {
        await withFetch(
            async () => new Response(
                typeof testCase.body === "string"
                    ? testCase.body
                    : JSON.stringify(testCase.body),
                {status: 503, headers: testCase.headers}
            ),
            async () => {
                await assert.rejects(
                    load(`https://example.test/${encodeURIComponent(testCase.name)}`),
                    error => error.status === 503 && error.code === undefined,
                    testCase.name
                );
            }
        );
    }
});

test("the loader exposes a transient network failure without retrying it", async () => {
    let requests = 0;
    await withFetch(
        async () => {
            requests++;
            throw new TypeError("connection reset");
        },
        async () => {
            await assert.rejects(
                load("https://example.test/transient-network-error"),
                error => error.code === "bluemap_network_error"
            );
        }
    );
    assert.equal(requests, 1);
});

test("retry jitter never exceeds the five-second cap", () => {
    const error = {code: "bluemap_network_error"};
    assert.equal(retryDelayMillis(error, 20, () => 0.999), 5000);
});

test("aborting one deduplicated loader leaves the other request alive", async () => {
    let resolveFetch;
    let networkSignal;

    await withFetch(
        request => new Promise((resolve, reject) => {
            resolveFetch = resolve;
            networkSignal = request.signal;
            request.signal.addEventListener(
                "abort",
                () => reject(new DOMException("aborted", "AbortError")),
                {once: true}
            );
        }),
        async () => {
            const firstLoader = new RevalidatingFileLoader();
            const secondLoader = new RevalidatingFileLoader();
            const first = loadWith(
                firstLoader,
                "https://example.test/shared-cancellation"
            );
            const second = loadWith(
                secondLoader,
                "https://example.test/shared-cancellation"
            );

            firstLoader.abort();
            await assert.rejects(first, error => error.name === "AbortError");
            assert.equal(networkSignal.aborted, false);

            resolveFetch(new Response("shared response"));
            assert.equal(await second, "shared response");
        }
    );
});

test("a deduplicated request is aborted after its final subscriber leaves", async () => {
    let networkSignal;

    await withFetch(
        request => new Promise((resolve, reject) => {
            networkSignal = request.signal;
            request.signal.addEventListener(
                "abort",
                () => reject(new DOMException("aborted", "AbortError")),
                {once: true}
            );
        }),
        async () => {
            const firstLoader = new RevalidatingFileLoader();
            const secondLoader = new RevalidatingFileLoader();
            const first = loadWith(
                firstLoader,
                "https://example.test/all-subscribers-cancelled"
            );
            const second = loadWith(
                secondLoader,
                "https://example.test/all-subscribers-cancelled"
            );

            firstLoader.abort();
            assert.equal(networkSignal.aborted, false);
            secondLoader.abort();
            assert.equal(networkSignal.aborted, true);

            await assert.rejects(first, error => error.name === "AbortError");
            await assert.rejects(second, error => error.name === "AbortError");
        }
    );
});

test("loaders with incompatible response options are not deduplicated", async () => {
    const requests = [];

    await withFetch(
        () => new Promise(resolve => requests.push(resolve)),
        async () => {
            const binaryLoader = new RevalidatingFileLoader();
            binaryLoader.setResponseType("arraybuffer");
            const textLoader = new RevalidatingFileLoader();

            const binary = loadWith(
                binaryLoader,
                "https://example.test/incompatible-options"
            );
            const text = loadWith(
                textLoader,
                "https://example.test/incompatible-options"
            );

            assert.equal(requests.length, 2);
            requests[0](new Response("binary"));
            requests[1](new Response("text"));

            assert.ok(await binary instanceof ArrayBuffer);
            assert.equal(await text, "text");
        }
    );
});

test("the cache separates incompatible response representations", async () => {
    const cacheWasEnabled = Cache.enabled;
    Cache.enabled = true;
    Cache.clear();

    try {
        let requests = 0;
        await withFetch(
            async () => {
                requests++;
                return new Response("payload");
            },
            async () => {
                const binaryLoader = new RevalidatingFileLoader();
                binaryLoader.setResponseType("arraybuffer");
                assert.ok(await loadWith(
                    binaryLoader,
                    "https://example.test/incompatible-cache-options"
                ) instanceof ArrayBuffer);

                const textLoader = new RevalidatingFileLoader();
                assert.equal(
                    await loadWith(
                        textLoader,
                        "https://example.test/incompatible-cache-options"
                    ),
                    "payload"
                );
                assert.equal(requests, 2);
            }
        );
    } finally {
        Cache.clear();
        Cache.enabled = cacheWasEnabled;
    }
});

test("a late response after final cancellation is not cached", async () => {
    const cacheWasEnabled = Cache.enabled;
    Cache.enabled = true;
    Cache.clear();

    try {
        let requests = 0;
        let resolveCancelledFetch;
        await withFetch(
            () => {
                requests++;
                if (requests === 1) {
                    return new Promise(resolve => {
                        resolveCancelledFetch = resolve;
                    });
                }
                return Promise.resolve(new Response("fresh"));
            },
            async () => {
                const loader = new RevalidatingFileLoader();
                const cancelled = loadWith(
                    loader,
                    "https://example.test/late-cancelled-response"
                );
                loader.abort();
                await assert.rejects(
                    cancelled,
                    error => error.name === "AbortError"
                );

                resolveCancelledFetch(new Response("stale"));
                await new Promise(resolve => setTimeout(resolve, 0));

                assert.equal(
                    await load("https://example.test/late-cancelled-response"),
                    "fresh"
                );
                assert.equal(requests, 2);
            }
        );
    } finally {
        Cache.clear();
        Cache.enabled = cacheWasEnabled;
    }
});

test("oversized chunked problem details are cancelled and remain generic", async () => {
    let cancelled = false;
    const body = new ReadableStream({
        start(controller) {
            controller.enqueue(new Uint8Array(700));
            controller.enqueue(new Uint8Array(700));
        },
        cancel() {
            cancelled = true;
        }
    });

    await withFetch(
        async () => new Response(body, {
            status: 503,
            headers: {
                "Content-Type": "application/problem+json",
                "Retry-After": "1",
                "X-BlueMap-Overload": "capacity"
            }
        }),
        async () => {
            await assert.rejects(
                load("https://example.test/oversized-chunked-problem"),
                error => error.status === 503 && error.code === undefined
            );
        }
    );
    assert.equal(cancelled, true);
});

test("an endless oversized problem stream is cancelled after 1025 bytes", async () => {
    let pulls = 0;
    let cancelled = false;
    const body = new ReadableStream({
        pull(controller) {
            pulls++;
            controller.enqueue(new Uint8Array(pulls === 1 ? 1024 : 1));
        },
        cancel() {
            cancelled = true;
        }
    });

    await withFetch(
        async () => new Response(body, {
            status: 406,
            headers: {
                "Content-Type": "application/problem+json",
                "X-BlueMap-Required-Content-Encoding": "zstd"
            }
        }),
        async () => {
            await assert.rejects(
                load("https://example.test/endless-problem"),
                error => error.status === 406 && error.code === undefined
            );
        }
    );
    assert.ok(pulls >= 2);
    assert.equal(cancelled, true);
});
