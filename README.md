<div align="center">

# StevTech Tools

**Desktop utilities for working with [StevTech](https://stevtech.com.au) services.**

[![Build Spray Packager](https://github.com/StevTech-Pty-Ltd/tools/actions/workflows/build-spray-packager.yml/badge.svg)](https://github.com/StevTech-Pty-Ltd/tools/actions/workflows/build-spray-packager.yml)

</div>

---

## Tools

| Tool | Description |
| --- | --- |
| [**Spray Packager**](tools/spray-packager) | Packages DJI Terra spray-drone output (`Result.tif` + `Segment.tif`) into a single optimized zip, ready to send to StevTech. |

## Installation

Download the tool's `.exe` from the [latest release](../../releases/latest) and
double-click it. No installer, no dependencies.

> [!NOTE]
> Windows SmartScreen may warn on first run — the binaries are not yet
> code-signed. Choose **More info → Run anyway**.

## Development

Each tool is self-contained under `tools/<name>/` with its own README, tests,
and build script.

```bash
cd tools/spray-packager
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python spray_packager.py
```

Releases are built by CI — push a `<tool>-vX.Y.Z` tag and the workflow
attaches the binary to a [GitHub Release](../../releases).

## Support

Contact your StevTech representative, or [open an issue](../../issues).

## License

Free to use with StevTech products and services — see [LICENSE.md](LICENSE.md).
