# Developing RAW Photo Editor

Start with the [developer handoff / 开发交接](docs/development.md) for a fresh machine, build commands, the code map and tests. The [development log / 开发日志](docs/development-log.md) explains the design decisions and regressions behind the current implementation. Both guides cover the public v1.0.1 baseline. For later changes, read the entries and commits after that tag.

- The extension ID is `raw_photo_editor`. Source lives in `blender_raw/`. Blender loads the installed extension as `bl_ext.user_default.raw_photo_editor`.
- Each photo owns a Blender scene. Use its native `scene.compositor_effects` for effect order and the separate preview/render flags. Preserve the destination photo's source when copying adjustments.
- For an install or export change, build the ZIP and exercise the installed copy in a fresh profile. Some older scripts in `tests/` still target the pre-0.19 single-tree model. The handoff identifies the installed-package tests used for v1.0.1.
- For a release, build one installer and pass the same ZIP between Windows x64 and Apple Silicon. Run the applicable installed-package tests locally on each machine and record the SHA-256 with each result. Once both platforms pass on that hash, acceptance is complete. Publish those exact bytes when a release is requested. See the [release handoff](docs/development.md#版本与交接).
- When handing off a design change or a reproduced bug, append the version or commit, symptom, decision, relevant files, verification and remaining uncertainty to the development log. User-facing changes belong in the [changelog](CHANGELOG.md).
