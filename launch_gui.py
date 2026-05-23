"""FFXIV Completion Tracker - PyQt6 desktop launcher.

Replaces the interactive text menu in launch.py with a GUI that:
  * shows a live server status indicator (running / not running / error)
  * starts uvicorn as a managed QProcess (no second console window)
  * opens the browser to the bound host:port
  * lets the user point at any workbook via a file picker for ingest
  * exposes every action that the CLI menu had (status, backup, clean
    probe artifacts, set bind IP, reinstall deps, open data folders,
    Discord invite)
  * checks GitHub releases for updates and offers to download the installer

CLI fallback is preserved: `python launch.py --cli` still renders the
original menu.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import TextIO

# Embedded Python (Inno Setup installer) uses a python313._pth file that does
# NOT add the script's directory to sys.path, so `from launch import ...`
# below fails with ModuleNotFoundError and pythonw.exe swallows the trace.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from PyQt6.QtCore import (
    QObject,
    QProcess,
    QProcessEnvironment,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QCloseEvent, QColor, QFont, QIcon, QPaintEvent, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

# Reuse helpers + path constants from the CLI launcher so the two stay in sync.
from launch import (
    BACKUP_DIR,
    DATA_DIR,
    DB_PATH,
    DISCORD_INVITE_URL,
    IS_EMBEDDED,
    PORT,
    PROBE_DIR,
    PROGRESS_DIR,
    REQUIREMENTS,
    ROOT,
    SPREADSHEET_DIR,
    VENV_PY,
    browser_host,
    current_host,
    detect_lan_ip,
    dir_size,
    fmt_bytes,
    load_config,
    port_in_use,
    save_config,
)

from _version import __version__ as APP_VERSION
import updater

ICON_PATH = ROOT / "assets" / "icon.png"


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class UpdateCheckWorker(QObject):
    """Runs updater.check_for_update() on a QThread so the UI stays responsive."""
    finished = pyqtSignal(object)  # updater.UpdateCheckResult

    def run(self) -> None:
        self.finished.emit(updater.check_for_update())


class DownloadWorker(QObject):
    progress = pyqtSignal(int, object)  # done, total (int or None)
    finished = pyqtSignal(str, str)     # path, error  (one of them empty)

    def __init__(self, url: str, dest: Path) -> None:
        super().__init__()
        self.url = url
        self.dest = dest

    def run(self) -> None:
        try:
            path = updater.download_installer(
                self.url, self.dest,
                progress=lambda d, t: self.progress.emit(d, t),
            )
            self.finished.emit(str(path), "")
        except Exception as e:  # surface to the UI thread
            self.finished.emit("", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Status indicator
# ---------------------------------------------------------------------------

class StatusDot(QLabel):
    """Small colored circle used as the server status indicator."""

    COLORS = {
        "running": QColor(46, 204, 113),   # green
        "stopped": QColor(149, 165, 166),  # gray
        "error":   QColor(231, 76, 60),    # red
        "starting": QColor(241, 196, 15),  # amber
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "stopped"
        self.setFixedSize(18, 18)

    def set_state(self, state: str) -> None:
        if state not in self.COLORS:
            state = "stopped"
        self._state = state
        self.update()

    def paintEvent(self, a0: QPaintEvent | None) -> None:
        del a0  # we don't use the event payload
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.COLORS[self._state]
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 14, 14)


# ---------------------------------------------------------------------------
# Bind IP dialog
# ---------------------------------------------------------------------------

class BindIpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set bind IP")
        self.setMinimumWidth(420)

        current = current_host()
        detected = detect_lan_ip()

        layout = QVBoxLayout(self)

        info = QLabel(
            f"<b>Current:</b> {current}<br>"
            f"<b>Detected LAN IP:</b> {detected or '(none found)'}"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        self.combo = QComboBox()
        self.combo.addItem("Loopback only (127.0.0.1) - safest, this machine only", "127.0.0.1")
        self.combo.addItem("All interfaces (0.0.0.0) - reachable from LAN", "0.0.0.0")
        if detected:
            self.combo.addItem(f"Detected LAN IP ({detected}) - bind to this interface", detected)
        self.combo.addItem("Custom IP...", "__custom__")

        # Preselect the current value if it matches a preset.
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == current:
                self.combo.setCurrentIndex(i)
                break

        layout.addWidget(self.combo)

        self.custom = QLineEdit()
        self.custom.setPlaceholderText("e.g. 192.168.1.42")
        self.custom.setEnabled(False)
        layout.addWidget(self.custom)

        self.combo.currentIndexChanged.connect(self._on_combo_change)

        warn = QLabel(
            "Reminder: the app has no auth. Only bind beyond loopback on networks you trust."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #c0392b;")
        layout.addWidget(warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_combo_change(self, _idx: int) -> None:
        is_custom = self.combo.currentData() == "__custom__"
        self.custom.setEnabled(is_custom)
        if is_custom:
            self.custom.setFocus()

    def selected_host(self) -> str | None:
        data = self.combo.currentData()
        if data == "__custom__":
            ip = self.custom.text().strip()
            try:
                socket.inet_aton(ip)
            except OSError:
                return None
            return ip
        return data


# ---------------------------------------------------------------------------
# Update dialog
# ---------------------------------------------------------------------------

class InstructionsDialog(QDialog):
    """Step-by-step walkthrough: get the workbook, ingest it, scrape and import
    a character from Lodestone. We dynamically include a clickable link to
    the running server when there is one, so the user can jump straight to
    the right page instead of typing the URL."""

    def __init__(self, server_url: str | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Instructions")
        self.resize(720, 620)

        layout = QVBoxLayout(self)

        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setHtml(self._build_html(server_url))
        layout.addWidget(view)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        layout.addWidget(btns)

    def _build_html(self, server_url: str | None) -> str:
        if server_url:
            probe_link = (
                f'<a href="{server_url}/lodestone-probe">{server_url}/lodestone-probe</a>'
            )
            chars_link = (
                f'<a href="{server_url}/characters">{server_url}/characters</a>'
            )
            server_block = (
                f'<p>Server is running at <a href="{server_url}">{server_url}</a>. '
                "The links below open in your browser.</p>"
            )
        else:
            probe_link = "<code>/lodestone-probe</code>"
            chars_link = "<code>/characters</code>"
            server_block = (
                "<p><b>Tip:</b> Start the server with the <b>Start server</b> button "
                "first - the Lodestone and import workflows are web pages, so they "
                "need the server running.</p>"
            )

        return f"""
