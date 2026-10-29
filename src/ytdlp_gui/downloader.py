"""Threaded download engine built on yt-dlp's Python API.

The engine has no GUI dependencies. Worker threads mutate `Job` objects and
mark them dirty; the GUI polls `DownloadManager.drain_changes()` on a timer
and redraws only what changed. That keeps progress hooks (which fire many
times a second per download) cheap, and confines all widget access to the
GUI thread.
"""
from __future__ import annotations

import itertools
import queue
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.postprocessor import PostProcessor
from yt_dlp.utils import DownloadCancelled, PostProcessingError

from . import compat

# Best video + best audio, merged; fall back to the best single file that
# already has both; finally audio-only for sources that have no video at all.
FORMAT = "bv*+ba/b/ba"
# Highest resolution and frame rate always win. Among equal ones, prefer SDR
# and H.264/AAC, which go straight into an MP4 with no re-encoding.
FORMAT_SORT = ["res", "fps", "hdr:sdr", "vcodec:h264", "acodec:aac"]
OUTPUT_TEMPLATE = "%(title)s.%(ext)s"

QUEUED = "Queued"
RESOLVING = "Fetching info…"
DOWNLOADING = "Downloading"
PROCESSING = "Merging…"
WAITING = "Waiting to convert"
CONVERTING = "Converting to H.264"
REMUXING = "Finishing MP4…"
DONE = "Done"
FAILED = "Failed"
CANCELLED = "Cancelled"
PLAYLIST = "Playlist"

FINISHED_STATES = {DONE, FAILED, CANCELLED, PLAYLIST}


def find_ffmpeg() -> str | None:
    """A system ffmpeg+ffprobe pair is preferred (ffprobe unlocks a few extra
    yt-dlp features); otherwise use the static ffmpeg bundled by imageio-ffmpeg."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None  # yt-dlp finds them on PATH itself
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_deno() -> str | None:
    """yt-dlp needs a JavaScript runtime for YouTube; the `deno` wheel ships one."""
    try:
        import deno

        return deno.find_deno_bin()
    except Exception:
        return shutil.which("deno")


def parse_urls(text: str) -> list[str]:
    """Pull links out of pasted text: one per line, or separated by spaces/commas."""
    urls = []
    for token in re.split(r"[\s,]+", text):
        token = token.strip().strip("<>\"'")
        if not token:
            continue
        if not re.match(r"^https?://", token, re.I):
            if "." not in token:
                continue
            token = "https://" + token
        if token not in urls:
            urls.append(token)
    return urls


@dataclass(eq=False)
class Job:
    url: str
    out_dir: Path
    title: str = ""
    status: str = QUEUED
    percent: float = 0.0
    detail: str = ""
    filepath: str = ""
    error: str = ""
    id: int = field(default_factory=itertools.count(1).__next__)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_STATES

    @property
    def display_title(self) -> str:
        return self.title or self.url


class _Logger:
    """Routes yt-dlp's messages to the job instead of stdout."""

    def __init__(self, job: Job):
        self.job = job

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.job.error = re.sub(r"^ERROR:\s*", "", msg).strip()


def _fmt_bytes(n: float | None) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _BeforeDownload(PostProcessor):
    """Runs once formats are chosen but before any bytes are fetched."""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def run(self, info):
        self.callback(info)
        return [], info


class _MakeCompatible(PostProcessor):
    """Runs after merging: turns the file into an MP4 that plays anywhere."""

    def __init__(self, convert):
        super().__init__()
        self.convert = convert

    def run(self, info):
        try:
            path = self.convert(Path(info["filepath"]))
        except compat.ConversionCancelled:
            raise DownloadCancelled()
        except (OSError, RuntimeError) as e:
            raise PostProcessingError(str(e))
        info["filepath"], info["ext"] = str(path), path.suffix[1:]
        return [], info


