/*
 * This file is part of BlueMap, licensed under the MIT License (MIT).
 *
 * Copyright (c) Blue (Lukas Rieger) <https://bluecolored.de>
 * Copyright (c) contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */
import { Vector2, Scene, Group } from 'three';
import  { Tile } from './Tile.js';
import {alert, dispatchEvent, hashTile} from '../util/Utils.js';
import {TileMap} from "./TileMap.js";
import {
    isRequiredEncodingError,
    isRetryableLoadError,
    retryDelayMillis,
    showRequiredEncodingError
} from "../util/RevalidatingFileLoader.js";

export class TileManager {

    static tileMapSize = 100;
    static tileMapHalfSize = TileManager.tileMapSize / 2;
    static maxTransientRetries = 3;
    static maxConcurrentLoads = 8;

    /**
     * @param tileLoader {TileLoader | LowresTileLoader}
     * @param onTileLoad {function(Tile)}
     * @param onTileUnload {function(Tile)}
     * @param events {EventTarget}
     */
    constructor(tileLoader, onTileLoad = null, onTileUnload = null, events = null) {
        Object.defineProperty( this, 'isTileManager', { value: true } );

        this.sceneParent = new Scene();
        this.scene = new Group();
        this.sceneParent.add(this.scene);

        this.events = events;
        this.tileLoader = tileLoader;

        this.onTileLoad = onTileLoad || function(){};
        this.onTileUnload = onTileUnload || function(){};

        this.viewDistanceX = 1;
        this.viewDistanceZ = 1;
        this.centerTile = new Vector2(0, 0);

        this.currentlyLoading = 0;
        this.loadTimeout = null;

        // Retry state deliberately outlives failed Tile instances, so removing
        // and recreating a tile cannot reset its bounded retry budget.
        this.tileLoadRetries = new Map();
        this.tileRefreshRetries = new Map();
        this.pendingTileRefreshes = new Set();
        this.queuedTileRefreshes = new Map();

        //map of loaded tiles
        this.tiles = new Map();

        // a canvas that keeps track of the loaded tiles, used for shaders
        this.tileMap = new TileMap(TileManager.tileMapSize, TileManager.tileMapSize);

        this.unloaded = true;
    }

    /**
     * @param x {number}
     * @param z {number}
     * @param viewDistanceX {number}
     * @param viewDistanceZ {number}
     */
    loadAroundTile(x, z, viewDistanceX, viewDistanceZ) {
        this.unloaded = false;

        let unloadTiles = false;
        if (this.viewDistanceX > viewDistanceX || this.viewDistanceZ > viewDistanceZ) {
            unloadTiles = true;
        }

        this.viewDistanceX = viewDistanceX;
        this.viewDistanceZ = viewDistanceZ;

        if (viewDistanceX <= 0 || viewDistanceZ <= 0) {
            this.removeAllTiles();
            return;
        }

        if (unloadTiles || this.centerTile.x !== x || this.centerTile.y !== z) {
            this.centerTile.set(x, z);
            this.removeFarTiles();

            this.tileMap.setAll(TileMap.EMPTY);
            this.tiles.forEach(tile => {
                if (!tile.loading && !tile.unloaded) {
                    this.tileMap.setTile(tile.x - this.centerTile.x + TileManager.tileMapHalfSize, tile.z - this.centerTile.y + TileManager.tileMapHalfSize, TileMap.LOADED);
                }
            });
        }

        this.loadCloseTiles();
    }

    unload() {
        this.unloaded = true;
        this.removeAllTiles();
    }

    removeFarTiles() {
        this.tiles.forEach((tile, hash, map) => {
            if (
                tile.x + this.viewDistanceX < this.centerTile.x ||
                tile.x - this.viewDistanceX > this.centerTile.x ||
                tile.z + this.viewDistanceZ < this.centerTile.y ||
                tile.z - this.viewDistanceZ > this.centerTile.y
            ) {
                tile.unload();
                map.delete(hash);
                this.pendingTileRefreshes.delete(hash);
                this.queuedTileRefreshes.delete(hash);
            }
        });
        this.removeFarRetryStates(this.tileLoadRetries);
        this.removeFarRetryStates(this.tileRefreshRetries);
    }

