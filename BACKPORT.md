# BlueMap 5.23 backport

This fork backports BlueMap 5.23 to the All the Mons 1.2.0 server baseline:

- All the Mons: `1.2.0`
- Pack repository commit: `c7bb230f21d14d26859d0b92548f089b3a493ad9`
- BlueMap upstream tag: `v5.23`
- Upstream commit: `4c4cbc291b361ceff6ee239448e9f988f9019dbb`
- BlueMapAPI 2.8 upstream base:
  `e20166d5ac93feab653392cf30a305a3e255754e`
- Java 21 BlueMapAPI fork commit:
  `285c9a6` on `jan-guenter/BlueMapAPI`
- Minecraft: 1.21.1
- NeoForge: 21.1.248
- Java: 21

The fork retains the BlueMap 5.23 API, core, common code, renderer, resource
system, and web application. It provides a dedicated NeoForge 1.21.1 platform
adapter and a stateless standalone Java web server. It intentionally does not
claim compatibility with BlueMap's other platform targets.

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
   21.1.248.
3. Start and stop a minimal dedicated server on Java 21.
4. Validate commands, permissions, dimensions, resources, web server, player
   data, and incremental updates.
5. Validate the exact All the Mons 1.2.0 staging pack with a fresh BlueMap
   setup and an unmodified client.
6. In the separate FramedBlocks add-on project, compile and load its first
   renderer skeleton against this build to exercise the external extension ABI.

## Validation status

The original 1.1.1 backport implementation is complete. The exact branch
commit tested before its validation status update was
`fe79cf5b9f4d8ca28f4e41c2aeb9ef792e336a8d`.

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

The public API remains BlueMapAPI 2.8 from the exact BlueMap 5.23 dependency,
retargeted only from Java 22 to Java 21. Building and loading the future
FramedBlocks renderer add-on is intentionally deferred to that add-on's
separate development session.

### All the Mons 1.2.0 loader refresh

The branch now targets NeoForge 21.1.248. The Java 21 compile/test gate and
merged-JAR audit passed. Dedicated-server validation passed both with a fresh
minimal NeoForge 21.1.248 installation and with the exact All the Mons 1.2.0
server archive (SHA-256
`de112ed8d79b3ff027e399a5108b706f6a2db3be74b15d0db6f6b9d6ac268e6c`).
The complete pack reached the ready state, BlueMap downloaded and loaded its
Minecraft resources, initialized all 17 dimensions, started its web server,
reloaded successfully, and then unloaded during a clean server shutdown. The
expected AE2 spatial-storage dimension metadata fallback remained unchanged.

This validates the server-side loader refresh. An unmodified All the Mons 1.2.0
client connection was not repeated as part of this loader-only update.

### BlueMap 5.23 rebase

The current branch rebases the NeoForge 1.21.1 and Java 21 adaptation onto the
exact upstream BlueMap 5.23 tag. Upstream 5.23 uses the same BlueMapAPI commit
as 5.22, so the existing Java 21 API fork remains the exact matching API base.

The Java 21 narrow compile gate and the full `spotlessCheck test release` gate
pass on the rebased branch. The resulting NeoForge JAR contains no class file
newer than Java 21, retains the Minecraft `[1.21.1,1.21.2)` and NeoForge
`[21.1.248,21.2)` ranges, and contains only the intended Flow Math and BlueNBT
nested dependencies. A fresh dedicated-server and full-pack smoke remain
release gates and were not repeated for this branch-only rebase.

### Standalone Java web server

The combined branch adds a Java 21 standalone web process, OCI image, and Helm
chart. The server keeps generated runtime files local to each replica and can
scale horizontally when map data is stored in MariaDB, MySQL, or PostgreSQL.
It includes bounded HTTP request handling, connection and storage-read limits,
graceful shutdown, and liveness and readiness endpoints.

Storage-backed validators make conditional requests consistent across
replicas. Stored map compression is passed through without recompressing normal
responses, and unsupported client encodings receive an explicit `406` response.
BlueMap's client-decompression `.gz` URLs remain compatible by returning raw
gzip files. Static, mutable, and private live-data responses use separate cache
policies so intermediaries do not retain player data or transform stored map
representations.

The standalone workflow and container use Java 21. The release build remains
limited to the NeoForge and web-server modules, and the BlueMapAPI submodule
stays pinned to the documented Java 21 fork commit.

Local validation passed 106 Java tests, the web-application tests, the full
`spotlessCheck test release` gate, workflow and shell checks, and all Helm chart
contract cases. The resulting NeoForge and web-server JARs contain no class
file newer than Java 21. The NeoForge JAR retains the documented Minecraft and
NeoForge dependency ranges and only the intended Flow Math and BlueNBT nested
libraries.
