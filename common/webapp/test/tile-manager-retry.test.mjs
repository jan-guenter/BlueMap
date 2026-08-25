import assert from "node:assert/strict";
import test from "node:test";

import {Group} from "three";
import {Tile} from "../src/js/map/Tile.js";

globalThis.document ??= {
    createElement: () => ({
        getContext: () => ({
            fillStyle: "#000",
            fillRect: () => {},
            drawImage: () => {},
            getImageData: () => ({data: new Uint8ClampedArray(4)})
        })
    }),
    createElementNS: () => ({
        getContext: () => ({fillStyle: "#000", fillRect: () => {}})
    })
};

const {TileManager} = await import("../src/js/map/TileManager.js");

function overloadError(retryAfterMillis = 0) {
    return Object.assign(new Error("temporarily overloaded"), {
        code: "bluemap_overload",
        retryAfterMillis
    });
}

function model() {
    const result = new Group();
    result.userData.tileType = "test";
    return result;
}

async function waitFor(predicate, timeoutMillis = 1000) {
    const deadline = Date.now() + timeoutMillis;
    while (!predicate()) {
        if (Date.now() >= deadline) {
            assert.fail("timed out waiting for retry state");
        }
        await new Promise(resolve => setTimeout(resolve, 5));
    }
}

function manager(loader, viewDistance = 0) {
    const result = new TileManager(loader);
    result.unloaded = false;
    result.viewDistanceX = viewDistance;
    result.viewDistanceZ = viewDistance;
    return result;
}

test("an initial tile receives one bounded retry series", async () => {
    let requests = 0;
    const tileManager = manager({
        load: () => {
            requests++;
            return Promise.reject(overloadError());
        }
    });

    assert.equal(tileManager.tryLoadTile(0, 0), true);
    await waitFor(() => tileManager.tileLoadRetries.get("x0z0")?.exhausted);

    assert.equal(requests, 4);
    assert.equal(tileManager.tiles.has("x0z0"), false);
    assert.equal(tileManager.tryLoadTile(0, 0), false);
    tileManager.unload();
});

test("per-tile retry timers do not cancel each other", async () => {
    const requests = new Map();
    const tileManager = manager({
        load: (x, z) => {
            const key = `${x},${z}`;
            const count = (requests.get(key) || 0) + 1;
            requests.set(key, count);
            return count === 1
                ? Promise.reject(overloadError())
                : Promise.resolve(model());
        }
    }, 1);

    assert.equal(tileManager.tryLoadTile(0, 0), true);
    assert.equal(tileManager.tryLoadTile(1, 0), true);
    await waitFor(() =>
        tileManager.tiles.get("x0z0")?.loaded &&
        tileManager.tiles.get("x1z0")?.loaded
    );

    assert.equal(requests.get("0,0"), 2);
    assert.equal(requests.get("1,0"), 2);
    tileManager.unload();
});

test("a failed SSE refresh keeps the model through bounded retries", async () => {
    let requests = 0;
    const tileManager = manager({
        load: () => {
            requests++;
            return Promise.reject(overloadError());
        }
    });
    const tile = new Tile(
        0,
        0,
        tileManager.handleLoadedTile,
        tileManager.handleUnloadedTile
    );
    const previousModel = model();
    tile.model = previousModel;
    tile.unloaded = false;
    tileManager.tiles.set("x0z0", tile);

    assert.equal(tileManager.refreshTile(tile), true);
    await waitFor(() =>
        tileManager.tileRefreshRetries.get("x0z0")?.exhausted
    );

    assert.equal(requests, 4);
    assert.equal(tile.model, previousModel);
    assert.equal(tile.loaded, true);
    tileManager.unload();
});

test("unloading the manager cancels pending retry timers", async () => {
    let requests = 0;
    const tileManager = manager({
        load: () => {
            requests++;
            return Promise.reject(overloadError(1000));
        }
    });

    tileManager.tryLoadTile(0, 0);
    await waitFor(() => tileManager.tileLoadRetries.get("x0z0")?.timer);
    tileManager.unload();
    await new Promise(resolve => setTimeout(resolve, 20));

    assert.equal(requests, 1);
    assert.equal(tileManager.tileLoadRetries.size, 0);
});

