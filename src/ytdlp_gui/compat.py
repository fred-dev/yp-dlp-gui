"""Make every download a plain MP4 (H.264 video + AAC audio) that plays
anywhere — PowerPoint, Keynote, QuickTime, Windows Media Player, phones.

Streams that are already compatible are copied bit-for-bit. Only a stream
that isn't (e.g. YouTube's 4K, which only exists as VP9/AV1) is re-encoded,
at high quality, using a hardware H.264 encoder when the machine has one.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# H.264 in 8-bit 4:2:0 is the only video every player decodes; High 10 or
# 4:4:4 H.264 fails in PowerPoint and QuickTime.
COMPATIBLE_PIX_FMTS = {"yuv420p", "yuvj420p", "nv12"}
COMPATIBLE_AUDIO = {"aac"}
AUDIO_BITRATE = "256k"

# Re-encoding uses every core (or the one hardware encoder); running several
# at once only makes each slower, so conversions take turns.
_convert_lock = threading.Semaphore(1)
_encoder_lock = threading.Lock()
_encoder_cache: dict[str, list[str]] = {}

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class ConversionCancelled(Exception):
    pass


@dataclass
class Probe:
    duration: float = 0.0
    bitrate_kbps: int = 0
    video_codec: str | None = None
    pix_fmt: str | None = None
    height: int = 0
    audio_codec: str | None = None

    @property
    def video_ok(self) -> bool:
        return self.video_codec is None or (self.video_codec == "h264" and self.pix_fmt in COMPATIBLE_PIX_FMTS)

    @property
    def audio_ok(self) -> bool:
        return self.audio_codec is None or self.audio_codec in COMPATIBLE_AUDIO


def probe(ffmpeg: str, path: Path) -> Probe:
    """Read stream info from `ffmpeg -i` (the bundled ffmpeg has no ffprobe)."""
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace", **_NO_WINDOW
    ).stderr
    p = Probe()
    if m := re.search(r"Duration: (\d+):(\d+):([\d.]+)", out):
        p.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    if m := re.search(r"bitrate: (\d+) kb/s", out):
        p.bitrate_kbps = int(m[1])
    for line in out.splitlines():
        m = re.search(r"Stream #\d+:\d+.*?: (Video|Audio): (\w+)(.*)", line)
        if not m or "(attached pic)" in line:
            continue
        kind, codec, rest = m.groups()
        if kind == "Video" and p.video_codec is None:
            p.video_codec = codec
            if fm := re.match(r"[^,]*,\s*(\w+)", rest):
                p.pix_fmt = fm[1]
            if rm := re.search(r"\b\d{2,5}x(\d{2,5})\b", rest):
                p.height = int(rm[1])
        elif kind == "Audio" and p.audio_codec is None:
            p.audio_codec = codec
    return p


def _encoder_candidates() -> list[list[str]]:
    """Fastest first; each is tried once and the first that works is cached."""
    x264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    if sys.platform == "darwin":
        return [["-c:v", "h264_videotoolbox", "-allow_sw", "0"], x264]
    hw = [
        ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0"],
        ["-c:v", "h264_qsv", "-global_quality", "20"],
    ]
    if sys.platform == "win32":
        hw.append(["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "20"])
    return hw + [x264]


def _pick_encoder(ffmpeg: str) -> list[str]:
    with _encoder_lock:
        if ffmpeg not in _encoder_cache:
            for args in _encoder_candidates():
                test = [ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        "color=black:s=640x360:d=0.2", *args, "-pix_fmt", "yuv420p", "-f", "null", "-"]
                if subprocess.run(test, capture_output=True, **_NO_WINDOW).returncode == 0:
                    _encoder_cache[ffmpeg] = args
                    break
            else:
                _encoder_cache[ffmpeg] = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
        return _encoder_cache[ffmpeg]


def _video_args(ffmpeg: str, p: Probe) -> list[str]:
    args = list(_pick_encoder(ffmpeg))
    if args[1] == "h264_videotoolbox":
        # VideoToolbox has no constant-quality mode on Intel Macs, so give it
        # a generous bitrate: H.264 needs ~1.5x what VP9/AV1 use for the same
        # picture, with a floor per resolution so nothing comes out soft.
        floor = 45000 if p.height > 1440 else 20000 if p.height > 1080 else 10000 if p.height > 720 else 5000
        kbps = min(100000, max(floor, int(p.bitrate_kbps * 1.6)))
        args += ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
    return args + ["-profile:v", "high", "-pix_fmt", "yuv420p", "-tag:v", "avc1"]


def make_compatible(ffmpeg: str, src: Path, on_status=None, on_progress=None, cancel_event=None) -> Path:
    """Return the path of a compatible MP4/M4A for `src`, converting only if needed.

    `on_status` receives "waiting", "converting" (re-encoding) or "remuxing"
    (streams copied as-is). `src` is removed if a new file replaces it.
    """
    p = probe(ffmpeg, src)
    audio_only = p.video_codec is None
    ext = ".m4a" if audio_only else ".mp4"
    if src.suffix.lower() == ext and p.video_ok and p.audio_ok:
        return src

    dest = src.with_suffix(ext)
    tmp = src.with_name(src.stem + ".converting" + ext)
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src)]
    if not audio_only:
        cmd += ["-map", "0:v:0"]
    if p.audio_codec:
        cmd += ["-map", "0:a:0"]
    if not audio_only:
        cmd += ["-c:v", "copy"] if p.video_ok else _video_args(ffmpeg, p)
    if p.audio_codec:
        cmd += ["-c:a", "copy"] if p.audio_ok else ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    cmd += ["-map_metadata", "0", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(tmp)]

    reencoding = not p.video_ok
    if reencoding and not _convert_lock.acquire(blocking=False):
        if on_status:
            on_status("waiting")
        while not _convert_lock.acquire(timeout=0.5):
            if cancel_event is not None and cancel_event.is_set():
                raise ConversionCancelled()
    try:
        if on_status:
            on_status("converting" if reencoding else "remuxing")
        _run(cmd, p.duration, on_progress, cancel_event, tmp)
    finally:
        if reencoding:
            _convert_lock.release()

    os.replace(tmp, dest)
    if dest != src:
        src.unlink(missing_ok=True)
    return dest


def _run(cmd, duration, on_progress, cancel_event, tmp: Path):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace", **_NO_WINDOW)
    err_lines = []
    stderr_reader = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    stderr_reader.start()
    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                raise ConversionCancelled()
            if on_progress and duration and line.startswith("out_time_us="):
                try:
                    on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration))
                except ValueError:
                    pass
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        tmp.unlink(missing_ok=True)
        raise
    stderr_reader.join(timeout=2)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Conversion failed: " + ("".join(err_lines).strip().splitlines() or ["unknown error"])[-1])
