# YT-DLP GUI

A small desktop app for downloading videos with [yt-dlp](https://pypi.org/project/yt-dlp/).
It uses native widgets on every platform through [Toga](https://beeware.org/project/projects/libraries/toga/): Cocoa on macOS, WinForms on Windows and GTK on Linux.

- Paste one link or many, one per line. Playlist links are split into one download per video.
- Always downloads the **highest resolution video merged with the best audio** (`bv*+ba/b`).
- **Every file is an MP4 with H.264 video and AAC audio**, which plays in PowerPoint, Keynote, QuickTime, Windows and phones (audio-only sources become `.m4a`):
  - At the top resolution, formats that are already H.264/AAC (and SDR rather than HDR) are preferred. They go straight into the MP4 with **no re-encoding**. On YouTube that covers everything up to 1080p.
  - If the best version only exists in another codec (YouTube's 1440p/4K are VP9/AV1 only; Opus audio), only that stream is re-encoded, at high quality, so the resolution is kept. A hardware H.264 encoder is used when the machine has one (Apple VideoToolbox; NVIDIA, Intel or AMD on Windows), otherwise libx264 at CRF 18. Conversions run one at a time so they don't fight over the encoder.
  - The MP4s are written with `faststart` (index at the front), which suits presentations and streaming.
- Files are named after the video title (`%(title)s.%(ext)s`) and saved to the folder you pick. The app remembers that folder.
- Runs several downloads at once (1–8, set with *Simultaneous*). Each download also fetches fragments in parallel.
- Ships its own **ffmpeg** (via `imageio-ffmpeg`) for merging and its own **deno** JavaScript runtime, which yt-dlp now needs for YouTube. Nothing else needs to be installed.

Double-click a finished row to open its folder. Double-click a failed row to see the full error.

## Run from source

Requires [uv](https://docs.astral.sh/uv/). uv installs the Python version pinned in `.python-version` (3.13) by itself.

```bash
uv run ytdlp-gui
```

On Linux, GTK needs system packages first. On Debian/Ubuntu:
`sudo apt install libgirepository-2.0-dev libcairo2-dev libpango1.0-dev gir1.2-gtk-3.0 pkg-config`.

## Build a packaged app

Run the build script on the platform you want to build for. Packages land in `dist/`.

| Platform | Command | Output |
|---|---|---|
| macOS | `./scripts/build.sh` | Universal `.dmg` (Apple Silicon **and** Intel in one app) |
| Windows | `scripts\build.bat` (or `scripts\build.ps1`) | `.msi` installer (`-Format zip` for a portable zip) |
| Linux | `./scripts/build.sh` | `.deb` / `.rpm` for the host distro (`LINUX_FORMAT=flatpak` or `appimage` for others) |

Each build re-resolves requirements, so it always ships the newest yt-dlp. Sites change often, so rebuild now and then.

### macOS signing

By default the macOS build is ad-hoc signed, so it only runs on the machine that built it.
To distribute it, sign with a Developer ID certificate:

```bash
./scripts/build.sh --identity "Developer ID Application: Your Name (TEAMID)"
```

`universal_build = true` in `pyproject.toml` merges the arm64 and x86_64 wheels into one fat binary.
For one architecture only, set it to `false` and build on that machine (or under `arch -x86_64` with Rosetta).

## Layout

```
src/ytdlp_gui/downloader.py   threaded yt-dlp engine (no GUI code)
src/ytdlp_gui/compat.py       makes each file a compatible H.264/AAC MP4, copying streams where possible
src/ytdlp_gui/app.py          Toga user interface
scripts/                      build scripts per platform
pyproject.toml                uv project + Briefcase packaging config
```
