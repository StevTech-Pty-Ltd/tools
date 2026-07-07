"""End-to-end test for the packaging pipeline.

Synthesizes a small georeferenced RGB orthomosaic (with a nodata flag, like
DJI Terra output) plus a single-band sprayfile, runs create_package(), and
verifies the zip contents match what optimize_raster.sh used to produce:
JPEG-compressed, tiled, overviews built, nodata flag removed, georeferencing
preserved, sprayfile untouched.

Run directly (no pytest needed):  python tests/test_pipeline.py
"""

import hashlib
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

# rasterio 1.5 + numpy 2.5 emit a deprecation from read_masks internals; not
# ours and not a behaviour we control.
warnings.filterwarnings("ignore", category=DeprecationWarning)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spray_packager import create_package  # noqa: E402

CRS = "EPSG:32755"


def make_ortho(path: Path, width=1024, height=768) -> None:
    transform = from_origin(500000.0, 6000000.0, 0.05, 0.05)
    data = np.random.default_rng(42).integers(
        0, 255, size=(3, height, width), dtype=np.uint8)
    with rasterio.open(
            path, "w", driver="GTiff", width=width, height=height, count=3,
            dtype="uint8", crs=CRS, transform=transform, nodata=0) as dst:
        dst.write(data)


def make_ortho_rgba(path: Path, width=1024, height=768) -> None:
    """A DJI-Terra-style 4-band RGBA orthomosaic: photographic-ish content in
    RGB with a circular valid region, and an alpha band marking the valid
    area (0 = transparent border, 255 = valid) instead of a nodata flag."""
    transform = from_origin(500000.0, 6000000.0, 0.05, 0.05)
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:height, 0:width]
    base = (np.sin(xx / 40.0) * 40 + np.cos(yy / 55.0) * 40 + 128).astype(np.uint8)
    valid = np.sqrt((yy - height / 2) ** 2 + (xx - width / 2) ** 2) < width * 0.45
    data = np.zeros((4, height, width), dtype=np.uint8)
    for b in range(3):
        data[b] = np.clip(base + rng.integers(-10, 10, (height, width)), 0, 255)
        data[b][~valid] = 0
    data[3] = np.where(valid, 255, 0).astype(np.uint8)
    with rasterio.open(
            path, "w", driver="GTiff", width=width, height=height, count=4,
            dtype="uint8", crs=CRS, transform=transform) as dst:
        dst.write(data)
        dst.colorinterp = [
            rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
            rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]


def make_segment(path: Path, size=200) -> None:
    transform = from_origin(500000.0, 6000000.0, 0.5, 0.5)
    data = (np.random.default_rng(7).random((1, size, size)) > 0.7).astype("uint8")
    with rasterio.open(
            path, "w", driver="GTiff", width=size, height=size, count=1,
            dtype="uint8", crs=CRS, transform=transform) as dst:
        dst.write(data)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_create_package() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        result = tmp / "Result.tif"
        segment = tmp / "Segment.tif"
        out_zip = tmp / "out" / "package.zip"
        make_ortho(result)
        make_segment(segment)
        segment_hash = sha256(segment)

        created = create_package(result, segment, out_zip, quality=40)

        assert created == out_zip and out_zip.is_file(), "zip not created"
        assert not list(out_zip.parent.glob("*.part")), "partial file left behind"

        with zipfile.ZipFile(out_zip) as zf:
            names = sorted(zf.namelist())
            assert names == ["Result.tif", "Segment.tif"], names
            infos = {i.filename: i for i in zf.infolist()}
            assert infos["Result.tif"].compress_type == zipfile.ZIP_STORED
            assert infos["Segment.tif"].compress_type == zipfile.ZIP_DEFLATED
            extract_dir = tmp / "extracted"
            zf.extractall(extract_dir)

        assert sha256(extract_dir / "Segment.tif") == segment_hash, \
            "sprayfile was modified"

        with rasterio.open(extract_dir / "Result.tif") as ds:
            assert ds.width == 1024 and ds.height == 768
            comp = ds.compression
            assert comp is not None and comp.value.lower() == "jpeg", comp
            assert ds.photometric is not None and \
                ds.photometric.value.lower() == "ycbcr", ds.photometric
            assert ds.block_shapes[0][0] > 1, "output is not tiled"
            assert ds.nodata is None, f"nodata flag not cleared: {ds.nodata}"
            # 1024/128 = 8 makes 128 the largest level that keeps >= 8 px
            assert ds.overviews(1) == [2, 4, 8, 16, 32, 64, 128], ds.overviews(1)
            assert str(ds.crs) == CRS
            assert abs(ds.transform.a - 0.05) < 1e-9
            assert abs(ds.transform.c - 500000.0) < 1e-6

        original = result.stat().st_size
        optimized = (extract_dir / "Result.tif").stat().st_size
        assert optimized < original, (original, optimized)

    print("PASS: create_package end-to-end")


