# Spray Packager

Desktop app that packages DJI Terra output for sending in: it takes the
orthomosaic (**Result.tif**) and the sprayfile (**Segment.tif**), optimizes the
orthomosaic for fast viewing in QGIS (JPEG-compressed tiled GeoTIFF with
overview pyramids — the same treatment as the old `optimize_raster.sh`), and
writes both into a single zip.

No GDAL install needed: the GDAL engine ships inside the app via
[rasterio](https://rasterio.readthedocs.io/).

## For operators (Windows)

1. Double-click `SprayPackager.exe`.
2. **Browse...** to the orthomosaic (usually `Result.tif`). If a `Segment.tif`
   sits in the same folder, it is filled in automatically; the zip name is
   suggested too.
3. Adjust anything you like, then press **Create Package**.
4. When it finishes, send the zip file it created.

The first launch can take ~20 seconds (the exe unpacks itself); later
launches are faster.

## Building the Windows exe

On any Windows machine with [Python 3.10+](https://www.python.org/downloads/)
installed ("Add python.exe to PATH" ticked):

```bat
build_windows.bat
```

The result is `dist\SprayPackager.exe` — a single file, distribute just that.

## Running from source (any platform)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python spray_packager.py           # GUI
```

Headless / scripted use:

```bash
python spray_packager.py --result Result.tif --segment Segment.tif \
    --zip package.zip --quality 40
```

## What "optimize" does (parity with optimize_raster.sh)

| Step | Old script | This app |
|---|---|---|
| Compression | `COMPRESS=JPEG`, `JPEG_QUALITY=40`, `TILED=YES`, `BIGTIFF=YES` | Same (quality adjustable in the GUI) |
| Nodata | `-a_nodata none` | Same (flag removed) |
| Overviews | `gdaladdo -r average 2 ... 512` | Same, levels capped to the image size |
| Extras | — | `PHOTOMETRIC=YCBCR` for 3-band images and JPEG-compressed overviews (both shrink the file further); non-8-bit input falls back to lossless DEFLATE instead of failing |

The sprayfile is never recompressed — it goes into the zip byte-for-byte. The
optimized orthomosaic is `STORED` in the zip (it is already JPEG-compressed;
deflating it again would waste minutes for ~1% saving).

## Tests

```bash
python tests/test_pipeline.py
```

Synthesizes a small orthomosaic + sprayfile and verifies the packaged output
(JPEG, tiled, overviews, nodata cleared, georeferencing intact, sprayfile
untouched).