    removeAllTiles() {
        if (this.loadTimeout) clearTimeout(this.loadTimeout);
        this.loadTimeout = null;
        this.tileMap.setAll(TileMap.EMPTY);

        this.tiles.forEach(tile => {
            tile.unload();
        });
        this.tiles.clear();
        this.clearRetryStates(this.tileLoadRetries);
        this.clearRetryStates(this.tileRefreshRetries);
        this.pendingTileRefreshes.clear();
        this.queuedTileRefreshes.clear();
    }

    loadCloseTiles = () => {
        this.loadTimeout = null;
        if (this.unloaded) return;
        this.drainTileRefreshQueue();
        if (this.currentlyLoading >= TileManager.maxConcurrentLoads) {
            this.scheduleLoadCloseTiles(1000);
            return;
        }
        if (!this.loadNextTile()) return;

        this.scheduleLoadCloseTiles(
            this.currentlyLoading < TileManager.maxConcurrentLoads ? 0 : 1000
        );
    }

    scheduleLoadCloseTiles(delay) {
        if (this.unloaded) return;
        if (this.loadTimeout) clearTimeout(this.loadTimeout);
        this.loadTimeout = setTimeout(this.loadCloseTiles, delay);
    }

    /**
     * @returns {boolean}
     */
    loadNextTile() {
        if (this.unloaded) return false;

        let x = 0;
        let z = 0;
        let d = 1;
        let m = 1;

        while (m < Math.max(this.viewDistanceX, this.viewDistanceZ) * 2 + 1) {
            while (2 * x * d < m) {
                if (this.tryLoadTile(this.centerTile.x + x, this.centerTile.y + z)) return true;
                x = x + d;
            }
            while (2 * z * d < m) {
                if (this.tryLoadTile(this.centerTile.x + x, this.centerTile.y + z)) return true;
                z = z + d;
            }
            d = -1 * d;
            m = m + 1;
        }

        return false;
    }

    /**
     * @param x {number}
     * @param z {number}
     * @returns {boolean}
     */
    tryLoadTile(x, z) {
        if (this.unloaded) return false;

        if (Math.abs(x - this.centerTile.x) > this.viewDistanceX) return false;
        if (Math.abs(z - this.centerTile.y) > this.viewDistanceZ) return false;
        if (this.currentlyLoading >= TileManager.maxConcurrentLoads) return false;

        let tileHash = hashTile(x, z);

        let tile = this.tiles.get(tileHash);
        if (tile !== undefined) return false;
        const retryState = this.tileLoadRetries.get(tileHash);
        if (retryState?.timer || retryState?.exhausted) return false;

        this.currentlyLoading++;

        tile = new Tile(x, z, this.handleLoadedTile, this.handleUnloadedTile);
        this.tiles.set(tileHash, tile);
        tile.load(this.tileLoader)
            .then(() => {
                this.clearRetryState(this.tileLoadRetries, tileHash);
                dispatchEvent(this.events, "bluemapTileLoaded", {
                    tileManager: this,
                    tile: tile
                });

            })
            .catch(error => {
                if (
                    isRequiredEncodingError(error) &&
                    showRequiredEncodingError(error)
                ) {
                    alert(this.events, error.message, "error");
                }

                if (
                    isRetryableLoadError(error) &&
                    this.tiles.get(tileHash) === tile &&
                    !tile.loaded
                ) {
                    this.tiles.delete(tileHash);
                    if (!this.scheduleTileLoadRetry(tileHash, x, z, error)) {
                        console.warn(
                            `Failed to load map tile ${tileHash} after bounded retries`,
                            error
                        );
                    }
                }
            })
            .finally(() => {
                this.currentlyLoading--;
                this.finishTileWork(tileHash, x, z);
            });

        return true;
    }