def test_non_uint8_falls_back_to_deflate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        result = tmp / "Result.tif"
        transform = from_origin(500000.0, 6000000.0, 0.5, 0.5)
        data = np.random.default_rng(1).random((1, 300, 300)).astype("float32")
        with rasterio.open(
                result, "w", driver="GTiff", width=300, height=300, count=1,
                dtype="float32", crs=CRS, transform=transform) as dst:
            dst.write(data)
        segment = tmp / "Segment.tif"
        make_segment(segment)

        out_zip = create_package(result, segment, tmp / "pkg.zip")
        with zipfile.ZipFile(out_zip) as zf:
            zf.extractall(tmp / "x")
        with rasterio.open(tmp / "x" / "Result.tif") as ds:
            assert ds.compression.value.lower() == "deflate", ds.compression
            assert ds.overviews(1), "no overviews built"

    print("PASS: non-uint8 fallback to DEFLATE")


def test_cli_return_codes() -> None:
    """The retro CLI must exit 0 on success (with a JOB COMPLETE line) and
    with the documented RC=2 when an input file is missing."""
    import subprocess
    script = Path(__file__).resolve().parents[1] / "spray_packager.py"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        result = tmp / "Result.tif"
        segment = tmp / "Segment.tif"
        make_ortho(result, width=512, height=512)
        make_segment(segment)

        ok = subprocess.run(
            [sys.executable, str(script), "--result", str(result),
             "--segment", str(segment), "--zip", str(tmp / "pkg.zip")],
            capture_output=True, text=True)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        assert "JOB COMPLETE" in ok.stdout, ok.stdout
        assert (tmp / "pkg.zip").is_file()

        missing = subprocess.run(
            [sys.executable, str(script), "--result", str(tmp / "nope.tif"),
             "--segment", str(segment), "--zip", str(tmp / "pkg2.zip")],
            capture_output=True, text=True)
        assert missing.returncode == 2, (missing.returncode, missing.stdout)
        assert "RC=2" in missing.stdout, missing.stdout

        bad_quality = subprocess.run(
            [sys.executable, str(script), "--result", str(result),
             "--segment", str(segment), "--zip", str(tmp / "pkg3.zip"),
             "--quality", "99"],
            capture_output=True, text=True)
        assert bad_quality.returncode == 3, bad_quality.stdout

    print("PASS: CLI return codes")


def test_rgba_uses_ycbcr_and_mask() -> None:
    """A 4-band RGBA orthomosaic (DJI Terra's standard output) must be
    packaged as a 3-band YCbCr-JPEG with the alpha carried as an internal
    mask -- not as a 4-band JPEG, which cannot use YCbCr and compresses
    several times worse. The valid-data area must survive as a mask so QGIS
    still renders transparency, including at overview resolution."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        result = tmp / "Result.tif"
        segment = tmp / "Segment.tif"
        out_zip = tmp / "pkg.zip"
        make_ortho_rgba(result, width=2048, height=1536)
        make_segment(segment)

        create_package(result, segment, out_zip, quality=40)

        extract = tmp / "x"
        with zipfile.ZipFile(out_zip) as zf:
            zf.extractall(extract)

        with rasterio.open(extract / "Result.tif") as ds:
            assert ds.count == 3, f"expected 3 colour bands, got {ds.count}"
            assert ds.compression is not None and \
                ds.compression.value.lower() == "jpeg", ds.compression
            assert ds.photometric is not None and \
                ds.photometric.value.lower() == "ycbcr", ds.photometric
            # alpha preserved as a per-dataset mask (transparency for QGIS)
            assert rasterio.enums.MaskFlags.per_dataset in ds.mask_flag_enums[0], \
                ds.mask_flag_enums[0]
            valid_frac = (ds.read_masks(1) > 0).mean()
            assert 0.4 < valid_frac < 0.9, f"mask valid fraction off: {valid_frac}"
            # overviews cover the mask too, so zoomed-out transparency is right
            assert ds.overviews(1), "no overviews built"
        with rasterio.open(extract / "Result.tif", OVERVIEW_LEVEL=1) as ov:
            ov_valid = (ov.read_masks(1) > 0).mean()
            assert 0.3 < ov_valid < 0.95, f"overview mask lost: {ov_valid}"

        # efficient: a 4-band no-YCbCr JPEG of this content is >3x larger
        opt_size = None
        with zipfile.ZipFile(out_zip) as zf:
            opt_size = zf.getinfo("Result.tif").file_size
        assert opt_size < result.stat().st_size / 8, \
            f"weak compression: {opt_size} vs {result.stat().st_size}"

    print("PASS: RGBA uses YCbCr + mask")


if __name__ == "__main__":
    test_create_package()
    test_non_uint8_falls_back_to_deflate()
    test_rgba_uses_ycbcr_and_mask()
    test_cli_return_codes()
    print("All tests passed.")
