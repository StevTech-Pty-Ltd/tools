# Tools Repo Refactor — Design

**Date:** 2026-07-06
**Status:** Implemented on branch `tools-repo-refactor` (decisions flagged for review)

## Problem

Steven and Jacob agreed the spray packager should be distributed to customers
directly ("should the spray package repo be public so people can just download
it from there?" → "We should make a tools repo maybe public... tools for our
APIs and things if we release those too"). The repo as created was a single-tool
developer repo: no customer-facing entry point, no license, no built download —
customers would have had to build the exe themselves.

## Shape of the change

Restructure `spray-packager` into a multi-tool, customer-facing **StevTech
Tools** repo:

- `tools/spray-packager/` — the app, its tests, docs, and local build script.
  Future tools (API clients etc.) each get their own `tools/<name>/` folder.
- Root `README.md` — customer-facing landing page: tool catalog table,
  download-from-Releases instructions, SmartScreen guidance, support pointer
  (StevTech representative / issues; no generic support email exists, so none
  is invented).
- `LICENSE.md` — free-to-use-with-StevTech-services, no warranty, no
  redistribution. **Needs review before the repo is made public.**
- `.github/workflows/build-spray-packager.yml` — builds `SprayPackager.exe` on
  `windows-latest`: tests → PyInstaller → smoke test of the built exe
  (windowless via `SPRAY_PACKAGER_SMOKE`) → artifact; on `spray-packager-v*`
  tags it also attaches the exe to a GitHub Release. This removes the need for
  a Windows box entirely and gives customers a Releases page to download from.
- App branding: `__version__`, window title "StevTech Spray Packager vX.Y.Z",
  `--version` flag.

## Decisions taken (flag for review)

1. **Repo left private and un-renamed.** Making it public and renaming to
   something like `stevtech-tools` are one-command follow-ups, but both are
   outward-facing business calls (license sign-off first; rename touches
   teammates' remotes) — deferred to Jacob/Steven after merge.
2. **Release trigger is per-tool tags** (`spray-packager-v1.0.0`) so future
   tools can release independently in the same repo.
3. **Exes are unsigned.** SmartScreen will warn customers; documented in both
   READMEs. Code-signing certificate is the production follow-up if downloads
   go wide.
4. **CI also runs on PRs** touching the tool, so every change proves the
   Windows build + tests before merge.

## Testing

- Existing end-to-end suite unchanged, re-run from the new location.
- Headless GUI smoke test re-run.
- The PR build on `windows-latest` is the real proof: it exercises the exact
  customer artifact (PyInstaller exe with bundled GDAL) end to end.
