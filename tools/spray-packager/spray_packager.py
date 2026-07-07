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
import time
import traceback
import zipfile
from pathlib import Path

__version__ = "1.1.1"

DEFAULT_QUALITY = 40
OVERVIEW_LEVELS = (2, 4, 8, 16, 32, 64, 128, 256, 512)
ZIP_CHUNK = 8 * 1024 * 1024

try:
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    RASTERIO_IMPORT_ERROR = None
except ImportError as exc:  # keep the GUI able to start and explain the problem
    rasterio = None
    RASTERIO_IMPORT_ERROR = exc


# --------------------------------------------------------------------------
# Progress reporting protocol
# --------------------------------------------------------------------------

class Reporter:
    """Progress sink for the pipeline. The GUI and the retro CLI each render
    these callbacks their own way; the default is silence."""

    def phase(self, index: int, count: int, title: str, detail: str = "") -> None:
        """A new phase of work is starting."""

    def tick(self, done: float, total: float, unit: str = "B") -> None:
        """Progress within the current phase. ``unit`` is "B" or "level"."""

    def log(self, message: str) -> None:
        """A line worth showing to the user."""


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# --------------------------------------------------------------------------
# Core pipeline (no GUI imports)
# --------------------------------------------------------------------------

def _plan_options(count: int, dtype: str, quality: int, rep: Reporter) -> dict:
    """Creation options matching the old optimize_raster.sh treatment.

    A 4-band uint8 raster is DJI Terra's RGB + alpha. JPEG cannot use the
    efficient YCbCr colour transform on 4 bands, so those orthos compress
    several times worse if left as-is. We instead write the 3 colour bands as
    YCbCr JPEG and carry the alpha as an internal 1-bit mask (transparency is
    preserved for QGIS). ``out_count`` and ``use_mask`` describe that plan.
    """
    options = {"tiled": True, "blockxsize": 256, "blockysize": 256,
               "bigtiff": "YES"}
    out_count, use_mask = count, False
    if dtype == "uint8":
        options.update(compress="JPEG", JPEG_QUALITY=str(quality))
        if count == 4:
            out_count, use_mask = 3, True
            options["photometric"] = "YCbCr"
        elif count == 3:
            options["photometric"] = "YCbCr"
    else:
        # JPEG compression only exists for 8-bit data; fail soft into lossless.
        rep.log(f"NOTE image is {dtype}, not 8-bit; JPEG does not apply. "
                "Falling back to lossless DEFLATE.")
        options.update(compress="DEFLATE",
                       predictor=3 if dtype.startswith("float") else 2)
    return {"options": options, "out_count": out_count, "use_mask": use_mask}


