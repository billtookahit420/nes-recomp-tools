#!/usr/bin/env python3
"""NESRecomp Tools — convert NES ROMs to native PC games.

Pick ROMs -> they are probed instantly from their headers -> Run Queue
converts them one at a time (cycle-accurate NESRecomp backend, launcher
UI, RetroAchievements with in-game pop-ups, cover art) and packages
Linux + Windows zips. Failures are logged and skipped, never fatal to
the queue. Games whose mapper is outside the runtime's coverage are
flagged "needs review" instead of being guessed at.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

from PySide6.QtCore import Qt, QThread, Signal  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_DIR / "settings.json"

HELP_TEXT = """\
NESRecomp Tools — turn your own NES cartridges' ROM dumps into native
PC games: no emulator, the game's code is recompiled to a real
executable with a launcher, save states, rewind, netplay, cover art
and RetroAchievements.

HOW IT WORKS
  Add .nes files (or a whole folder). Each ROM is probed instantly
  from its header: mapper, PRG/CHR size, mirroring, region. Press
  Run Queue and each game is converted one at a time and packaged
  as <Game>-Linux.zip and <Game>-Windows.zip in the output folder.
  A game that fails is marked red and the queue moves on.

PLAYERS / REGION
  Players defaults to 2 (most NES games are 1-2 player) and region
  is read from the ROM's filename (Europe/PAL names -> PAL, else
  NTSC). Both can be changed per queue run in Settings — they apply
  to every game in the run.

MAPPERS — "NEEDS REVIEW"
  The cycle runtime covers the classic mappers (0, 1, 2, 3, 4, 7,
  66) and dozens more. A ROM whose mapper is outside the coverage
  list is flagged "needs review" and skipped by the queue. The
  Settings option "Try unsupported mappers anyway" attempts them
  regardless — some will work, some won't; that's what the flag
  means.

THE GAMES THEMSELVES
  The package contains the executable, its launcher assets and a
  README. It does NOT contain the ROM: at first launch you point
  the launcher at your own .nes file and it verifies it is the
  exact ROM the game was built from (matching fingerprint). Keep
  the assets/ folder next to the executable — the launcher's art,
  fonts and icons live there.

RETROACHIEVEMENTS
  With RA on (default), each game ships with retroachievements.toml
  support: put your filled toml next to the game executable
  (username + the Connect api_token, in double quotes; hardcore
  unquoted) and the game logs in when it starts — you'll see a
  "Logged in" toast in game, and achievement unlocks pop up the
  same way. The Settings pop-ups checkbox hides both. Note that
  RetroAchievements counts hardcore only from approved clients;
  recomp clients currently earn Casual/softcore unlocks. Use the
  RA toml picker in Settings to have your filled toml included in
  every package automatically.

NETPLAY
  Two-player games can be played online from the game's launcher
  (Netplay page): set a player name, host or join a lobby. Both
  players must be running the identical ROM. LAN lobbies are
  discovered automatically.

COLORS
  The framework renders NES colour from a model of the NTSC TV
  signal, so games look a little less saturated than most emulator
  defaults — closer to a real television. That's deliberate.