<style>
  body {{ font-family: Segoe UI, sans-serif; }}
  h2 {{ margin-top: 18px; }}
  ol li {{ margin-bottom: 6px; }}
  code {{ background: #2c3e50; color: #ecf0f1; padding: 1px 4px; border-radius: 3px; }}
</style>

<h1>Quick start</h1>
{server_block}

<h2>1. Get the official workbook</h2>
<ol>
  <li>Click <b>Open Discord</b> in the top right of this window to join the
      FFXIV Completionist community.</li>
  <li>In the server, open the <code>#download</code> channel and grab the
      latest <code>.xlsx</code> spreadsheet. Save it anywhere on your PC.</li>
</ol>

<h2>2. Ingest the workbook</h2>
<ol>
  <li>Click <b>Ingest workbook (.xlsx → SQLite)…</b> in the Workbook / Data
      box on the left.</li>
  <li>Pick the <code>.xlsx</code> you downloaded. The Log pane at the bottom
      streams progress in real time.</li>
  <li>When it finishes, the <b>Database</b> dot turns green and the
      <b>Characters</b> panel will refresh.</li>
</ol>

<h2>3. Start the server and create a character</h2>
<ol>
  <li>Click <b>Start server</b> at the top. The status dot goes green and
      your browser opens automatically.</li>
  <li>In the browser, open {chars_link}, then create a new character by name
      and (optionally) pick a starting class.</li>
</ol>

<h2>4. Scrape your Lodestone progress</h2>
<ol>
  <li>Open {probe_link} in your browser.</li>
  <li>Paste your Lodestone character URL (looks like
      <code>https://na.finalfantasyxiv.com/lodestone/character/12345678/</code>)
      and save it.</li>
  <li>Click <b>Open in new tab</b> and <b>sign in to Lodestone</b> in your
      chosen browser (Edge, Chrome, or Firefox). The probe will read cookies
      from that browser, so the sign-in must be in the same one you pick on
      the probe page.</li>
  <li>Back on the probe page, pick the cookie source browser, leave
      <b>Include standard authenticated pages</b> checked, and click
      <b>Run authenticated scrape</b>. A status panel polls until the run
      reports <code>completed</code> and shows the payload path.</li>
</ol>

<h2>5. Import the scrape into a character</h2>
<ol>
  <li>Open {chars_link}.</li>
  <li>Pick the target character.</li>
  <li>Either <b>upload</b> the payload JSON the probe just produced, or
      select it from the server-side dropdown (it reads
      <code>data/lodestone_probe/*.json</code>).</li>
  <li>Optionally tick <b>Clear existing character progress before import</b>
      to start from a blank slate.</li>
  <li>Click <b>Start import</b>. The monitor polls until done. When it
      finishes, an <b>Open unmatched items</b> link surfaces anything the
      matcher could not place - review those manually.</li>
</ol>

<h2>If something goes wrong</h2>
<ul>
  <li><b>Log pane looks idle</b>: be patient. Some steps (workbook ingest,
      first server startup, Lodestone scrape) genuinely have quiet stretches
      where the child process is busy but not printing. Give it a minute or
      two before assuming anything is stuck.</li>
  <li><b>Probe never finishes</b>: the cookie source browser probably is
      not the one you actually signed in with. Pick the right one and re-run.</li>
  <li><b>Import shows lots of unmatched items</b>: open the
      <b>Open unmatched items</b> report, copy the names you care about, and
      tick them by hand on the relevant sheets. If a name is consistently
      mismatched (e.g. a Lodestone label that should clearly map to a
      workbook row), please
      <a href="https://github.com/JEschete/FFXIV_Completionist_Browser_App/issues/new">open
      an issue on the repo</a> with the unmatched name and the row it should
      match so the matcher can be corrected.</li>
  <li><b>Clean stale probe artifacts</b>: in this window, click
      <b>Clean Lodestone probe artifacts…</b> to free disk space without
      touching your sidecar progress files.</li>
</ul>
"""


class UpdateDialog(QDialog):
    def __init__(self, check_result: updater.UpdateCheckResult, parent=None) -> None:
        super().__init__(parent)
        # Avoid shadowing QDialog.result() - keep the data on a different attr.
        self._check = check_result
        self.download_btn: QPushButton | None = None
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status_lbl = QLabel("")

        self.setWindowTitle("Check for updates")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Installed:</b> {check_result.current}"))

        if check_result.error:
            layout.addWidget(QLabel(f"<b>Error:</b> {check_result.error}"))
        elif check_result.latest is None:
            layout.addWidget(QLabel("No release information available."))
        else:
            rel = check_result.latest
            layout.addWidget(QLabel(f"<b>Latest release:</b> {rel.tag} - {rel.name}"))
            if check_result.is_newer:
                layout.addWidget(QLabel("<b style='color:#27ae60;'>An update is available.</b>"))
            else:
                layout.addWidget(QLabel("You are on the latest version."))

            notes = QPlainTextEdit()
            notes.setReadOnly(True)
            notes.setPlainText(rel.body or "(no release notes)")
            notes.setMinimumHeight(180)
            layout.addWidget(notes)

            layout.addWidget(self.progress)
            layout.addWidget(self.status_lbl)

        btns = QDialogButtonBox()
        open_page_btn = btns.addButton("Open release page", QDialogButtonBox.ButtonRole.ActionRole)
        if open_page_btn is not None:
            open_page_btn.clicked.connect(self._open_page)

        if check_result.latest and check_result.is_newer and check_result.latest.installer_url:
            self.download_btn = btns.addButton("Download installer", QDialogButtonBox.ButtonRole.ActionRole)
            if self.download_btn is not None:
                self.download_btn.clicked.connect(self._download)
        close = btns.addButton(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.clicked.connect(self.reject)
        layout.addWidget(btns)

    def _open_page(self) -> None:
        url = self._check.latest.html_url if self._check.latest else updater.RELEASES_PAGE
        webbrowser.open(url)

    def _download(self) -> None:
        rel = self._check.latest
        if not rel or not rel.installer_url or not rel.installer_name:
            return
        dest = Path(tempfile.gettempdir()) / rel.installer_name
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # indeterminate until we get a total
        self.status_lbl.setText(f"Downloading to {dest} ...")
        if self.download_btn is not None:
            self.download_btn.setEnabled(False)

        self._thread = QThread(self)
        self._worker = DownloadWorker(rel.installer_url, dest)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_progress(self, done: int, total) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            # Stay indeterminate.
            self.status_lbl.setText(f"Downloaded {fmt_bytes(done)} ...")

    def _on_done(self, path: str, error: str) -> None:
        if error:
            self.status_lbl.setText(f"Failed: {error}")
            if self.download_btn is not None:
                self.download_btn.setEnabled(True)
            return
        self.status_lbl.setText(f"Saved: {path}")
        reply = QMessageBox.question(
            self, "Run installer?",
            f"Installer downloaded to:\n{path}\n\nRun it now?\n\n"
            "The launcher will close before installation starts.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.startfile(path)  # type: ignore[attr-defined]
                self.status_lbl.setText(f"Saved: {path}  |  Installer launched")
                # Defer shutdown to avoid re-entrancy while this slot is running.
                QTimer.singleShot(0, self._request_launcher_shutdown)
            except OSError as e:
                QMessageBox.warning(self, "Could not run installer", str(e))

    def _request_launcher_shutdown(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "request_quit_for_update"):
            getattr(parent, "request_quit_for_update")()
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    POLL_MS = 800

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"FFXIV Completion Tracker - {APP_VERSION}")
        self.resize(1480, 900)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        # Launcher settings shared with launch.py (.launch.json).
        self._cfg = load_config()
        self._ingest_mem_log_enabled = bool(self._cfg.get("ingest_mem_log", False))

        self._server: QProcess | None = None
        self._server_error = False  # sticky error flag until next start
        self._user_stopping = False  # set while a user-initiated stop is in flight
        self._closing_for_update = False

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        root.addWidget(self._build_header())
        root.addWidget(self._build_server_box())

        actions_row = QHBoxLayout()
        actions_row.addWidget(self._build_data_box(), 2)
        actions_row.addWidget(self._build_chars_box(), 2)
        actions_row.addWidget(self._build_settings_box(), 1)
        root.addLayout(actions_row)

        root.addWidget(self._build_log_box(), 1)

        # Poll the port to keep the indicator honest even if the server was
        # started or stopped outside this process.
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._refresh_status)
        self._poll.timeout.connect(self._refresh_db_status)
        self._poll.start()
        self._refresh_status()
        self._refresh_db_status()
        self._refresh_characters()

    # -- builders --------------------------------------------------------

    def _build_header(self) -> QWidget:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(box)

        if ICON_PATH.exists():
            icon_lbl = QLabel()
            pix = QPixmap(str(ICON_PATH)).scaled(
                48, 48,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_lbl.setPixmap(pix)
            icon_lbl.setFixedSize(48, 48)
            layout.addWidget(icon_lbl)

        title = QLabel(f"<b>FFXIV Completion Tracker</b>  <span style='color:#7f8c8d;'>{APP_VERSION}</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setFont(QFont(self.font().family(), 14))
        layout.addWidget(title)

        layout.addStretch(1)

        instr_btn = QPushButton("Instructions")
        instr_btn.clicked.connect(self.show_instructions)
        layout.addWidget(instr_btn)

        update_btn = QPushButton("Check for updates")
        update_btn.clicked.connect(self.check_for_updates)
        layout.addWidget(update_btn)

        discord_btn = QPushButton("Open Discord")
        discord_btn.clicked.connect(self.open_discord)
        layout.addWidget(discord_btn)

        return box

    def _build_server_box(self) -> QGroupBox:
        box = QGroupBox("Server")
        layout = QGridLayout(box)

        self.dot = StatusDot()
        layout.addWidget(self.dot, 0, 0)
        self.status_lbl = QLabel("Not running")
        f = self.status_lbl.font()
        f.setBold(True)
        self.status_lbl.setFont(f)
        layout.addWidget(self.status_lbl, 0, 1)

        self.url_lbl = QLabel("")
        self.url_lbl.setStyleSheet("color: #2980b9;")
        layout.addWidget(self.url_lbl, 0, 2)
        layout.setColumnStretch(2, 1)

        self.bind_lbl = QLabel("")
        self.bind_lbl.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(self.bind_lbl, 1, 1, 1, 2)

        self.start_btn = QPushButton("Start server")
        self.start_btn.clicked.connect(self.start_server)
        layout.addWidget(self.start_btn, 0, 3)

        self.stop_btn = QPushButton("Stop server")
        self.stop_btn.clicked.connect(self.stop_server)
        layout.addWidget(self.stop_btn, 0, 4)

        self.browser_btn = QPushButton("Launch browser")
        self.browser_btn.clicked.connect(self.launch_browser)
        layout.addWidget(self.browser_btn, 0, 5)

        return box

    def _build_data_box(self) -> QGroupBox:
        box = QGroupBox("Workbook / Data")
        layout = QVBoxLayout(box)

        # DB status row - sibling indicator to the server dot at the top.
        status_row = QHBoxLayout()
        self.db_dot = StatusDot()
        status_row.addWidget(self.db_dot)
        self.db_status_lbl = QLabel("Database: checking…")
        f = self.db_status_lbl.font()
        f.setBold(True)
        self.db_status_lbl.setFont(f)
        status_row.addWidget(self.db_status_lbl)
        status_row.addStretch(1)
        layout.addLayout(status_row)
        self.db_detail_lbl = QLabel("")
        self.db_detail_lbl.setStyleSheet("color: #7f8c8d;")
        self.db_detail_lbl.setWordWrap(True)
        layout.addWidget(self.db_detail_lbl)

        ingest_btn = QPushButton("Ingest workbook (.xlsx → SQLite)…")
        ingest_btn.clicked.connect(self.ingest_workbook)
        layout.addWidget(ingest_btn)

        status_btn = QPushButton("Show status / health")
        status_btn.clicked.connect(self.show_status)
        layout.addWidget(status_btn)

        open_btn = QPushButton("Open data folder…")
        open_btn.clicked.connect(self.open_data_folder)
        layout.addWidget(open_btn)

        backup_btn = QPushButton("Backup data/ to a dated zip")
        backup_btn.clicked.connect(self.backup_data)
        layout.addWidget(backup_btn)

        clean_btn = QPushButton("Clean Lodestone probe artifacts…")
        clean_btn.clicked.connect(self.clean_probe)
        layout.addWidget(clean_btn)

        layout.addStretch(1)
        return box

    def _build_chars_box(self) -> QGroupBox:
        box = QGroupBox("Characters")
        layout = QVBoxLayout(box)

        self.chars_list = QListWidget()
        self.chars_list.setAlternatingRowColors(True)
        layout.addWidget(self.chars_list, 1)

        row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_characters)
        row.addWidget(refresh_btn)
        open_chars_btn = QPushButton("Open in browser")
        open_chars_btn.clicked.connect(lambda: self._open_app_page("/characters"))
        row.addWidget(open_chars_btn)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_settings_box(self) -> QGroupBox:
        box = QGroupBox("Settings")
        layout = QVBoxLayout(box)

        self.mem_log_chk = QCheckBox("Enable ingest memory logging")
        self.mem_log_chk.setChecked(self._ingest_mem_log_enabled)
        self.mem_log_chk.setToolTip(
            "Pass --mem-log to workbook ingest for per-phase RAM checkpoints."
        )
        self.mem_log_chk.toggled.connect(self.set_ingest_mem_log)
        layout.addWidget(self.mem_log_chk)

        bind_btn = QPushButton("Set bind IP for LAN access…")
        bind_btn.clicked.connect(self.set_bind_ip)
        layout.addWidget(bind_btn)

        deps_btn = QPushButton("Reinstall / upgrade dependencies")
        deps_btn.clicked.connect(self.reinstall_deps)
        if IS_EMBEDDED:
            deps_btn.setEnabled(False)
            deps_btn.setToolTip("Bundled runtime: reinstall via the latest installer instead.")
        layout.addWidget(deps_btn)

        runtime = "bundled python\\" if IS_EMBEDDED else ".venv\\"
        layout.addWidget(QLabel(f"<i>Runtime: {runtime}</i>"))

        layout.addStretch(1)
        return box

    def _build_log_box(self) -> QGroupBox:
        box = QGroupBox("Log")
        layout = QVBoxLayout(box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(10000)
        self.log.setFont(QFont("Consolas"))
        layout.addWidget(self.log)
        return box

    # -- helpers ---------------------------------------------------------

    def _log(self, msg: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{ts}] {msg}")

    def _new_unbuffered_process(self) -> QProcess:
        """Build a QProcess wired for live, line-buffered output.

        Two non-obvious choices here, both learned the hard way:

        * SeparateChannels (not MergedChannels). On Windows, Qt's merged
          channel does not reliably interleave stderr - and uvicorn's startup
          logs ("Application startup complete.", etc.) all go to stderr.
          With merged channels the log pane stayed empty for minutes.
        * PYTHONUNBUFFERED=1 plus passing `-u` to python. Python defaults to
          block-buffering when stdout is a pipe; without forcing unbuffered
          mode, even prints to stdout can sit in the pipe for ages.
        """
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        proc.setProcessEnvironment(env)
        return proc

    def _drain_to_log(self, proc: QProcess, *, sink=None) -> None:
        out = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
        err = proc.readAllStandardError().data().decode("utf-8", errors="replace")
        for line in (out + err).splitlines():
            if line.strip():
                self.log.appendPlainText(line)
                if sink is not None:
                    try:
                        sink(line)
                    except Exception:
                        # Don't let file logging issues break the live UI log.
                        pass

    def _is_server_listening(self) -> bool:
        """Probe the actual bind host, not just loopback. A LAN-only bind
        like 192.168.1.42 won't answer on 127.0.0.1, so the previous fixed
        probe reported 'not running' when the server was up."""
        host = current_host()
        # 0.0.0.0 means "all interfaces" - loopback works.
        # 127.0.0.1 → loopback.
        # Anything else → probe that exact interface.
        if port_in_use("127.0.0.1", PORT):
            return True
        if host not in ("0.0.0.0", "127.0.0.1"):
            return port_in_use(host, PORT)
        return False

    def _refresh_status(self) -> None:
        host = current_host()
        running = self._is_server_listening()

        if self._server_error and not running:
            self.dot.set_state("error")
            self.status_lbl.setText("Server exited with an error")
            self.url_lbl.setText("")
        elif running:
            self.dot.set_state("running")
            url = f"http://{browser_host(host)}:{PORT}"
            self.status_lbl.setText("Running")
            self.url_lbl.setText(f'<a href="{url}">{url}</a>')
            self.url_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            self.url_lbl.setOpenExternalLinks(True)
        else:
            self.dot.set_state("stopped")
            self.status_lbl.setText("Not running")
            self.url_lbl.setText("")

        if host == "127.0.0.1":
            self.bind_lbl.setText(f"Bind: {host}:{PORT}  (loopback)")
        elif host == "0.0.0.0":
            self.bind_lbl.setText(f"Bind: {host}:{PORT}  (all interfaces - LAN reachable)")
        else:
            self.bind_lbl.setText(f"Bind: {host}:{PORT}  (LAN bind)")

        # Button gating: don't let the user start a second one or stop one we
        # didn't spawn (we only own _server when we started it ourselves).
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(self._server is not None and self._server.state() != QProcess.ProcessState.NotRunning)
        self.browser_btn.setEnabled(running)

    # -- database status + characters list ------------------------------

    def _db_status(self) -> tuple[str, str, str]:
        """Return (dot_state, summary, detail) for the database indicator.

        States:
          stopped  - no DB file yet (user hasn't ingested anything)
          starting - DB exists but no ingest run / no rows
          running  - DB exists with a completed ingest run and >0 rows
          error    - DB file present but cannot be opened or schema is unexpected
        """
        if not DB_PATH.exists():
            return ("stopped", "Database: not yet built",
                    "Ingest the workbook to create it.")

        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                run = conn.execute(
                    "SELECT id, source_file, completed_at, sheet_count, row_count "
                    "FROM ingest_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                char_count = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            return ("error", "Database: error",
                    f"Could not read {DB_PATH.name}: {e}")

        size = fmt_bytes(DB_PATH.stat().st_size)
        if run is None:
            return ("starting", "Database: empty (no ingest yet)",
                    f"{DB_PATH.name} exists ({size}) but has no ingest runs.")
        if not run["row_count"]:
            return ("starting", "Database: empty (last ingest produced no rows)",
                    f"Last source: {run['source_file']} - re-ingest a newer workbook.")
        completed = run["completed_at"] or "in-progress"
        detail = (
            f"Latest run #{run['id']} · {run['sheet_count']} sheets · "
            f"{run['row_count']:,} rows · completed {completed} · "
            f"source: {run['source_file']} · {size} · {char_count} character(s)"
        )
        return ("running", "Database: ready", detail)

    def _refresh_db_status(self) -> None:
        state, summary, detail = self._db_status()
        self.db_dot.set_state(state)
        self.db_status_lbl.setText(summary)
        self.db_detail_lbl.setText(detail)

    def _refresh_characters(self) -> None:
        self.chars_list.clear()
        if not DB_PATH.exists():
            item = QListWidgetItem("(no database yet - ingest the workbook)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.chars_list.addItem(item)
            return
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT name, starting_class FROM characters ORDER BY name"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            item = QListWidgetItem(f"(error reading characters: {e})")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.chars_list.addItem(item)
            return

        if not rows:
            item = QListWidgetItem("(no characters yet - create one on the /characters page)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.chars_list.addItem(item)
            return

        for r in rows:
            cls = f"  [{r['starting_class']}]" if r["starting_class"] else ""
            self.chars_list.addItem(QListWidgetItem(f"{r['name']}{cls}"))

    def _open_app_page(self, path: str) -> None:
        if not self._is_server_listening():
            QMessageBox.information(
                self, "Server not running",
                "Start the server first - then this button opens the page in your browser.",
            )
            return
        host = current_host()
        webbrowser.open(f"http://{browser_host(host)}:{PORT}{path}")

    def show_instructions(self) -> None:
        url = None
        if self._is_server_listening():
            host = current_host()
            url = f"http://{browser_host(host)}:{PORT}"
        InstructionsDialog(url, self).exec()

    # -- server lifecycle -----------------------------------------------

    def start_server(self) -> None:
        if self._is_server_listening():
            host = current_host()
            self._log(f"Port {PORT} already in use on {host} - not spawning a second server.")
            QMessageBox.information(
                self, "Server already running",
                f"Something is already listening on {host}:{PORT}.\n"
                "Use 'Launch browser' to open it.",
            )
            self._refresh_status()
            return

        host = current_host()
        self._server_error = False
        self.dot.set_state("starting")
        self.status_lbl.setText("Starting…")

        # NOTE on the missing --reload flag: uvicorn's --reload spawns a
        # supervisor + worker pair; QProcess only sees the supervisor's stdio,
        # so worker logs (the interesting ones) never reach the log pane.
        # End users don't need reload anyway. Pass -u so Python is line-
        # buffered, otherwise log output sits in pipe buffers for ages.
        args = [
            "-u",
            "-m", "uvicorn",
            "app.main:app",
            "--host", host,
            "--port", str(PORT),
            "--log-level", "info",
        ]

        proc = self._new_unbuffered_process()
        proc.setProgram(str(VENV_PY))
        proc.setArguments(args)
        proc.setWorkingDirectory(str(ROOT))

        proc.readyReadStandardOutput.connect(self._on_proc_output)
        proc.readyReadStandardError.connect(self._on_proc_output)
        proc.errorOccurred.connect(self._on_proc_error)
        proc.finished.connect(self._on_proc_finished)

        cmd_line = " ".join([str(VENV_PY)] + args)
        self._log(f"$ {cmd_line}")

        proc.start()
        if not proc.waitForStarted(3000):
            self._log(f"Failed to start: {proc.errorString()}")
            self._server_error = True
            self._server = None
            self._refresh_status()
            return

        self._server = proc
        self._log(f"Spawned uvicorn (pid {int(proc.processId())}) on {host}:{PORT}")

        # Open the browser only once the port is actually listening (so the
        # browser doesn't race uvicorn's bind). We poll every 300 ms up to 15 s.
        self._wait_for_bind_then_browse(deadline_ms=15000, every_ms=300)

    def _wait_for_bind_then_browse(self, deadline_ms: int, every_ms: int) -> None:
        elapsed = {"ms": 0}

        def tick() -> None:
            if self._is_server_listening():
                self.launch_browser()
                self._refresh_status()
                return
            if self._server is None or self._server.state() == QProcess.ProcessState.NotRunning:
                # Process died before binding - _on_proc_finished will report.
                return
            elapsed["ms"] += every_ms
            if elapsed["ms"] >= deadline_ms:
                self._log("Server did not start listening within 15 s; not opening the browser automatically.")
                return
            QTimer.singleShot(every_ms, tick)

        QTimer.singleShot(every_ms, tick)

    def stop_server(self) -> None:
        if self._server is None or self._server.state() == QProcess.ProcessState.NotRunning:
            return
        self._log("Stopping server…")
        self._user_stopping = True
        self._server.terminate()
        if not self._server.waitForFinished(3000):
            self._log("Server did not terminate gracefully - killing.")
            self._server.kill()
            self._server.waitForFinished(2000)

    def launch_browser(self) -> None:
        host = current_host()
        url = f"http://{browser_host(host)}:{PORT}"
        webbrowser.open(url)

    def _on_proc_output(self) -> None:
        if self._server is None:
            return
        self._drain_to_log(self._server)

    def _on_proc_error(self, err: QProcess.ProcessError) -> None:
        self._log(f"Server process error: {err.name}")
        if not self._user_stopping:
            self._server_error = True
        self._refresh_status()

    def _on_proc_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self._log(f"Server exited (code={exit_code}, status={exit_status.name})")
        # A user-clicked Stop produces a non-zero exit on Windows because
        # we terminate the process - don't treat that as an error.
        if not self._user_stopping and (
            exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit
        ):
            self._server_error = True
        self._user_stopping = False
        self._server = None
        self._refresh_status()

    # -- actions ---------------------------------------------------------

    def ingest_workbook(self) -> None:
        start_dir = str(SPREADSHEET_DIR if SPREADSHEET_DIR.exists() else ROOT)
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick the FFXIV completion workbook",
            start_dir, "Excel workbooks (*.xlsx);;All files (*.*)",
        )
        if not path:
            return

        log_dir = DATA_DIR / "logs"
        log_path: Path | None = None
        ingest_log_fh: TextIO | None = None
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            log_path = log_dir / f"ingest_{stamp}.log"
            ingest_log_fh = log_path.open("w", encoding="utf-8")
        except OSError as e:
            self._log(f"Could not open ingest log file under {log_dir}: {e}")

        def _write_ingest_line(line: str) -> None:
            if ingest_log_fh is None:
                return
            ingest_log_fh.write(line + "\n")
            ingest_log_fh.flush()

        def _close_ingest_log() -> None:
            nonlocal ingest_log_fh
            if ingest_log_fh is None:
                return
            try:
                ingest_log_fh.flush()
                ingest_log_fh.close()
            finally:
                ingest_log_fh = None

        self._log(f"Ingesting {path} …")
        _write_ingest_line(f"[gui] Ingesting {path}")
        if log_path is not None:
            self._log(f"Saving ingest log: {log_path}")
            _write_ingest_line(f"[gui] Saving ingest log: {log_path}")

        script = ROOT / "scripts" / "prep_xlsx_to_sqlite.py"
        proc = self._new_unbuffered_process()
        proc.setProgram(str(VENV_PY))
        args = ["-u", str(script), "--xlsx", path]
        if self._ingest_mem_log_enabled:
            args.append("--mem-log")
            self._log("Ingest memory logging is enabled (--mem-log).")
            _write_ingest_line("[gui] Ingest memory logging enabled (--mem-log)")
        _write_ingest_line("[gui] Command: " + " ".join([str(VENV_PY), *args]))
        proc.setArguments(args)
        proc.setWorkingDirectory(str(ROOT))

        def on_done(code, status):
            self._log(f"Ingest finished (code={code}, status={status.name})")
            _write_ingest_line(f"[gui] Ingest finished (code={code}, status={status.name})")
            _close_ingest_log()
            self._refresh_db_status()
            self._refresh_characters()
            details = f"Ingest exited with code {code}.\n\nSee the log pane for details."
            if log_path is not None:
                details += f"\n\nSaved log:\n{log_path}"
            QMessageBox.information(
                self, "Ingest finished",
                details,
            )

        proc.readyReadStandardOutput.connect(
            lambda: self._drain_to_log(proc, sink=_write_ingest_line)
        )
        proc.readyReadStandardError.connect(
            lambda: self._drain_to_log(proc, sink=_write_ingest_line)
        )
        proc.finished.connect(on_done)
        proc.start()
        if not proc.waitForStarted(3000):
            self._log(f"Could not start ingest: {proc.errorString()}")
            _write_ingest_line(f"[gui] Could not start ingest: {proc.errorString()}")
            _close_ingest_log()
            QMessageBox.warning(self, "Ingest failed to start", proc.errorString())

    def show_status(self) -> None:
        lines: list[str] = []
        lines.append("=== Workbook & DB ===")
        if SPREADSHEET_DIR.exists():
            xlsx = sorted(SPREADSHEET_DIR.glob("*.xlsx"))
            lines.append(f"  Spreadsheet/    {len(xlsx)} .xlsx file(s)")
            if xlsx:
                newest = max(xlsx, key=lambda p: p.stat().st_mtime)
                mtime = dt.datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"                  newest: {newest.name}  ({mtime})")
        else:
            lines.append("  Spreadsheet/    (missing)")

        if DB_PATH.exists():
            lines.append(f"  Database        {DB_PATH.name}  {fmt_bytes(DB_PATH.stat().st_size)}")
            try:
                conn = sqlite3.connect(str(DB_PATH))
                conn.row_factory = sqlite3.Row
                try:
                    run = conn.execute(
                        "SELECT id, source_file, started_at, completed_at, sheet_count, row_count "
                        "FROM ingest_runs ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if run is None:
                        lines.append("  Latest ingest   (no runs recorded)")
                    else:
                        lines.append(f"  Latest ingest   run #{run['id']}  source: {run['source_file']}")
                        lines.append(f"                  started:   {run['started_at']}")
                        lines.append(f"                  completed: {run['completed_at'] or '(in progress?)'}")
                        lines.append(f"                  sheets={run['sheet_count']}  rows={run['row_count']}")
                    chars = conn.execute(
                        "SELECT name, starting_class FROM characters ORDER BY name"
                    ).fetchall()
                    lines.append(f"  Characters      {len(chars)}")
                    for c in chars:
                        cls = f" [{c['starting_class']}]" if c["starting_class"] else ""
                        lines.append(f"                   - {c['name']}{cls}")
                finally:
                    conn.close()
            except sqlite3.Error as e:
                lines.append(f"  DB query error: {e}")
        else:
            lines.append("  Database        (no DB; run ingest first)")

        lines.append("")
        lines.append("=== Storage ===")
        sidecar_count = len(list(PROGRESS_DIR.glob("*.json"))) if PROGRESS_DIR.exists() else 0
        lines.append(f"  data/                  {fmt_bytes(dir_size(DATA_DIR))}")
        lines.append(f"    progress/            {fmt_bytes(dir_size(PROGRESS_DIR))}  ({sidecar_count} sidecars)")
        lines.append(f"    lodestone_probe/     {fmt_bytes(dir_size(PROBE_DIR))}")
        if BACKUP_DIR.exists():
            backups = sorted(BACKUP_DIR.glob("data_*.zip"))
            lines.append(f"  backups/               {fmt_bytes(dir_size(BACKUP_DIR))}  ({len(backups)} archive(s))")

        text = "\n".join(lines)
        dlg = QDialog(self)
        dlg.setWindowTitle("Status / health")
        dlg.resize(640, 480)
        v = QVBoxLayout(dlg)
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setFont(QFont("Consolas"))
        body.setPlainText(text)
        v.addWidget(body)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        v.addWidget(btns)
        dlg.exec()

    def open_data_folder(self) -> None:
        folders = [
            ("Project root",          ROOT),
            ("Spreadsheet/",          SPREADSHEET_DIR),
            ("data/",                 DATA_DIR),
            ("data/progress/",        PROGRESS_DIR),
            ("data/lodestone_probe/", PROBE_DIR),
            ("backups/",              BACKUP_DIR),
        ]
        items = [
            f"{label}{'  (does not exist yet)' if not path.exists() else ''}"
            for label, path in folders
        ]
        choice, ok = QInputDialog.getItem(
            self, "Open folder", "Folder:", items, 0, False,
        )
        if not ok:
            return
        idx = items.index(choice)
        _, path = folders[idx]
        if not path.exists():
            QMessageBox.information(self, "Folder missing", f"{path} does not exist yet.")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as e:
            QMessageBox.warning(self, "Could not open", str(e))

    def backup_data(self) -> None:
        if not DB_PATH.exists() and not PROGRESS_DIR.exists():
            QMessageBox.information(
                self, "Nothing to back up",
                "No DB and no progress sidecars yet - nothing to archive.",
            )
            return

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = BACKUP_DIR / f"data_{ts}"
        work = BACKUP_DIR / f".staging_{ts}"
        try:
            work.mkdir(parents=True, exist_ok=False)
            staged_data = work / "data"
            staged_data.mkdir()
            if DB_PATH.exists():
                shutil.copy2(DB_PATH, staged_data / DB_PATH.name)
            if PROGRESS_DIR.exists():
                shutil.copytree(PROGRESS_DIR, staged_data / "progress")
            archive = shutil.make_archive(str(base), "zip", str(work))
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)

        size = Path(archive).stat().st_size
        self._log(f"Backup written: {Path(archive).name} ({fmt_bytes(size)})")
        QMessageBox.information(
            self, "Backup written",
            f"{Path(archive).name}  ({fmt_bytes(size)})\n\n"
            f"Location: {BACKUP_DIR}\n"
            "Includes: data/ffxiv_tracker.sqlite + data/progress/\n"
            "Excludes: data/lodestone_probe/ (regenerable)",
        )

    def clean_probe(self) -> None:
        if not PROBE_DIR.exists():
            QMessageBox.information(
                self, "Nothing to clean",
                f"{PROBE_DIR} does not exist yet - nothing to clean.",
            )
            return

        sections: list[tuple[str, list[Path]]] = [
            ("logs/",            [p for p in (PROBE_DIR / "logs").rglob("*") if p.is_file()]
                                  if (PROBE_DIR / "logs").exists() else []),
            ("import_logs/",     [p for p in (PROBE_DIR / "import_logs").rglob("*") if p.is_file()]
                                  if (PROBE_DIR / "import_logs").exists() else []),
            ("import_uploads/",  [p for p in (PROBE_DIR / "import_uploads").rglob("*") if p.is_file()]
                                  if (PROBE_DIR / "import_uploads").exists() else []),
            ("unmatched/",       [p for p in (PROBE_DIR / "unmatched").rglob("*") if p.is_file()]
                                  if (PROBE_DIR / "unmatched").exists() else []),
            ("*.json payloads",  list(PROBE_DIR.glob("*.json"))),
        ]
        total_files = sum(len(f) for _, f in sections)
        total_bytes = sum(sum(p.stat().st_size for p in f if p.exists()) for _, f in sections)

        if total_files == 0:
            QMessageBox.information(self, "Nothing to delete", "No probe artifacts on disk.")
            return

        summary = "\n".join(
            f"  {label:24s} {len(files):4d} files   {fmt_bytes(sum(p.stat().st_size for p in files if p.exists()))}"
            for label, files in sections
        )
        days, ok = QInputDialog.getInt(
            self, "Clean probe artifacts",
            (f"{summary}\n\n  TOTAL: {total_files} files / {fmt_bytes(total_bytes)}\n\n"
             "Delete files older than how many days?\n"
             "(0 = delete all)"),
            value=14, min=0, max=3650,
        )
        if not ok:
            return

        if days == 0:
            confirm = QMessageBox.question(
                self, "Confirm",
                f"Delete ALL {total_files} probe artifact files ({fmt_bytes(total_bytes)})?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        import time as _time
        cutoff = _time.time() - days * 86400 if days > 0 else None
        deleted = 0
        freed = 0
        for _, files in sections:
            for f in files:
                try:
                    st = f.stat()
                    if cutoff is None or st.st_mtime < cutoff:
                        size = st.st_size
                        f.unlink()
                        deleted += 1
                        freed += size
                except OSError:
                    pass

        self._log(f"Deleted {deleted} probe artifact(s) ({fmt_bytes(freed)})")
        QMessageBox.information(
            self, "Done", f"Deleted {deleted} file(s)  ({fmt_bytes(freed)})",
        )

    def set_bind_ip(self) -> None:
        dlg = BindIpDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_host = dlg.selected_host()
        if not new_host:
            QMessageBox.warning(self, "Invalid IP", "That doesn't look like a valid IPv4 address.")
            return
        self._cfg["host"] = new_host
        save_config(self._cfg)
        self._log(f"Bind host set to {new_host}")
        self._refresh_status()
        if port_in_use("127.0.0.1", PORT):
            QMessageBox.information(
                self, "Restart needed",
                "The server is running. Stop and restart it for the new bind to take effect.",
            )

    def set_ingest_mem_log(self, enabled: bool) -> None:
        self._ingest_mem_log_enabled = bool(enabled)
        self._cfg["ingest_mem_log"] = self._ingest_mem_log_enabled
        save_config(self._cfg)
        state = "enabled" if self._ingest_mem_log_enabled else "disabled"
        self._log(f"Ingest memory logging {state}.")

    def reinstall_deps(self) -> None:
        if IS_EMBEDDED:
            QMessageBox.information(
                self, "Bundled runtime",
                "This installation uses a bundled Python runtime.\n"
                "To update dependencies, reinstall from the latest release.",
            )
            return

        self._log("Reinstalling dependencies from requirements.txt …")
        proc = self._new_unbuffered_process()
        proc.setProgram(str(VENV_PY))
        proc.setArguments(["-u", "-m", "pip", "install", "--upgrade", "-r", str(REQUIREMENTS)])
        proc.setWorkingDirectory(str(ROOT))

        def on_done(code, _status):
            self._log(f"pip install finished (code={code})")
            if code == 0:
                QMessageBox.information(self, "Done", "Dependencies upgraded.")
            else:
                QMessageBox.warning(self, "pip failed", f"pip exited with code {code}. See log.")

        proc.readyReadStandardOutput.connect(lambda: self._drain_to_log(proc))
        proc.readyReadStandardError.connect(lambda: self._drain_to_log(proc))
        proc.finished.connect(on_done)
        proc.start()

    def open_discord(self) -> None:
        webbrowser.open(DISCORD_INVITE_URL)
        self._log(f"Opened {DISCORD_INVITE_URL}")

    def check_for_updates(self) -> None:
        self._log("Checking GitHub for updates…")

        self._update_thread = QThread(self)
        self._update_worker = UpdateCheckWorker()
        self._update_worker.moveToThread(self._update_thread)
        self._update_thread.started.connect(self._update_worker.run)
        self._update_worker.finished.connect(self._on_update_check)
        self._update_worker.finished.connect(self._update_thread.quit)
        self._update_worker.finished.connect(self._update_worker.deleteLater)
        self._update_thread.finished.connect(self._update_thread.deleteLater)
        self._update_thread.start()

    def _on_update_check(self, result: updater.UpdateCheckResult) -> None:
        if result.error:
            self._log(f"Update check failed: {result.error}")
        elif result.is_newer and result.latest:
            self._log(f"Update available: {result.latest.tag} (installed: {result.current})")
        elif result.latest:
            self._log(f"Up to date: {result.current}")
        dlg = UpdateDialog(result, self)
        dlg.exec()

    def request_quit_for_update(self) -> None:
        self._log("Installer launched; closing launcher for update…")
        self._closing_for_update = True
        self.close()

    # -- shutdown --------------------------------------------------------

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        if a0 is None:
            return
        if self._closing_for_update:
            if self._server is not None and self._server.state() != QProcess.ProcessState.NotRunning:
                self.stop_server()
            a0.accept()
            return
        if self._server is not None and self._server.state() != QProcess.ProcessState.NotRunning:
            reply = QMessageBox.question(
                self, "Server still running",
                "The web server is running. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                a0.ignore()
                return
            self.stop_server()
        a0.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("FFXIV Completion Tracker")
    app.setApplicationVersion(APP_VERSION)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