def _compress_windowed(src_path: Path, dst_path: Path, quality: int,
                       rep: Reporter) -> dict:
    """Rewrite the raster tile-by-tile with the planned creation options.

    Windowed copying (vs GDAL CreateCopy) measures within ~2% at scale and
    buys two things: real byte progress, and the nodata flag is simply never
    written -- the equivalent of gdal_translate -a_nodata none. Returns the
    plan dict (options + mask flag) that overview building needs.
    """
    with rasterio.open(src_path) as src:
        plan = _plan_options(src.count, src.dtypes[0], quality, rep)
        options, out_count, use_mask = (
            plan["options"], plan["out_count"], plan["use_mask"])
        profile = dict(driver="GTiff", width=src.width, height=src.height,
                       count=out_count, dtype=src.dtypes[0], crs=src.crs,
                       transform=src.transform, nodata=None, **options)
        pixel_bytes = src.count * np.dtype(src.dtypes[0]).itemsize
        total = src.width * src.height * pixel_bytes
        # Strips of whole tile-rows, sized ~64 MB raw, keep Python overhead
        # negligible while feeding GDAL's tile cache in order.
        strip = max(256, int(64e6 / (src.width * pixel_bytes)) // 256 * 256)
        colour_bands = list(range(1, out_count + 1))
        done = 0
        # Internal mask so the alpha rides inside the single .tif.
        env = {"GDAL_TIFF_INTERNAL_MASK": "YES"} if use_mask else {}
        with rasterio.Env(**env), rasterio.open(dst_path, "w", **profile) as dst:
            if "photometric" not in options:
                try:
                    dst.colorinterp = src.colorinterp
                except Exception:
                    pass
            for row in range(0, src.height, strip):
                height = min(strip, src.height - row)
                window = Window(0, row, src.width, height)
                dst.write(src.read(colour_bands, window=window), window=window)
                if use_mask:
                    dst.write_mask(src.read(src.count, window=window),
                                   window=window)
                done += src.width * height * pixel_bytes
                rep.tick(done, total)
    return plan


def _build_overviews(dst_path: Path, levels: list[int], plan: dict,
                     rep: Reporter) -> None:
    """Add averaged overviews in a single cascaded pass.

    One ``build_overviews(levels)`` call builds each level from the previous
    (like a single gdaladdo run). Calling it once per level instead re-reads
    the full-resolution base every time -- ~5x slower on large orthos, which
    looks like a hang right after compression hits 100%.
    """
    options = plan["options"]
    env = {"COMPRESS_OVERVIEW": str(options.get("compress", "DEFLATE"))}
    if options.get("compress") == "JPEG":
        env["JPEG_QUALITY_OVERVIEW"] = options["JPEG_QUALITY"]
    if options.get("photometric") == "YCbCr":
        env["PHOTOMETRIC_OVERVIEW"] = "YCBCR"
    if plan["use_mask"]:
        env["GDAL_TIFF_INTERNAL_MASK"] = "YES"  # build mask overviews too
    rep.tick(0, len(levels), unit="level")
    with rasterio.Env(**env):
        with rasterio.open(dst_path, "r+") as ds:
            ds.build_overviews(levels, Resampling.average)
    rep.tick(len(levels), len(levels), unit="level")


def _zip_files(zip_path: Path, entries: list[tuple[Path, str, int]],
               rep: Reporter) -> None:
    """Stream files into the zip in chunks so progress is real. Writes to a
    .part file and renames, so a half-written zip never looks finished."""
    total = sum(path.stat().st_size for path, _, _ in entries)
    done = 0
    partial = zip_path.with_name(zip_path.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as zf:
            for path, arcname, compress_type in entries:
                info = zipfile.ZipInfo.from_file(path, arcname)
                info.compress_type = compress_type
                with open(path, "rb") as src, \
                        zf.open(info, "w", force_zip64=True) as dst:
                    while chunk := src.read(ZIP_CHUNK):
                        dst.write(chunk)
                        done += len(chunk)
                        rep.tick(done, total)
        os.replace(partial, zip_path)
    finally:
        if partial.exists():
            partial.unlink(missing_ok=True)


def create_package(result_tif, segment_tif, zip_path,
                   quality: int = DEFAULT_QUALITY,
                   reporter: Reporter | None = None) -> Path:
    """Optimize the orthomosaic, then zip it with the untouched sprayfile.
    Returns the final zip path."""
    rep = reporter or Reporter()
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

    with rasterio.open(result_tif) as src:
        width, height = src.width, src.height
        bands = src.count
    levels = [lvl for lvl in OVERVIEW_LEVELS if max(width, height) / lvl >= 8]

    # Work next to the destination so the big intermediate lands on the same
    # drive as the zip instead of filling the system temp directory.
    with tempfile.TemporaryDirectory(prefix="spraypkg_", dir=zip_path.parent) as tmp:
        optimized = Path(tmp) / result_name

        rep.phase(1, 3, "COMPRESS ORTHOMOSAIC",
                  f"{result_name}  {width} x {height}  {bands} band(s)")
        plan = _compress_windowed(result_tif, optimized, quality, rep)
        # Note: the optimized size here is the compressed base; overviews
        # (phase 2) add it back on top, so the final zip is a little larger.
        orig = result_tif.stat().st_size
        comp = optimized.stat().st_size
        ratio = orig / comp if comp else 0
        method = ("DEFLATE lossless"
                  if plan["options"].get("compress") == "DEFLATE"
                  else f"JPEG q{quality}")
        rep.log(f"orthomosaic compressed {format_size(orig)}"
                f" -> {format_size(comp)}  ({ratio:.0f}x smaller, {method})")

        rep.phase(2, 3, "BUILD OVERVIEWS",
                  f"levels {levels[0]}..{levels[-1]}" if levels else "none needed")
        if levels:
            _build_overviews(optimized, levels, plan, rep)

        rep.phase(3, 3, "WRITE PACKAGE", zip_path.name)
        _zip_files(zip_path, [
            # already JPEG-compressed; deflating again wastes minutes for ~1%
            (optimized, result_name, zipfile.ZIP_STORED),
            (segment_tif, segment_name, zipfile.ZIP_DEFLATED),
        ], rep)

    rep.log(f"package ready: {zip_path.name}"
            f"  {format_size(zip_path.stat().st_size)}")
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

class QueueReporter(Reporter):
    """Forwards pipeline events onto a queue the tkinter thread drains."""

    def __init__(self, q: "queue.Queue") -> None:
        self.q = q

    def phase(self, index, count, title, detail=""):
        self.q.put(("phase", (index, count, title, detail)))

    def tick(self, done, total, unit="B"):
        self.q.put(("tick", (done, total, unit)))

    def log(self, message):
        self.q.put(("log", message))


def run_gui() -> None:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on HiDPI
        except Exception:
            pass

    root = tk.Tk()
    root.title(f"StevTech Spray Packager v{__version__}")
    root.minsize(680, 500)

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

    def autofill_segment(result_path: str) -> None:
        """Picking Result.tif fills in a Segment.tif sitting beside it.
        The zip destination is deliberately left for the user to choose."""
        if segment_var.get():
            return
        folder = Path(result_path).parent
        candidate = next(
            (p for p in sorted(folder.glob("*.tif*"))
             if p.stem.lower() == "segment"), None)
        if candidate:
            segment_var.set(str(candidate))

    def browse_result() -> None:
        path = filedialog.askopenfilename(
            title="Select the orthomosaic (usually result.tif)",
            filetypes=tif_types)
        if path:
            result_var.set(path)
            autofill_segment(path)

    def browse_segment() -> None:
        path = filedialog.askopenfilename(
            title="Select the spray file (usually segment.tif)",
            filetypes=tif_types)
        if path:
            segment_var.set(path)

    def browse_zip() -> None:
        path = filedialog.asksaveasfilename(
            title="Save package as",
            defaultextension=".zip",
            filetypes=[("Zip archives", "*.zip")],
            initialfile="spray_package.zip")
        if path:
            zip_var.set(path)

    rows = [
        ("Orthomosaic (result.tif)", result_var, browse_result, "Browse..."),
        ("Spray File (segment.tif)", segment_var, browse_segment, "Browse..."),
        ("Output Spray Package (.zip)", zip_var, browse_zip, "Save As"),
    ]
    for row, (label, var, handler, button_label) in enumerate(rows):
        ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w",
                                          pady=4, padx=(0, 8))
        ttk.Entry(outer, textvariable=var).grid(row=row, column=1,
                                                sticky="ew", pady=4)
        ttk.Button(outer, text=button_label, command=handler).grid(
            row=row, column=2, padx=(8, 0), pady=4)

    quality_frame = ttk.Frame(outer)
    quality_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 4))
    ttk.Label(quality_frame, text="JPEG quality:").pack(side="left")
    quality_box = ttk.Spinbox(quality_frame, from_=10, to=95, increment=5,
                              width=5, textvariable=quality_var)
    quality_box.pack(side="left", padx=(6, 6))
    ttk.Label(quality_frame,
              text="(Default = 40)"
              ).pack(side="left")

    run_button = ttk.Button(outer, text="Create Package")
    run_button.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 6),
                    ipady=6)

    progress_bar = ttk.Progressbar(outer, maximum=100)
    progress_bar.grid(row=5, column=0, columnspan=3, sticky="ew")
    ttk.Label(outer, textvariable=status_var).grid(
        row=6, column=0, columnspan=3, sticky="w", pady=(4, 8))

    # CRT-flavoured activity log: phosphor green on near-black, monospace.
    families = set(tkfont.families())
    mono = next((f for f in ("Consolas", "Menlo", "Courier New", "Courier")
                 if f in families), "TkFixedFont")
    log_text = tk.Text(outer, height=13, state="disabled", wrap="word",
                       bg="#0a100a", fg="#5dff7f", insertbackground="#5dff7f",
                       selectbackground="#1f3f1f", relief="sunken",
                       borderwidth=2, font=(mono, 11), padx=8, pady=6)
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

    append_log(f"STEVTECH SPRAY PACKAGER v{__version__} -- READY")
    append_log("═" * 52)

    phase_state = {"index": 0, "count": 3, "title": "", "started": 0.0}

    def set_busy(busy: bool) -> None:
        state = "disabled" if busy else "normal"
        run_button.configure(state=state)
        quality_box.configure(state=state)

    def worker(result: str, segment: str, zip_path: str, quality: int) -> None:
        try:
            out = create_package(result, segment, zip_path, quality,
                                 reporter=QueueReporter(ui_queue))
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
            problems.append("Select the spray file (Segment.tif).")
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
        progress_bar["value"] = 0
        status_var.set("Working...")
        append_log("")
        append_log(f"JOB START {time.strftime('%Y-%m-%d %H:%M:%S')}")
        threading.Thread(target=worker,
                         args=(result, segment, zip_path, quality),
                         daemon=True).start()

    run_button.configure(command=on_run)

    def poll_queue() -> None:
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "phase":
                    index, count, title, detail = payload  # type: ignore[misc]
                    phase_state.update(index=index, count=count, title=title,
                                       started=time.monotonic())
                    append_log(f"[{index}/{count}] {title}"
                               + (f"  {detail}" if detail else ""))
                    progress_bar["value"] = (index - 1) / count * 100
                    status_var.set(f"[{index}/{count}] {title}")
                elif kind == "tick":
                    done, total, unit = payload  # type: ignore[misc]
                    frac = done / total if total else 1.0
                    index, count = phase_state["index"], phase_state["count"]
                    progress_bar["value"] = ((index - 1) + frac) / count * 100
                    elapsed = time.monotonic() - phase_state["started"]
                    eta = format_duration(elapsed * (1 - frac) / frac) \
                        if 0 < frac < 1 and elapsed > 1 else "--:--"
                    amount = (f"{format_size(done)} / {format_size(total)}"
                              if unit == "B" else f"{int(done)}/{int(total)} levels")
                    status_var.set(f"[{index}/{count}] {phase_state['title']}"
                                   f"  {frac:4.0%}  {amount}  ETA {eta}")
                elif kind == "log":
                    append_log("      " + str(payload))
                elif kind == "done":
                    progress_bar["value"] = 100
                    status_var.set("Package created.")
                    out = Path(str(payload))
                    append_log(f"JOB COMPLETE  {out.name}"
                               f"  {format_size(out.stat().st_size)}")
                    set_busy(False)
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
                    "spray file. Run with no arguments for the GUI.")
    parser.add_argument("--version", action="version",
                        version=f"Spray Packager {__version__}")
    parser.add_argument("--result", help="Path to the orthomosaic (Result.tif)")
    parser.add_argument("--segment", help="Path to the spray file (Segment.tif)")
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
        from retro_cli import run as run_cli
        sys.exit(run_cli(args))
    else:
        run_gui()


if __name__ == "__main__":
    main()