class DownloadManager:
    def __init__(self, max_workers: int = 3):
        self._queue: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._dirty: set[int] = set()
        self._new: list[Job] = []
        self._limit = max_workers
        self._workers = 0
        self.ffmpeg = find_ffmpeg()
        self.deno = find_deno()

    # ---- public API (called from the GUI thread) -------------------------

    def set_concurrency(self, n: int):
        with self._lock:
            self._limit = max(1, n)
        self._ensure_workers()

    def submit(self, urls: list[str], out_dir: Path) -> int:
        """Queue links, skipping any that are already queued or downloading."""
        with self._lock:
            active = {j.url for j in self._jobs.values() if not j.finished}
        added = 0
        for url in urls:
            if url in active:
                continue
            self._add_job(Job(url=url, out_dir=out_dir))
            added += 1
        self._ensure_workers()
        return added

    def retry(self, job_id: int):
        job = self._jobs.get(job_id)
        if job and job.status in (FAILED, CANCELLED):
            job.cancel_event.clear()
            job.status, job.percent, job.detail, job.error = QUEUED, 0.0, "", ""
            self._touch(job)
            self._queue.put(job)
            self._ensure_workers()

    def cancel_all(self):
        for job in list(self._jobs.values()):
            if not job.finished:
                job.cancel_event.set()
                if job.status == QUEUED:
                    job.status = CANCELLED
                    self._touch(job)

    def forget(self, job_id: int):
        with self._lock:
            self._jobs.pop(job_id, None)
            self._dirty.discard(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def drain_changes(self) -> tuple[list[Job], list[Job]]:
        """Returns (new jobs, changed jobs) since the last call."""
        with self._lock:
            new, self._new = self._new, []
            changed = [self._jobs[i] for i in self._dirty if i in self._jobs]
            self._dirty.clear()
        return new, changed

    # ---- internals -------------------------------------------------------

    def _add_job(self, job: Job):
        with self._lock:
            self._jobs[job.id] = job
            self._new.append(job)
        self._queue.put(job)

    def _touch(self, job: Job):
        with self._lock:
            self._dirty.add(job.id)

    def _ensure_workers(self):
        with self._lock:
            missing = min(self._limit, self._queue.qsize() + self._busy()) - self._workers
            self._workers += max(0, missing)
        for _ in range(max(0, missing)):
            threading.Thread(target=self._worker, daemon=True).start()

    def _busy(self) -> int:
        return sum(1 for j in self._jobs.values() if not j.finished and j.status != QUEUED)

    def _worker(self):
        while True:
            with self._lock:
                if self._workers > self._limit:
                    self._workers -= 1
                    return
            try:
                job = self._queue.get(timeout=2)
            except queue.Empty:
                with self._lock:
                    # Re-check under the lock: a job put after our timeout but
                    # before this point would otherwise be stranded.
                    if not self._queue.empty():
                        continue
                    self._workers -= 1
                return
            if job.cancel_event.is_set():
                job.status = CANCELLED
                self._touch(job)
                continue
            try:
                self._run(job)
            except Exception as e:  # never let one job kill the worker
                job.status, job.error = FAILED, job.error or str(e)
                self._touch(job)

    def _options(self, job: Job, progress_hook, pp_hook) -> dict:
        opts = {
            "format": FORMAT,
            "format_sort": FORMAT_SORT,
            # Always merge into MP4 (it can hold every YouTube codec), so a
            # finished "Title.mp4" is recognised and not downloaded again.
            "merge_output_format": "mp4",
            "outtmpl": {"default": OUTPUT_TEMPLATE},
            "paths": {"home": str(job.out_dir)},
            "trim_file_name": 180,
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
            "quiet": True,
            "noprogress": True,
            "logger": _Logger(job),
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
        }
        if self.ffmpeg:
            opts["ffmpeg_location"] = self.ffmpeg
        if self.deno:
            opts["js_runtimes"] = {"deno": {"path": self.deno}}
        return opts

    def _run(self, job: Job):
        job.status, job.error, job.filepath = RESOLVING, "", ""
        self._touch(job)

        parts = {"weights": [1.0], "index": 0, "last": 0.0}

        def before_download(info):
            # A best-video + best-audio download fetches two files in turn;
            # weight each by size so the bar moves smoothly across both.
            formats = info.get("requested_formats") or [info]
            sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
            parts["weights"] = (
                [s / sum(sizes) for s in sizes] if all(sizes) else [1 / len(formats)] * len(formats)
            )
            parts["index"] = 0
            job.title = info.get("title") or job.title
            self._touch(job)

        def progress_hook(d):
            if job.cancel_event.is_set():
                raise DownloadCancelled()
            weights = parts["weights"]
            idx = min(parts["index"], len(weights) - 1)
            before = sum(weights[:idx])

            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                frac = (d.get("downloaded_bytes") or 0) / total if total else 0.0
                job.status = DOWNLOADING
                job.percent = min(100.0, (before + weights[idx] * frac) * 100)
                # Rate-limit the text work; percent updates are cheap.
                now = time.monotonic()
                if now - parts["last"] >= 0.3:
                    parts["last"] = now
                    bits = [_fmt_bytes(d.get("speed")) + "/s" if d.get("speed") else "",
                            ("ETA " + _fmt_eta(d.get("eta"))) if d.get("eta") is not None else ""]
                    if len(weights) > 1:
                        bits.insert(0, "video" if idx == 0 else "audio")
                    job.detail = "  ".join(b for b in bits if b)
                self._touch(job)
            elif d["status"] == "finished":
                parts["index"] += 1
                job.percent = min(100.0, (before + weights[idx]) * 100)
                job.detail = ""
                self._touch(job)

        def pp_hook(d):
            if d["status"] == "started" and d.get("postprocessor") in ("Merger", "FixupM3u8", "FixupM4a"):
                job.status, job.detail = PROCESSING, ""
                self._touch(job)

        status_names = {"waiting": WAITING, "converting": CONVERTING, "remuxing": REMUXING}

        def on_convert_status(name):
            job.status, job.detail = status_names[name], ""
            self._touch(job)

        def on_convert_progress(frac):
            job.detail = f"{frac * 100:.0f}%"
            self._touch(job)

        def convert(path):
            job.filepath = str(compat.make_compatible(
                self.ffmpeg or "ffmpeg", path, on_convert_status, on_convert_progress, job.cancel_event))
            return Path(job.filepath)

        with yt_dlp.YoutubeDL(self._options(job, progress_hook, pp_hook)) as ydl:
            ydl.add_post_processor(_BeforeDownload(before_download), when="before_dl")
            ydl.add_post_processor(_MakeCompatible(convert), when="post_process")
            try:
                # process=False resolves the link once without downloading, so we
                # can learn the title or expand a playlist before committing.
                ie_result = ydl.extract_info(job.url, download=False, process=False)
                if ie_result is None:
                    raise yt_dlp.utils.DownloadError(job.error or "Could not read this link")

                if ie_result.get("_type") in ("playlist", "multi_video"):
                    self._expand_playlist(job, ie_result)
                    return

                job.title = ie_result.get("title") or job.title
                self._touch(job)
                info = ydl.process_ie_result(ie_result, download=True)
            except DownloadCancelled:
                job.status, job.detail = CANCELLED, ""
                self._touch(job)
                return
            except yt_dlp.utils.DownloadError as e:
                if job.cancel_event.is_set():
                    job.status = CANCELLED
                else:
                    job.status = FAILED
                    job.error = job.error or re.sub(r"^ERROR:\s*", "", str(e))
                job.detail = ""
                self._touch(job)
                return

        downloads = (info or {}).get("requested_downloads") or [{}]
        job.filepath = job.filepath or downloads[0].get("filepath") or ""
        job.title = (info or {}).get("title") or job.title
        job.status, job.percent, job.detail = DONE, 100.0, ""
        self._touch(job)

    def _expand_playlist(self, job: Job, ie_result: dict):
        """Turn each playlist entry into its own job so they download in parallel."""
        job.title = ie_result.get("title") or job.title
        count = 0
        for entry in ie_result.get("entries") or []:
            if job.cancel_event.is_set():
                break
            if not entry:
                continue
            url = entry.get("webpage_url") or entry.get("url")
            if not url:
                continue
            if not re.match(r"^https?://", url) and entry.get("ie_key") == "Youtube":
                url = f"https://www.youtube.com/watch?v={url}"
            self._add_job(Job(url=url, out_dir=job.out_dir, title=entry.get("title") or ""))
            count += 1
            self._ensure_workers()
        job.status, job.percent = PLAYLIST, 100.0
        job.detail = f"{count} video{'s' if count != 1 else ''} queued"
        self._touch(job)
