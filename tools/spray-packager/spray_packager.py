#!/usr/bin/env python3
"""StevTech Spray Packager.

Packages DJI Terra output for submission to StevTech: optimizes the
orthomosaic (Result.tif) into a JPEG-compressed tiled GeoTIFF with overview
pyramids and zips it together with the untouched sprayfile (Segment.tif).

Run with no arguments for the GUI. Headless mode:

    python spray_packager.py --result Result.tif --segment Segment.tif --zip out.zip
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import tempfile
import threading
import traceback
import zipfile
from datetime import date
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_QUALITY = 40
OVERVIEW_LEVELS = (2, 4, 8, 16, 32, 64, 128, 256, 512)

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.shutil import copy as raster_copy

    RASTERIO_IMPORT_ERROR = None
except ImportError as exc:  # keep the GUI able to start and explain the problem
    rasterio = None
    RASTERIO_IMPORT_ERROR = exc


# --------------------------------------------------------------------------
# Core pipeline (no GUI imports)
# --------------------------------------------------------------------------

def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def optimize_geotiff(src_path, dst_path, quality: int = DEFAULT_QUALITY, log=print) -> str:
    """Equivalent of optimize_raster.sh: JPEG-compressed tiled GeoTIFF with
    averaged overview pyramids and the nodata flag removed."""
    src_path, dst_path = os.fspath(src_path), os.fspath(dst_path)

    with rasterio.open(src_path) as src:
        count = src.count
        dtype = src.dtypes[0]
        width, height = src.width, src.height

    options = {"TILED": "YES", "BIGTIFF": "YES"}
    if dtype == "uint8":
        options.update(COMPRESS="JPEG", JPEG_QUALITY=str(quality))
        if count == 3:
            options["PHOTOMETRIC"] = "YCBCR"
    else:
        # JPEG compression only exists for 8-bit data; fail soft into lossless.
        log(f"Note: image is {dtype}, not 8-bit, so JPEG does not apply. "
            "Using lossless DEFLATE compression instead.")
        options.update(
            COMPRESS="DEFLATE",
            PREDICTOR="3" if dtype.startswith("float") else "2",
        )

    detail = f"quality {quality}" if options["COMPRESS"] == "JPEG" else "lossless"
    log(f"Compressing {os.path.basename(src_path)} "
        f"({width} x {height}, {count} band(s), {options['COMPRESS']} {detail})...")
    raster_copy(src_path, dst_path, driver="GTiff", **options)

    # The bash script passed -a_nodata none: without it, lossy JPEG shifts
    # values near the old nodata and QGIS renders transparent speckle.
    try:
        with rasterio.open(dst_path, "r+") as ds:
            if ds.nodata is not None:
                ds.nodata = None
    except Exception as exc:
        log(f"Warning: could not clear the nodata flag ({exc}); continuing.")

    levels = [lvl for lvl in OVERVIEW_LEVELS if max(width, height) / lvl >= 8]
    if levels:
        log(f"Building overviews (levels {', '.join(map(str, levels))})...")
        overview_env = {"COMPRESS_OVERVIEW": options["COMPRESS"]}
        if options["COMPRESS"] == "JPEG":
            overview_env["JPEG_QUALITY_OVERVIEW"] = str(quality)
        if options.get("PHOTOMETRIC") == "YCBCR":
            overview_env["PHOTOMETRIC_OVERVIEW"] = "YCBCR"
        with rasterio.Env(**overview_env):
            with rasterio.open(dst_path, "r+") as ds:
                ds.build_overviews(levels, Resampling.average)
    return dst_path


def create_package(result_tif, segment_tif, zip_path,
                   quality: int = DEFAULT_QUALITY, log=print, progress=None) -> Path:
    """Optimize the orthomosaic, then zip it with the untouched sprayfile.

    ``progress(fraction, message)`` is an optional coarse phase callback.
    Returns the final zip path.
    """
    result_tif = Path(result_tif)
    segment_tif = Path(segment_tif)
    zip_path = Path(zip_path)

    for label, path in (("Orthomosaic (Result)", result_tif),
                        ("Sprayfile (Segment)", segment_tif)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
    if result_tif.resolve() == segment_tif.resolve():
        raise ValueError("The Result and Segment selections point at the same file.")
    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path.with_name(zip_path.name + ".zip")
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    result_name = result_tif.name
    segment_name = segment_tif.name
    if result_name == segment_name:
        segment_name = f"{segment_tif.stem}_segment{segment_tif.suffix}"

    # Work next to the destination so the big intermediate lands on the same
    # drive as the zip instead of filling the system temp directory.
    with tempfile.TemporaryDirectory(prefix="spraypkg_", dir=zip_path.parent) as tmp:
        optimized = Path(tmp) / result_name

        if progress:
            progress(0.1, "Optimizing orthomosaic...")
        optimize_geotiff(result_tif, optimized, quality=quality, log=log)
        log(f"Orthomosaic size: {format_size(result_tif.stat().st_size)}"
            f" -> {format_size(optimized.stat().st_size)}")

        if progress:
            progress(0.75, "Writing zip...")
        log(f"Writing {zip_path.name}...")
        # Write to a .part file and rename, so a half-written zip is never
        # mistaken for a finished one.
        partial = zip_path.with_name(zip_path.name + ".part")
        try:
            with zipfile.ZipFile(partial, "w", allowZip64=True) as zf:
                # The optimized tif is already JPEG-compressed; deflating it
                # again costs minutes for ~1%, so store it as-is.
                zf.write(optimized, arcname=result_name,
                         compress_type=zipfile.ZIP_STORED)
                zf.write(segment_tif, arcname=segment_name,
                         compress_type=zipfile.ZIP_DEFLATED)
            os.replace(partial, zip_path)
        finally:
            if partial.exists():
                partial.unlink(missing_ok=True)

    if progress:
        progress(1.0, "Done")
    log(f"Package ready: {zip_path} ({format_size(zip_path.stat().st_size)})")
    return zip_path


def open_folder(path: Path) -> None:
    """Open a directory in the platform file manager. Best effort."""
    import subprocess
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on HiDPI
        except Exception:
            pass

    root = tk.Tk()
    root.title(f"StevTech Spray Packager v{__version__}")
    root.minsize(640, 480)

    if RASTERIO_IMPORT_ERROR is not None:
        messagebox.showerror(
            "Missing component",
            "The rasterio/GDAL component failed to load, so images cannot be "
            "processed.\n\nIf you are running from source: pip install rasterio\n"
            f"\nDetails: {RASTERIO_IMPORT_ERROR}")
        root.destroy()
        return

    result_var = tk.StringVar()
    segment_var = tk.StringVar()
    zip_var = tk.StringVar()
    quality_var = tk.StringVar(value=str(DEFAULT_QUALITY))
    status_var = tk.StringVar(value="Select the files, then press Create Package.")

    ui_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(1, weight=1)

    tif_types = [("GeoTIFF images", "*.tif *.tiff"), ("All files", "*.*")]

    def suggest_paths(result_path: str) -> None:
        """After picking Result.tif, prefill the sprayfile and zip fields."""
        folder = Path(result_path).parent
        if not segment_var.get():
            candidate = next(
                (p for p in sorted(folder.glob("*.tif*"))
                 if p.stem.lower() == "segment"), None)
            if candidate:
                segment_var.set(str(candidate))
        if not zip_var.get():
            name = folder.name or "spray"
            zip_var.set(str(folder / f"{name}_spray_package_{date.today():%Y-%m-%d}.zip"))

    def browse_result() -> None:
        path = filedialog.askopenfilename(
            title="Select the orthomosaic (usually Result.tif)",
            filetypes=tif_types)
        if path:
            result_var.set(path)
            suggest_paths(path)

    def browse_segment() -> None:
        path = filedialog.askopenfilename(
            title="Select the sprayfile (usually Segment.tif)",
            filetypes=tif_types)
        if path:
            segment_var.set(path)

    def browse_zip() -> None:
        initial = Path(zip_var.get()) if zip_var.get() else None
        path = filedialog.asksaveasfilename(
            title="Save package as",
            defaultextension=".zip",
            filetypes=[("Zip archives", "*.zip")],
            initialdir=str(initial.parent) if initial else None,
            initialfile=initial.name if initial else "spray_package.zip")
        if path:
            zip_var.set(path)

    rows = [
        ("Orthomosaic (Result.tif)", result_var, browse_result),
        ("Sprayfile (Segment.tif)", segment_var, browse_segment),
        ("Save package as (.zip)", zip_var, browse_zip),
    ]
    for row, (label, var, handler) in enumerate(rows):
        ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w",
                                          pady=4, padx=(0, 8))
        ttk.Entry(outer, textvariable=var).grid(row=row, column=1,
                                                sticky="ew", pady=4)
        ttk.Button(outer, text="Browse...", command=handler).grid(
            row=row, column=2, padx=(8, 0), pady=4)

    quality_frame = ttk.Frame(outer)
    quality_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 4))
    ttk.Label(quality_frame, text="JPEG quality:").pack(side="left")
    quality_box = ttk.Spinbox(quality_frame, from_=10, to=95, increment=5,
                              width=5, textvariable=quality_var)
    quality_box.pack(side="left", padx=(6, 6))
    ttk.Label(quality_frame,
              text="(lower = smaller file; 40 is the standard setting)"
              ).pack(side="left")

    run_button = ttk.Button(outer, text="Create Package")
    run_button.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 6),
                    ipady=6)

    progress_bar = ttk.Progressbar(outer, maximum=100)
    progress_bar.grid(row=5, column=0, columnspan=3, sticky="ew")
    ttk.Label(outer, textvariable=status_var).grid(
        row=6, column=0, columnspan=3, sticky="w", pady=(4, 8))

    log_text = tk.Text(outer, height=12, state="disabled", wrap="word")
    log_text.grid(row=7, column=0, columnspan=3, sticky="nsew")
    scroll = ttk.Scrollbar(outer, command=log_text.yview)
    scroll.grid(row=7, column=3, sticky="ns")
    log_text.configure(yscrollcommand=scroll.set)
    outer.rowconfigure(7, weight=1)

    def append_log(message: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", message + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def set_busy(busy: bool) -> None:
        state = "disabled" if busy else "normal"
        run_button.configure(state=state)
        quality_box.configure(state=state)

    def worker(result: str, segment: str, zip_path: str, quality: int) -> None:
        try:
            out = create_package(
                result, segment, zip_path, quality,
                log=lambda m: ui_queue.put(("log", m)),
                progress=lambda f, m: ui_queue.put(("progress", (f, m))))
            ui_queue.put(("done", str(out)))
        except Exception:
            ui_queue.put(("error", traceback.format_exc()))

    def on_run() -> None:
        result = result_var.get().strip()
        segment = segment_var.get().strip()
        zip_path = zip_var.get().strip()

        problems = []
        if not result or not Path(result).is_file():
            problems.append("Select the orthomosaic (Result.tif).")
        if not segment or not Path(segment).is_file():
            problems.append("Select the sprayfile (Segment.tif).")
        if result and segment and result == segment:
            problems.append("Result and Segment must be different files.")
        if not zip_path:
            problems.append("Choose where to save the zip package.")
        try:
            quality = int(quality_var.get())
            if not 10 <= quality <= 95:
                raise ValueError
        except ValueError:
            problems.append("JPEG quality must be a number between 10 and 95.")
            quality = DEFAULT_QUALITY
        if problems:
            messagebox.showwarning("Almost there", "\n".join(problems))
            return
        if Path(zip_path).exists() and not messagebox.askyesno(
                "Replace file?",
                f"{Path(zip_path).name} already exists.\nReplace it?"):
            return

        set_busy(True)
        progress_bar["value"] = 2
        status_var.set("Working...")
        append_log("-" * 60)
        threading.Thread(target=worker,
                         args=(result, segment, zip_path, quality),
                         daemon=True).start()

    run_button.configure(command=on_run)

    def poll_queue() -> None:
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "log":
                    append_log(str(payload))
                elif kind == "progress":
                    fraction, message = payload  # type: ignore[misc]
                    progress_bar["value"] = fraction * 100
                    status_var.set(message)
                elif kind == "done":
                    progress_bar["value"] = 100
                    status_var.set("Package created.")
                    set_busy(False)
                    out = Path(str(payload))
                    if messagebox.askyesno(
                            "Package created",
                            f"Created {out.name}\n({format_size(out.stat().st_size)})\n\n"
                            "Open the folder it was saved in?"):
                        open_folder(out.parent)
                elif kind == "error":
                    detail = str(payload)
                    append_log(detail)
                    last_line = detail.strip().splitlines()[-1]
                    progress_bar["value"] = 0
                    status_var.set("Failed - see log below.")
                    set_busy(False)
                    messagebox.showerror(
                        "Something went wrong",
                        f"The package was not created.\n\n{last_line}")
        except queue.Empty:
            pass
        root.after(100, poll_queue)

    poll_queue()
    if os.environ.get("SPRAY_PACKAGER_SMOKE"):
        # Headless smoke test: build every widget, run the event loop briefly,
        # exit without showing a window.
        root.withdraw()
        root.after(400, root.destroy)
    root.mainloop()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Optimize a DJI Terra orthomosaic and zip it with the "
                    "sprayfile. Run with no arguments for the GUI.")
    parser.add_argument("--version", action="version",
                        version=f"Spray Packager {__version__}")
    parser.add_argument("--result", help="Path to the orthomosaic (Result.tif)")
    parser.add_argument("--segment", help="Path to the sprayfile (Segment.tif)")
    parser.add_argument("--zip", dest="zip_path", help="Output zip path")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                        help=f"JPEG quality (default {DEFAULT_QUALITY})")
    args = parser.parse_args(argv)

    if args.result or args.segment or args.zip_path:
        if not (args.result and args.segment and args.zip_path):
            parser.error("--result, --segment and --zip must be given together")
        if RASTERIO_IMPORT_ERROR is not None:
            sys.exit(f"rasterio failed to import: {RASTERIO_IMPORT_ERROR}\n"
                     "Install it with: pip install rasterio")
        create_package(args.result, args.segment, args.zip_path, args.quality)
    else:
        run_gui()


if __name__ == "__main__":
    main()
