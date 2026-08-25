# Agent guide for the BlueMap 5.23 backport

Read the workspace-root `AGENTS.md`, upstream `README.md`, and `BACKPORT.md`
before changing this repository.

## Repository scope

This is the `jan-guenter/BlueMap` fork of upstream
`BlueMap-Minecraft/BlueMap`, based on the exact `v5.23` commit documented in
`BACKPORT.md`.

The supported runtimes are Minecraft 1.21.1 with NeoForge 21.1.248 and the
standalone web server, both on Java 21. Preserve the upstream BlueMap 5.23
API/core/common behaviour wherever it does not depend on newer Minecraft or
Java APIs.

## Invariants

- Keep the BlueMapAPI submodule pinned to the documented Java 21 fork commit,
  based on the exact API commit shipped by BlueMap 5.23, unless a separately
  reviewed API update is requested.
- Do not cherry-pick the 5.23 feature set onto BlueMap 5.7.
- Keep the release build limited to the NeoForge and standalone web-server
  modules unless another platform is explicitly requested and validated.
- Produce Java 21 class files only.
- Preserve server-only behaviour and unmodified-client compatibility.
- Use fresh configuration and storage for every runtime test. Migration from
  5.7 is explicitly out of scope.
- Never deploy to production or write into production world, map, web, or
  database locations.
- Keep upstream provenance visible and use a fork-specific version; never
  publish the result as an unqualified upstream `5.23`.

## Validation

Run the narrow core/common/web-server compile and test gate first, followed by
the full release build and Java 21 class-version audit. Dedicated-server and
full-pack staging gates remain required before release.
