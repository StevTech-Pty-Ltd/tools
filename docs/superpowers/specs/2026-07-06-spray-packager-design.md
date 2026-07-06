# Spray Packager — Design

**Date:** 2026-07-06
**Status:** Implemented (design decisions made autonomously; flagged for review)

## Problem

Field operators export two files from DJI Terra: `Result.tif` (the orthomosaic) and
`Segment.tif` (the sprayfile). These need to be sent in as a single zip. Before zipping,
the orthomosaic must be optimized the same way `optimize_raster.sh` does it today
(JPEG-compressed tiled GeoTIFF + overview pyramids) so it opens fast in QGIS on the
receiving side. The current bash script needs GDAL installed and a terminal — non-starters
for non-technical Windows users.

## Requirements

- Runs on Windows, usable by non-technical users (file pickers, one button).
- Select Result.tif, Segment.tif, and the output zip name/location.
- Optimize Result.tif: JPEG compression (default quality 40), tiled, BIGTIFF, nodata
  removed, averaged overviews at levels 2–512 — parity with the bash script.
- Segment.tif goes into the zip untouched (it is prescription data, never recompress).
- No separate GDAL install on end-user machines.

## Approaches considered

1. **Tkinter GUI + rasterio + PyInstaller one-file exe (chosen).** `rasterio` wheels
   bundle the GDAL binaries, so `pip install rasterio` is the whole dependency story and
   PyInstaller can freeze everything into a double-clickable `SprayPackager.exe`.
   Optimization uses GDAL through rasterio's API in-process — same engine as
   `gdal_translate`/`gdaladdo`.
2. GUI shelling out to `gdal_translate.exe`/`gdaladdo.exe`. Rejected: requires
   OSGeo4W/QGIS on every operator machine and brittle PATH discovery.
3. PyQt6 GUI. Rejected: much heavier to bundle; tkinter is plenty for a three-field form
   and ships with CPython.

## Architecture

Single file `spray_packager.py`, three layers:

- **Core pipeline (no GUI imports)** — `optimize_geotiff()`, `create_package()`.
  Testable headless and reusable from a CLI.
  1. Copy Result.tif with creation options `TILED=YES`, `BIGTIFF=YES`, `COMPRESS=JPEG`,
     `JPEG_QUALITY=<q>`; adds `PHOTOMETRIC=YCBCR` when the image is 3-band uint8
     (large size win, matches common ortho layout). If the image is not 8-bit (JPEG
     would hard-fail), falls back to lossless DEFLATE and says so in the log.
  2. Unset nodata (equivalent of `-a_nodata none`).
  3. Build internal overviews, average resampling, levels 2–512 filtered to the image
     size, overview compression matching the base raster.
  4. Zip: optimized tif STORED (it is already JPEG-compressed; deflating giant files
     again wastes minutes for ~1%), Segment.tif DEFLATED. Original basenames kept —
     the receiving pipeline has no hard-coded names, and operators recognize them.
  The optimized tif is written to a temp dir **next to the output zip** (same drive —
  avoids filling `C:\Temp` when output goes to another disk) and deleted after zipping.
- **Tkinter GUI** — three pickers, JPEG-quality spinbox (default 40), Create Package
  button, progress bar, log pane. Work runs on a background thread; UI updates flow
  through a `queue.Queue` polled with `after()` (tkinter is not thread-safe).
  Convenience: picking Result.tif auto-fills Segment.tif if one sits beside it, and
  suggests a zip path. On success, offers to open the output folder.
- **CLI mode** — `--result/--segment/--zip [--quality]` runs headless (testing,
  power users). No flags → GUI.

## Error handling

- Input validation before starting (files exist, are .tif, distinct; zip dir exists).
- Pipeline exceptions surface as an error dialog with the message, full detail in the
  log pane; partial temp output is always cleaned up.
- Nodata-unset failure is non-fatal (logged warning; file keeps its nodata like the
  original did).

## Testing

- End-to-end headless test: synthesize a small RGB uint8 GeoTIFF (with georeferencing
  and nodata) and a single-band Segment.tif, run `create_package()` via the CLI path,
  then assert: zip has both entries, extracted ortho is JPEG-compressed + tiled, has
  averaged overviews, nodata is gone, georeferencing preserved.
- GUI smoke-tested manually (macOS during development; Windows for release).

## Distribution

`build_windows.bat` on any Windows machine: creates a venv, installs
`rasterio` + `pyinstaller`, emits `dist\SprayPackager.exe` (one file, windowed,
`--collect-all rasterio` so GDAL/PROJ data ships inside). Operators get just the exe.

## Open items for reviewer

- Zip entry names: kept as the original basenames (`Result.tif`, `Segment.tif`). If the
  receiving side would rather see `Result_optimized.tif`, it is a one-line change.
- Default JPEG quality 40 kept from the bash script; exposed in the GUI.