    /**
     * Handles an SSE tile update by coordinate. Missing tiles and exhausted
     * initial loads get a fresh bounded attempt series when they are in view.
     * Updates received during a load are coalesced into one later refresh.
     *
     * @param x {number}
     * @param z {number}
     * @returns {boolean}
     */
    handleTileUpdate(x, z) {
        if (this.unloaded || !this.isInView(x, z)) return false;

        const tileHash = hashTile(x, z);
        const tile = this.tiles.get(tileHash);
        if (tile?.loading) {
            this.pendingTileRefreshes.add(tileHash);
            return true;
        }

        if (!tile || tile.unloaded) {
            if (tile) this.tiles.delete(tileHash);
            const loadRetryState = this.tileLoadRetries.get(tileHash);
            if (loadRetryState?.timer) return true;
            if (loadRetryState?.exhausted) {
                this.clearRetryState(this.tileLoadRetries, tileHash);
            }
            this.clearRetryState(this.tileRefreshRetries, tileHash);
            this.queuedTileRefreshes.delete(tileHash);

            if (!this.tryLoadTile(x, z)) this.scheduleLoadCloseTiles(0);
            return true;
        }

        return this.refreshTile(tile);
    }

    /**
     * Reloads an already displayed tile after an SSE update. A failed refresh
     * retains the previous model and receives the same bounded retry treatment
     * as an initial tile load.
     *
     * @param tile {Tile}
     * @returns {boolean}
     */
    refreshTile(tile) {
        if (this.unloaded) return false;

        const tileHash = hashTile(tile.x, tile.z);
        if (this.tiles.get(tileHash) !== tile || tile.unloaded) return false;
        if (tile.loading) {
            this.pendingTileRefreshes.add(tileHash);
            return true;
        }

        // A new SSE event represents new content and starts a fresh bounded
        // attempt series. A single event can never retry indefinitely.
        const retryState = this.tileRefreshRetries.get(tileHash);
        if (retryState?.timer) return true;
        if (retryState?.exhausted) {
            this.clearRetryState(this.tileRefreshRetries, tileHash);
        }
        return this.enqueueTileRefresh(tile, tileHash);
    }

    enqueueTileRefresh(tile, tileHash) {
        if (
            this.unloaded ||
            this.tiles.get(tileHash) !== tile ||
            tile.unloaded
        ) return false;

        if (tile.loading) {
            this.pendingTileRefreshes.add(tileHash);
            return true;
        }
        if (this.queuedTileRefreshes.has(tileHash)) return true;

        if (this.currentlyLoading >= TileManager.maxConcurrentLoads) {
            this.queuedTileRefreshes.set(tileHash, tile);
            return true;
        }

        this.startTileRefresh(tile, tileHash);
        return true;
    }

    startTileRefresh(tile, tileHash) {
        this.currentlyLoading++;
        tile.load(this.tileLoader, true)
            .then(() => {
                this.clearRetryState(this.tileRefreshRetries, tileHash);
            })
            .catch(error => {
                if (
                    isRequiredEncodingError(error) &&
                    showRequiredEncodingError(error)
                ) {
                    alert(this.events, error.message, "error");
                }

                if (isRetryableLoadError(error)) {
                    if (!this.scheduleTileRefreshRetry(tile, tileHash, error)) {
                        console.warn(
                            `Failed to refresh map tile ${tileHash} after bounded retries`,
                            error
                        );
                    }
                } else if (
                    !isRequiredEncodingError(error) &&
                    error?.status !== "cancelled"
                ) {
                    console.warn(`Failed to refresh map tile ${tileHash}`, error);
                }
            })
            .finally(() => {
                this.currentlyLoading--;
                this.finishTileWork(tileHash, tile.x, tile.z);
            });
    }

