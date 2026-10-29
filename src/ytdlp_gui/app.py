"""Native-widget desktop front end for the yt-dlp download engine.

Run from source:   uv run ytdlp-gui
Package as an app: ./scripts/build.sh   (or scripts\\build.ps1 on Windows)
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

from .downloader import (
    CANCELLED, CONVERTING, DONE, FAILED, FINISHED_STATES, PLAYLIST, REMUXING, WAITING, DownloadManager, parse_urls,
)

REFRESH_SECONDS = 0.25
MAX_CONCURRENCY = 8


def open_path(path: Path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


class YtDlpGuiApp(toga.App):
    def startup(self):
        self.settings_file = self.paths.config / "settings.json"
        settings = self._load_settings()
        self.out_dir = Path(settings.get("out_dir") or Path.home() / "Downloads")
        concurrency = int(settings.get("concurrency", 3))

        self.manager = DownloadManager(max_workers=concurrency)
        self.rows = {}  # job id -> table row

        self.links_input = toga.MultilineTextInput(
            placeholder="Paste one or more links, one per line (videos or playlists)",
            style=Pack(height=110, margin_bottom=8),
        )

        self.folder_label = toga.Label(str(self.out_dir), style=Pack(flex=1, margin_right=8))
        folder_row = toga.Box(
            children=[
                toga.Label("Save to:", style=Pack(width=70)),
                self.folder_label,
                toga.Button("Choose…", on_press=self.on_choose_folder),
                toga.Button("Open", on_press=lambda w: open_path(self.out_dir), style=Pack(margin_left=5)),
            ],
            style=Pack(direction=ROW, align_items="center", margin_bottom=8),
        )

        self.concurrency_input = toga.NumberInput(
            min=1, max=MAX_CONCURRENCY, step=1, value=concurrency,
            on_change=self.on_concurrency_change, style=Pack(width=70),
        )
        self.download_button = toga.Button("Download", on_press=self.on_download, style=Pack(width=130))
        action_row = toga.Box(
            children=[
                self.download_button,
                toga.Button("Stop All", on_press=self.on_stop, style=Pack(margin_left=5)),
                toga.Button("Retry Failed", on_press=self.on_retry, style=Pack(margin_left=5)),
                toga.Button("Clear Finished", on_press=self.on_clear, style=Pack(margin_left=5)),
                toga.Box(style=Pack(flex=1)),
                toga.Label("Simultaneous:", style=Pack(margin_right=5)),
                self.concurrency_input,
            ],
            style=Pack(direction=ROW, align_items="center", margin_bottom=8),
        )

        self.table = toga.Table(
            columns=["Title", "Status", "Progress"],
            data=[],
            on_activate=self.on_row_activate,
            style=Pack(flex=1, margin_bottom=8),
        )

        self.overall_bar = toga.ProgressBar(max=100, value=0, style=Pack(flex=1, margin_right=8))
        self.summary_label = toga.Label("Ready", style=Pack(width=260))
        footer = toga.Box(
            children=[self.overall_bar, self.summary_label],
            style=Pack(direction=ROW, align_items="center"),
        )

        self.main_window = toga.MainWindow(title=self.formal_name, size=(820, 560))
        self.main_window.content = toga.Box(
            children=[self.links_input, folder_row, action_row, self.table, footer],
            style=Pack(direction=COLUMN, margin=12),
        )
        self.main_window.show()

        self.on_exit = self.handle_exit

    # ---- settings --------------------------------------------------------

    def _load_settings(self) -> dict:
        try:
            return json.loads(self.settings_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(json.dumps({
                "out_dir": str(self.out_dir),
                "concurrency": self._concurrency(),
            }))
        except OSError:
            pass

    def _concurrency(self) -> int:
        try:
            return max(1, min(MAX_CONCURRENCY, int(self.concurrency_input.value)))
        except (TypeError, ValueError):
            return 3

    # ---- handlers --------------------------------------------------------

    async def on_choose_folder(self, widget):
        path = await self.main_window.dialog(
            toga.SelectFolderDialog("Choose where to save downloads", initial_directory=self.out_dir)
        )
        if path:
            self.out_dir = Path(path)
            self.folder_label.text = str(self.out_dir)
            self._save_settings()

    def on_concurrency_change(self, widget):
        self.manager.set_concurrency(self._concurrency())
        self._save_settings()

    async def on_download(self, widget):
        urls = parse_urls(self.links_input.value)
        if not urls:
            await self.main_window.dialog(
                toga.InfoDialog("No links", "Paste at least one video or playlist link first.")
            )
            return
        if not self.out_dir.is_dir():
            await self.main_window.dialog(
                toga.ErrorDialog("Folder missing", f"The save folder doesn't exist:\n{self.out_dir}")
            )
            return
        self.manager.submit(urls, self.out_dir)
        self.links_input.value = ""

    def on_stop(self, widget):
        self.manager.cancel_all()

    def on_retry(self, widget):
        for job in self.manager.jobs():
            if job.status in (FAILED, CANCELLED):
                self.manager.retry(job.id)

    def on_clear(self, widget):
        for job in self.manager.jobs():
            if job.status in FINISHED_STATES:
                row = self.rows.pop(job.id, None)
                if row is not None:
                    self.table.data.remove(row)
                self.manager.forget(job.id)

    async def on_row_activate(self, widget, row, **kwargs):
        job = next((j for j in self.manager.jobs() if self.rows.get(j.id) is row), None)
        if job is None:
            return
        if job.status == DONE and job.filepath and Path(job.filepath).exists():
            open_path(Path(job.filepath).parent)
        elif job.status == FAILED and job.error:
            await self.main_window.dialog(toga.ErrorDialog("Download failed", f"{job.display_title}\n\n{job.error}"))

    def handle_exit(self, app, **kwargs):
        self.manager.cancel_all()
        return True

    # ---- table refresh ---------------------------------------------------

    async def on_running(self):
        """All widget updates happen here, batched, on the GUI thread."""
        while True:
            new, changed = self.manager.drain_changes()
            for job in new:
                self.rows[job.id] = self.table.data.append(self._row_values(job))
            for job in changed:
                row = self.rows.get(job.id)
                if row is None:
                    continue
                for key, value in self._row_values(job).items():
                    if getattr(row, key) != value:
                        setattr(row, key, value)
            if new or changed:
                self._update_summary()
            await asyncio.sleep(REFRESH_SECONDS)

    @staticmethod
    def _row_values(job) -> dict:
        if job.status == FAILED:
            progress = job.error.splitlines()[0][:120] if job.error else ""
        elif job.status in (DONE, PLAYLIST, CANCELLED, WAITING, CONVERTING, REMUXING):
            progress = job.detail
        else:
            progress = f"{job.percent:5.1f}%  {job.detail}".rstrip() if job.percent or job.detail else ""
        return {"title": job.display_title, "status": job.status, "progress": progress}

    def _update_summary(self):
        jobs = [j for j in self.manager.jobs() if j.status != PLAYLIST]
        if not jobs:
            self.overall_bar.value = 0
            self.summary_label.text = "Ready"
            return
        counts = {}
        for j in jobs:
            counts[j.status] = counts.get(j.status, 0) + 1
        active = len(jobs) - sum(counts.get(s, 0) for s in (DONE, FAILED, CANCELLED))
        # Failed/cancelled jobs drop out of the bar so it can still reach 100%.
        counted = [j for j in jobs if j.status not in (FAILED, CANCELLED)]
        self.overall_bar.value = (
            sum(100 if j.status == DONE else j.percent for j in counted) / len(counted) if counted else 0
        )
        parts = []
        if active:
            parts.append(f"{active} active")
        for status in (DONE, FAILED, CANCELLED):
            if counts.get(status):
                parts.append(f"{counts[status]} {status.lower()}")
        self.summary_label.text = " · ".join(parts)


def main():
    return YtDlpGuiApp("YT-DLP GUI", "com.frederickrodrigues.ytdlp_gui")


def run():
    main().main_loop()
