# UX Polish: Analyst Feedback + Retro CLI — Design

**Date:** 2026-07-06
**Status:** Implemented on branch `tools-repo-refactor`

## Inputs

1. **Analyst feedback (Steven, tested v1.0):** liked Segment.tif auto-filling
   when picking Result.tif; did NOT want the zip destination auto-filled — the
   zip should be chosen manually.
2. **Jacob's direction:** delight users. Native file pickers stay; polish the
   CLI and aesthetics without hurting performance — clearly communicate
   progress, ETAs, work remaining, and clear error codes. CRT / Apple II /
   IBM 3278 / mainframe vibe.

## Changes

- **GUI:** zip auto-suggestion removed (save dialog still opens with a default
  *filename*, but location/name are the user's choice). Segment auto-fill kept.
- **Honest progress everywhere.** GDAL's `CreateCopy` has no progress
  callback, so compression now uses tile-aligned windowed copying
  (benchmarked at 1.02x of CreateCopy on a 768 MB raster — free), which also
  drops the nodata flag natively. Overviews build one level at a time (same
  result as a single gdaladdo pass, each level built from the previous) for
  per-level ticks. Zips stream in 8 MB chunks. Every phase reports real
  done/total, so ETAs are computed, not guessed.
- **Reporter protocol** (`phase/tick/log`) decouples the pipeline from
  rendering; the GUI queue adapter and the CLI renderer both consume it.
- **Retro CLI** (`retro_cli.py`, stdlib-only ANSI): green-phosphor banner box,
  `JOB START` header with input sizes, per-phase `█░` progress bars with
  spinner, percent, amounts, ETA; `═══ JOB COMPLETE ═══ ... RC=0` trailer.
  Documented return codes (0/2/3/4/5/130) with amber remediation hints,
  `SPK_DEBUG=1` for tracebacks. Degrades to plain milestone lines when stdout
  is not a TTY or `NO_COLOR` is set; `errors="replace"` guards legacy-codepage
  pipes on Windows; VT mode enabled via SetConsoleMode.
- **GUI aesthetics:** log pane restyled as a CRT terminal (phosphor green on
  near-black, monospace, job banner), native ttk pickers/controls untouched.
  Status line shows phase, percent, amounts, ETA from the same reporter.

## Testing

- Existing end-to-end assertions unchanged and passing (windowed copy and
  per-level overviews are behavior-identical: JPEG/YCbCr, tiled, overview
  levels, nodata cleared, georeferencing preserved, sprayfile untouched).
- New CLI subprocess tests: RC=0 + `JOB COMPLETE` on success, RC=2 on missing
  input, RC=3 on bad quality — these also exercise the non-TTY renderer.
- TTY rendering verified under a pseudo-terminal; GUI smoke test passing.