test("an SSE update revives an exhausted missing tile", async () => {
    let requests = 0;
    let recovered = false;
    const tileManager = manager({
        load: () => {
            requests++;
            return recovered
                ? Promise.resolve(model())
                : Promise.reject(overloadError());
        }
    });

    tileManager.tryLoadTile(0, 0);
    await waitFor(() => tileManager.tileLoadRetries.get("x0z0")?.exhausted);
    assert.equal(tileManager.tiles.has("x0z0"), false);

    recovered = true;
    assert.equal(tileManager.handleTileUpdate(0, 0), true);
    await waitFor(() => tileManager.tiles.get("x0z0")?.loaded);

    assert.equal(requests, 5);
    assert.equal(tileManager.tileLoadRetries.has("x0z0"), false);
    tileManager.unload();
});

test("SSE updates during a load coalesce into one later refresh", async () => {
    const requests = [];
    const tileManager = manager({
        load: (x, z, cancelCheck, force) => new Promise(resolve => {
            requests.push({force, resolve});
        })
    });

    assert.equal(tileManager.tryLoadTile(0, 0), true);
    assert.equal(tileManager.handleTileUpdate(0, 0), true);
    assert.equal(tileManager.handleTileUpdate(0, 0), true);
    assert.equal(tileManager.handleTileUpdate(0, 0), true);

    const initialModel = model();
    requests[0].resolve(initialModel);
    await waitFor(() => requests.length === 2);
    assert.deepEqual(requests.map(request => request.force), [false, true]);

    const refreshedModel = model();
    requests[1].resolve(refreshedModel);
    await waitFor(() => tileManager.tiles.get("x0z0")?.model === refreshedModel);

    assert.equal(requests.length, 2);
    tileManager.unload();
});

test("an SSE update does not bypass initial-load retry backoff", async () => {
    let requests = 0;
    const tileManager = manager({
        load: () => {
            requests++;
            return Promise.reject(overloadError(1000));
        }
    });

    tileManager.tryLoadTile(0, 0);
    await waitFor(() => tileManager.tileLoadRetries.get("x0z0")?.timer);
    const retryTimer = tileManager.tileLoadRetries.get("x0z0").timer;

    assert.equal(tileManager.handleTileUpdate(0, 0), true);
    assert.equal(tileManager.tileLoadRetries.get("x0z0").timer, retryTimer);
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(requests, 1);
    tileManager.unload();
});

test("an SSE update does not bypass refresh retry backoff", async () => {
    let requests = 0;
    const tileManager = manager({
        load: () => {
            requests++;
            return Promise.reject(overloadError(1000));
        }
    });
    const tile = new Tile(
        0,
        0,
        tileManager.handleLoadedTile,
        tileManager.handleUnloadedTile
    );
    tile.model = model();
    tile.unloaded = false;
    tileManager.tiles.set("x0z0", tile);

    tileManager.refreshTile(tile);
    await waitFor(() => tileManager.tileRefreshRetries.get("x0z0")?.timer);
    const retryTimer = tileManager.tileRefreshRetries.get("x0z0").timer;

    assert.equal(tileManager.handleTileUpdate(0, 0), true);
    assert.equal(tileManager.tileRefreshRetries.get("x0z0").timer, retryTimer);
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(requests, 1);
    tileManager.unload();
});

test("SSE refreshes share the tile manager concurrency limit", async () => {
    let active = 0;
    let maximumActive = 0;
    const pending = [];
    const tileManager = manager({
        load: () => {
            active++;
            maximumActive = Math.max(maximumActive, active);
            let resolve;
            const result = new Promise(done => {
                resolve = done;
            });
            pending.push(() => resolve(model()));
            return result.finally(() => active--);
        }
    });

    for (let x = 0; x < 9; x++) {
        const tile = new Tile(
            x,
            0,
            tileManager.handleLoadedTile,
            tileManager.handleUnloadedTile
        );
        tile.model = model();
        tile.unloaded = false;
        tileManager.tiles.set(`x${x}z0`, tile);
        assert.equal(tileManager.refreshTile(tile), true);
    }

    assert.equal(pending.length, TileManager.maxConcurrentLoads);
    assert.equal(tileManager.queuedTileRefreshes.size, 1);
    pending[0]();
    await waitFor(() => pending.length === 9);

    for (let i = 1; i < pending.length; i++) pending[i]();
    await waitFor(() => tileManager.currentlyLoading === 0);

    assert.equal(maximumActive, TileManager.maxConcurrentLoads);
    assert.equal(tileManager.queuedTileRefreshes.size, 0);
    tileManager.unload();
});
