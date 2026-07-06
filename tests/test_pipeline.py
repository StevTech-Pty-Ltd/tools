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
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

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


if __name__ == "__main__":
    test_create_package()
    test_non_uint8_falls_back_to_deflate()
    print("All tests passed.")