SAVE STATES / REWIND
  In game: F7 opens the save-state browser, F8 rewinds (see the
  game's Settings for the rewind buffer).

CREDITS
  Built on NESRecomp by Matthew Stanley (mstan), part of the
  RetroPortingToolkit (retroportingtoolkit.com;
  github.com/mstan, github.com/RetroPortingToolKit).
  NESRecomp license: PolyForm Noncommercial 1.0.0.
  Launcher: recomp-ui (MIT, (c) 2026 Matthew Stanley).
"""


class Worker(QThread):
    log = Signal(str)
    status = Signal(int, str)
    finished_all = Signal()

    def __init__(self, items, settings):
        super().__init__()
        self.items = items          # list of (row, job-dict)
        self.settings = settings

    def run(self):
        for row, job in self.items:
            self.status.emit(row, "converting…")
            try:
                result = engine.run_job(job, log=self.log.emit)
                if result["status"] == "needs_review":
                    self.status.emit(row, "needs review")
                else:
                    self.status.emit(row, "done")
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"FAILED {job.get('title')}: {e}")
                self.status.emit(row, "failed")
        self.finished_all.emit()


class CoverWorker(QThread):
    done = Signal(str, object)

    def __init__(self, title):
        super().__init__()
        self.title = title

    def run(self):
        try:
            p = engine.fetch_cover(self.title)
        except Exception:  # noqa: BLE001
            p = None
        self.done.emit(self.title, p)


class InstallWorker(QThread):
    done = Signal(bool)

    def __init__(self, packages):
        super().__init__()
        self.packages = packages

    def run(self):
        try:
            subprocess.run(["pkexec", "apt-get", "update"], check=False)
            r = subprocess.run(["pkexec", "apt-get", "install", "-y"]
                               + self.packages, check=False)
            self.done.emit(r.returncode == 0)
        except Exception:  # noqa: BLE001
            self.done.emit(False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NESRecomp Tools")
        self.resize(1180, 760)
        self.items = []             # list of probe dicts (+ status)
        self.worker = None
        self.cover_worker = None
        self._build_ui()
        self._load_settings()
        self.refresh_toolbox()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        tabs = QTabWidget()
        tabs.addTab(self._convert_tab(), "Convert")
        help_edit = QPlainTextEdit(HELP_TEXT)
        help_edit.setReadOnly(True)
        tabs.addTab(help_edit, "Help")
        self.setCentralWidget(tabs)

    def _convert_tab(self):
        root = QWidget()
        v = QVBoxLayout(root)
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Title", "Mapper", "PRG", "CHR", "Region", "Status"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self.show_selected)
        lv.addWidget(self.table)
        btns = QHBoxLayout()
        for label, fn in [("Add ROMs…", self.add_roms),
                          ("Add Folder…", self.add_folder),
                          ("Remove", self.remove_selected),
                          ("Clear", self.clear_queue)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lv.addLayout(btns)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(self._probe_box())
        rv.addWidget(self._settings_box())
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([640, 480])
        v.addWidget(split, 3)

        run_row = QHBoxLayout()
        self.toolbox_label = QLabel("Toolbox: …")
        run_row.addWidget(self.toolbox_label)
        self.install_btn = QPushButton("Install missing packages")
        self.install_btn.clicked.connect(self.install_packages)
        run_row.addWidget(self.install_btn)
        run_row.addStretch(1)
        self.run_btn = QPushButton("Run Queue")
        self.run_btn.clicked.connect(self.run_queue)
        run_row.addWidget(self.run_btn)
        v.addLayout(run_row)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        v.addWidget(self.log_edit, 2)
        root._split = split
        return root

    def _probe_box(self):
        box = QGroupBox("ROM details")
        form = QFormLayout(box)
        self.p_title = QLabel("—")
        self.p_mapper = QLabel("—")
        self.p_sizes = QLabel("—")
        self.p_region = QLabel("—")
        self.p_crc = QLabel("—")
        self.p_cover = QLabel("")
        self.p_cover.setAlignment(Qt.AlignCenter)
        form.addRow("Title:", self.p_title)
        form.addRow("Mapper:", self.p_mapper)
        form.addRow("PRG / CHR:", self.p_sizes)
        form.addRow("Region:", self.p_region)
        form.addRow("CRC32:", self.p_crc)
        form.addRow(self.p_cover)
        return box

    def _settings_box(self):
        box = QGroupBox("Settings (apply to the whole queue)")
        form = QFormLayout(box)
        self.s_ra = QCheckBox("RetroAchievements")
        self.s_ra.setChecked(True)
        self.s_popups = QCheckBox("Achievement pop-ups + login toast")
        self.s_popups.setChecked(True)
        self.s_force = QCheckBox("Try unsupported mappers anyway")
        form.addRow(self.s_ra)
        form.addRow(self.s_popups)
        form.addRow(self.s_force)
        toml_row = QHBoxLayout()
        self.s_toml = QLineEdit()
        pick = QPushButton("Pick…")
        pick.clicked.connect(lambda: self._pick_file(self.s_toml,
                                                     "TOML (*.toml)"))
        toml_row.addWidget(self.s_toml)
        toml_row.addWidget(pick)
        form.addRow("RA toml to include:", toml_row)
        tgt_row = QHBoxLayout()
        self.s_linux = QCheckBox("Linux")
        self.s_linux.setChecked(True)
        self.s_windows = QCheckBox("Windows")
        self.s_windows.setChecked(True)
        tgt_row.addWidget(self.s_linux)
        tgt_row.addWidget(self.s_windows)
        form.addRow("Build targets:", tgt_row)
        self.s_players = QComboBox()
        self.s_players.addItems(["1", "2"])
        self.s_players.setCurrentText("2")
        form.addRow("Players:", self.s_players)
        self.s_jobs = QSpinBox()
        self.s_jobs.setRange(1, 16)
        self.s_jobs.setValue(2)
        form.addRow("Build jobs:", self.s_jobs)
        out_row = QHBoxLayout()
        self.s_out = QLineEdit(str(APP_DIR / "output"))
        pick_o = QPushButton("Pick…")
        pick_o.clicked.connect(lambda: self._pick_dir(self.s_out))
        out_row.addWidget(self.s_out)
        out_row.addWidget(pick_o)
        form.addRow("Output folder:", out_row)
        return box

    # ------------------------------------------------------------ util
    def _pick_file(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, "Pick a file", "", filt)
        if p:
            edit.setText(p)

    def _pick_dir(self, edit):
        p = QFileDialog.getExistingDirectory(self, "Pick a folder")
        if p:
            edit.setText(p)

    def log_line(self, msg):
        self.log_edit.appendPlainText(str(msg))

    def refresh_toolbox(self):
        try:
            t = engine.toolbox()
            if t["ready"]:
                self.toolbox_label.setText("Toolbox: ready")
                self.install_btn.setEnabled(False)
            else:
                self.toolbox_label.setText(
                    "Toolbox: missing " + ", ".join(t["missing_apt"]))
                self.install_btn.setEnabled(True)
            self._missing = t["missing_apt"]
        except Exception as e:  # noqa: BLE001
            self.toolbox_label.setText(f"Toolbox: check failed ({e})")
            self._missing = []

    def install_packages(self):
        if not getattr(self, "_missing", None):
            return
        self.install_btn.setEnabled(False)
        self.log_line("Installing: " + ", ".join(self._missing))
        self._inst = InstallWorker(self._missing)
        self._inst.done.connect(self._install_done)
        self._inst.start()

    def _install_done(self, ok):
        self.log_line("Package install " + ("done." if ok else "FAILED."))
        self.refresh_toolbox()

    # ------------------------------------------------------------ queue
    def _add_paths(self, paths):
        for p in paths:
            try:
                probe = engine.probe_rom(str(p))
            except Exception as e:  # noqa: BLE001
                self.log_line(f"skip {p}: {e}")
                continue
            self.items.append(probe)
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._fill_row(row, probe,
                           "needs review" if probe["needs_review"] else "ready")
        self.table.resizeColumnsToContents()

    def _fill_row(self, row, probe, status):
        vals = [probe["title"], str(probe["mapper"]),
                f"{probe['prg_kb']}K", f"{probe['chr_kb']}K",
                probe["region"], status]
        for c, val in enumerate(vals):
            self.table.setItem(row, c, QTableWidgetItem(val))

    def _set_status(self, row, status):
        self.table.setItem(row, 5, QTableWidgetItem(status))

    def add_roms(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add NES ROMs", "", "NES ROMs (*.nes)")
        self._add_paths(paths)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of ROMs")
        if d:
            self._add_paths(sorted(Path(d).glob("*.nes")))

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
            del self.items[r]

    def clear_queue(self):
        self.table.setRowCount(0)
        self.items.clear()

    def show_selected(self):
        rows = self.table.selectedIndexes()
        if not rows:
            return
        probe = self.items[rows[0].row()]
        self.p_title.setText(probe["title"])
        self.p_mapper.setText(
            f"{probe['mapper']} (sub {probe['submapper']})"
            + ("" if probe["mapper_supported"] else " — NOT COVERED"))
        self.p_sizes.setText(
            f"{probe['prg_kb']}K / {probe['chr_kb']}K"
            + (" CHR-RAM" if probe["chr_ram"] else "")
            + f", {probe['mirroring']}"
            + (" , battery" if probe["battery"] else ""))
        self.p_region.setText(probe["region"])
        self.p_crc.setText(probe["crc32"])
        self.p_cover.setText("fetching art…")
        self.cover_worker = CoverWorker(probe["title"])
        self.cover_worker.done.connect(self._cover_done)
        self.cover_worker.start()

    def _cover_done(self, title, path):
        rows = self.table.selectedIndexes()
        if not rows or self.items[rows[0].row()]["title"] != title:
            return
        if path:
            pix = QPixmap(str(path))
            if not pix.isNull():
                self.p_cover.setPixmap(pix.scaled(
                    160, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self.p_cover.setText("(no art found)")

    # ------------------------------------------------------------ run
    def current_settings(self):
        return {
            "output_dir": self.s_out.text().strip(),
            "ra": self.s_ra.isChecked(),
            "ra_popups": self.s_popups.isChecked(),
            "ra_toml": self.s_toml.text().strip() or None,
            "targets": [t for t, on in [("linux", self.s_linux.isChecked()),
                                        ("windows", self.s_windows.isChecked())] if on],
            "players": int(self.s_players.currentText()),
            "jobs": self.s_jobs.value(),
            "force": self.s_force.isChecked(),
        }

    def run_queue(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.items:
            QMessageBox.information(self, "NESRecomp Tools",
                                    "Add at least one ROM first.")
            return
        s = self.current_settings()
        if not s["targets"]:
            QMessageBox.information(self, "NESRecomp Tools",
                                    "Pick at least one build target.")
            return
        self._save_settings(s)
        jobs = []
        for row, probe in enumerate(self.items):
            job = dict(s)
            job["rom"] = probe["rom"]
            job["title"] = probe["title"]
            job["region"] = probe["region"]
            jobs.append((row, job))
        self.run_btn.setEnabled(False)
        self.worker = Worker(jobs, s)
        self.worker.log.connect(self.log_line)
        self.worker.status.connect(self._set_status)
        self.worker.finished_all.connect(self._run_done)
        self.worker.start()

    def _run_done(self):
        self.run_btn.setEnabled(True)
        self.log_line("Queue finished.")

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            s = json.loads(SETTINGS_FILE.read_text())
        except Exception:  # noqa: BLE001
            return
        self.s_ra.setChecked(s.get("ra", True))
        self.s_popups.setChecked(s.get("ra_popups", True))
        self.s_force.setChecked(s.get("force", False))
        self.s_toml.setText(s.get("ra_toml") or "")
        self.s_linux.setChecked("linux" in s.get("targets", ["linux", "windows"]))
        self.s_windows.setChecked("windows" in s.get("targets", ["linux", "windows"]))
        self.s_players.setCurrentText(str(s.get("players", 2)))
        self.s_jobs.setValue(int(s.get("jobs", 2)))
        if s.get("output_dir"):
            self.s_out.setText(s["output_dir"])

    def _save_settings(self, s=None):
        try:
            SETTINGS_FILE.write_text(json.dumps(s or self.current_settings(),
                                                indent=2))
        except Exception:  # noqa: BLE001
            pass


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
