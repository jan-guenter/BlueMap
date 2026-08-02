import assert from "node:assert/strict";
import test from "node:test";

import {
    rethrowRequiredEncodingError,
    showRequiredEncodingError
} from "../src/js/util/RevalidatingFileLoader.js";

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
