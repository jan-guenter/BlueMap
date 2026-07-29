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

## Backport and follow-up stages

1. Compile API, core, and common code as Java 21 bytecode.
2. Compile the NeoForge implementation against Minecraft 1.21.1 and NeoForge
   21.1.234.
3. Start and stop a minimal dedicated server on Java 21.
4. Validate commands, permissions, dimensions, resources, web server, player
   data, and incremental updates.
5. Validate the exact All the Mons 1.1.1 staging pack with a fresh BlueMap
   setup and an unmodified client.
6. In the separate FramedBlocks add-on project, compile and load its first
   renderer skeleton against this build to exercise the external extension ABI.

## Validation status

The backport implementation is complete. The exact branch commit tested before
this status update was `fe79cf5b9f4d8ca28f4e41c2aeb9ef792e336a8d`.

Automated validation:

- GitHub Actions CI run
  [30415244484](https://github.com/jan-guenter/BlueMap/actions/runs/30415244484)
  passed both `spotlessCheck test` and `release` on Java 21.
- The CI NeoForge artifact has SHA-256
  `642a134bfcbbc970cfe1de39660a9f873e5c15069ee83f512ec579743e38efe7`.
- The artifact installed on staging was byte-for-byte identical to that CI
  artifact.
- The backport and pinned BlueMapAPI fork introduce no `TODO`, `FIXME`, `HACK`,
  or `XXX` markers relative to their documented upstream bases.

All the Mons 1.1.1 staging validation on 2026-07-29:

- Started the complete pack on Java 21, Minecraft 1.21.1, and NeoForge
  21.1.234 with a fresh BlueMap configuration and empty BlueMap data.
- Loaded resources and all 17 discovered dimensions.
- Started the integrated web server and served the web application, settings,
  map settings, and rendered PRBM tile data successfully.
- Rendered 137 initial overworld tiles; the tile served over HTTP matched the
  decompressed on-disk PRBM payload.
- Completed a clean server restart, reused the cached Minecraft client JAR and
  existing tiles without modification, and returned to a healthy pod with no
  container restart.
- Connected with an unmodified All the Mons 1.1.1 client.
- Published the connected player's UUID, name, position, and rotation through
  the live-player endpoint; the player marker was visible and moving in the web
  application.

The only BlueMap warning was the expected fallback for the AE2 spatial-storage
dimension, whose world data does not contain normal dimension metadata.
An unrelated Ars Nouveau watchdog crash was observed before BlueMap was
installed as well as during an earlier staging start; the final clean restart
completed without it.

The public API remains BlueMapAPI 2.8 from the exact BlueMap 5.22 dependency,
retargeted only from Java 22 to Java 21. Building and loading the future
FramedBlocks renderer add-on is intentionally deferred to that add-on's
separate development session.
