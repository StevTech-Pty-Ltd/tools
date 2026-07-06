# Spray Packager

Desktop app that prepares DJI Terra spray-drone output for submission to
StevTech. It takes the orthomosaic (**Result.tif**) and the sprayfile
(**Segment.tif**), optimizes the orthomosaic for fast viewing (JPEG-compressed
tiled GeoTIFF with overview pyramids), and writes both into a single zip ready
to send.

No GDAL or other software install needed — everything ships inside the app.

## Using it (Windows)

1. Download `SprayPackager.exe` from the
   [latest release](../../../../releases/latest) and double-click it.
   (First launch can take ~20 seconds while it unpacks itself.)
2. **Browse...** to the orthomosaic (usually `Result.tif`). If a `Segment.tif`
   sits in the same folder it is filled in automatically, and a zip name is
   suggested.
3. Press **Create Package**.
4. Send the zip it creates to StevTech.

If Windows shows a "Windows protected your PC" warning, click
**More info → Run anyway** — the tools are not yet code-signed.

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

## What "optimize" does

| Step | Setting |
|---|---|
| Compression | `COMPRESS=JPEG`, quality 40 (adjustable), `TILED=YES`, `BIGTIFF=YES`; `PHOTOMETRIC=YCBCR` for 3-band images |
| Nodata | Flag removed (avoids transparent speckle after lossy compression) |
| Overviews | Averaged pyramids, levels 2–512 capped to the image size, JPEG-compressed |
| Fallback | Non-8-bit input uses lossless DEFLATE instead of failing |

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

## Releasing (StevTech staff)

CI builds and tests the exe on every pull request that touches this tool. To
publish a customer download, tag the commit and push the tag:

```bash
git tag spray-packager-v1.0.0
git push origin spray-packager-v1.0.0
```

The `Build Spray Packager` workflow builds `SprayPackager.exe` on a Windows
runner, runs the test suite plus a smoke test of the built exe, and attaches
it to a GitHub Release. Bump `__version__` in `spray_packager.py` to match
the tag. A local build is also possible on any Windows machine with Python
installed: run `build_windows.bat`.
