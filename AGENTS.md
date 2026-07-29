# Agent guide for the BlueMap 5.22 backport

Read the workspace-root `AGENTS.md`, upstream `README.md`, and `BACKPORT.md`
before changing this repository.

## Repository scope

This is the `jan-guenter/BlueMap` fork of upstream
`BlueMap-Minecraft/BlueMap`, based on the exact `v5.22` commit documented in
`BACKPORT.md`.

The supported runtime is only Minecraft 1.21.1, NeoForge 21.1.234, and Java
21. Preserve the upstream BlueMap 5.22 API/core/common behaviour wherever it
does not depend on newer Minecraft or Java APIs.

## Invariants

- Keep the BlueMapAPI submodule pinned to the documented Java 21 fork commit,
  based on the exact API commit shipped by BlueMap 5.22, unless a separately
  reviewed API update is requested.
- Do not cherry-pick the 5.22 feature set onto BlueMap 5.7.
- Do not restore unsupported platform modules to the release build without
  explicit scope expansion and validation.
- Produce Java 21 class files only.
- Preserve server-only behaviour and unmodified-client compatibility.
- Use fresh configuration and storage for every runtime test. Migration from
  5.7 is explicitly out of scope.
- Never deploy to production or write into production world, map, web, or
  database locations.
- Keep upstream provenance visible and use a fork-specific version; never
  publish the result as an unqualified upstream `5.22`.

## Validation

Run the narrow compile/test gate first, followed by the merged NeoForge JAR
build and class-version/JAR-content audit. Dedicated-server and full-pack
staging gates remain required before release.
