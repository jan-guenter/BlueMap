import assert from "node:assert/strict";
import test from "node:test";

import {Tile} from "../src/js/map/Tile.js";

test("a failed refresh keeps the last loaded tile", async () => {
    let unloaded = 0;
    const tile = new Tile(1, 2, () => {}, () => unloaded++);
    const previousModel = {userData: {tileType: "test"}};
    tile.model = previousModel;
    tile.unloaded = false;

    const error = Object.assign(new Error("temporary outage"), {
        code: "bluemap_overload"
    });
    await assert.rejects(
        tile.load({load: () => Promise.reject(error)}, true),
        thrown => thrown === error
    );

    assert.equal(tile.model, previousModel);
    assert.equal(tile.unloaded, false);
    assert.equal(tile.loaded, true);
    assert.equal(unloaded, 0);
});

test("a failed initial tile load remains unloaded", async () => {
    const tile = new Tile(1, 2, () => {}, () => {});
    const error = Object.assign(new Error("temporary outage"), {
        code: "bluemap_overload"
    });

    await assert.rejects(
        tile.load({load: () => Promise.reject(error)}),
        thrown => thrown === error
    );

    assert.equal(tile.model, null);
    assert.equal(tile.unloaded, true);
    assert.equal(tile.loaded, false);
});

test("unloading a tile aborts its in-flight request", async () => {
    const tile = new Tile(1, 2, () => {}, () => {});
    let observedSignal;
    const pending = tile.load({
        load: (x, z, cancelCheck, force, signal) => {
            observedSignal = signal;
            return new Promise((resolve, reject) => {
                signal.addEventListener(
                    "abort",
                    () => reject({status: "cancelled"}),
                    {once: true}
                );
            });
        }
    });

    tile.unload();

    await assert.rejects(pending, error => error.status === "cancelled");
    assert.equal(observedSignal.aborted, true);
    assert.equal(tile.unloaded, true);
});