    finishTileWork(tileHash, x, z) {
        if (this.pendingTileRefreshes.delete(tileHash)) {
            this.handleTileUpdate(x, z);
        }
        this.drainTileRefreshQueue();
        this.scheduleLoadCloseTiles(0);
    }

    drainTileRefreshQueue() {
        if (this.unloaded) return;

        for (const [tileHash, tile] of this.queuedTileRefreshes) {
            if (this.currentlyLoading >= TileManager.maxConcurrentLoads) return;
            this.queuedTileRefreshes.delete(tileHash);

            if (
                this.tiles.get(tileHash) !== tile ||
                tile.unloaded
            ) {
                this.clearRetryState(this.tileRefreshRetries, tileHash);
                continue;
            }
            if (tile.loading) {
                this.pendingTileRefreshes.add(tileHash);
                continue;
            }

            this.startTileRefresh(tile, tileHash);
        }
    }

    scheduleTileLoadRetry(tileHash, x, z, error) {
        return this.scheduleRetry(
            this.tileLoadRetries,
            tileHash,
            x,
            z,
            error,
            () => {
                if (this.unloaded || !this.isInView(x, z)) {
                    this.clearRetryState(this.tileLoadRetries, tileHash);
                    return;
                }
                this.tryLoadTile(x, z);
            }
        );
    }

    scheduleTileRefreshRetry(tile, tileHash, error) {
        return this.scheduleRetry(
            this.tileRefreshRetries,
            tileHash,
            tile.x,
            tile.z,
            error,
            () => {
                if (
                    this.unloaded ||
                    this.tiles.get(tileHash) !== tile ||
                    tile.unloaded
                ) {
                    this.clearRetryState(this.tileRefreshRetries, tileHash);
                    return;
                }
                this.enqueueTileRefresh(tile, tileHash);
            }
        );
    }

    scheduleRetry(states, tileHash, x, z, error, retry) {
        let state = states.get(tileHash);
        if (!state) {
            state = {attempts: 0, exhausted: false, timer: null, x, z};
            states.set(tileHash, state);
        }

        if (state.timer || state.exhausted) return false;
        if (state.attempts >= TileManager.maxTransientRetries) {
            state.exhausted = true;
            return false;
        }

        const delay = retryDelayMillis(error, state.attempts);
        state.attempts++;
        state.timer = setTimeout(() => {
            state.timer = null;
            retry();
        }, delay);
        return true;
    }

    clearRetryState(states, tileHash) {
        const state = states.get(tileHash);
        if (state?.timer) clearTimeout(state.timer);
        states.delete(tileHash);
    }

    clearRetryStates(states) {
        states.forEach(state => {
            if (state.timer) clearTimeout(state.timer);
        });
        states.clear();
    }

    removeFarRetryStates(states) {
        states.forEach((state, tileHash) => {
            if (!this.isInView(state.x, state.z)) {
                this.clearRetryState(states, tileHash);
            }
        });
    }

    isInView(x, z) {
        return Math.abs(x - this.centerTile.x) <= this.viewDistanceX &&
            Math.abs(z - this.centerTile.y) <= this.viewDistanceZ;
    }

    handleLoadedTile = tile => {
        this.tileMap.setTile(tile.x - this.centerTile.x + TileManager.tileMapHalfSize, tile.z - this.centerTile.y + TileManager.tileMapHalfSize, TileMap.LOADED);

        this.scene.add(tile.model);
        this.onTileLoad(tile);
    }

    handleUnloadedTile = tile => {
        this.tileMap.setTile(tile.x - this.centerTile.x + TileManager.tileMapHalfSize, tile.z - this.centerTile.y + TileManager.tileMapHalfSize, TileMap.EMPTY);

        this.scene.remove(tile.model);
        this.onTileUnload(tile);
    }
}
