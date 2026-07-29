# BlueMap 5.22 backport

This fork backports BlueMap 5.22 to the All the Mons 1.1.1 server baseline:

- BlueMap upstream tag: `v5.22`
- Upstream commit: `fe5115d5548a30d34175b8e0449aaca280af199f`
- BlueMapAPI 2.8 upstream base:
  `e20166d5ac93feab653392cf30a305a3e255754e`
- Java 21 BlueMapAPI fork commit:
  `285c9a6` on `jan-guenter/BlueMapAPI`
- Minecraft: 1.21.1
- NeoForge: 21.1.234
- Java: 21

The fork retains the BlueMap 5.22 API, core, common code, renderer, resource
system, and web application. It provides a dedicated NeoForge 1.21.1 platform
adapter and intentionally does not claim compatibility with BlueMap's other
platform targets.

## Data and migration scope

Only fresh BlueMap installations are supported. Migration or reuse of BlueMap
5.7 configuration, render state, map storage, web data, and generated tiles is
out of scope.

Testing must always use a new BlueMap configuration and empty BlueMap
data/web/storage directories. Existing production BlueMap directories must not
be used as writable test targets.

## Work stages

1. Compile API, core, and common code as Java 21 bytecode.
2. Compile the NeoForge implementation against Minecraft 1.21.1 and NeoForge
   21.1.234.
3. Start and stop a minimal dedicated server on Java 21.
4. Validate commands, permissions, dimensions, resources, web server, player
   data, and incremental updates.
5. Validate the exact All the Mons 1.1.1 staging pack with a fresh BlueMap
   setup and an unmodified client.
6. Load a minimal external BlueMap 5.22 custom-renderer add-on to prove the
   extension ABI required by the future FramedBlocks add-on.

## Current status

The fork and backport branch are established. Java 21 and NeoForge 1.21.1
retargeting is in progress; no runtime-compatible release has been produced
yet.
