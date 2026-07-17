"""logmon_tray.py -- logmon alert watcher (system tray).  YASDC

The UNPRIVILEGED, per-user, always-on half of logmon's alerting.

WHY A SEPARATE APP (not "the GUI minimized to tray"):
  * PRIVILEGE. The config GUI must write C:\\ProgramData\\logmon\\logmon.cfg,
    which requires elevation. This watcher must run at every login, unelevated.
    Bundling them would leave an ELEVATED process running permanently in the
    user's session with the power to silently rewrite the archival config --
    an unacceptable liability in a forensic tool. This app is READ-ONLY on
    everything logmon owns.
  * LIFECYCLE. The GUI is an on-demand admin editor. This is a 24/7 login-to-
    logoff watcher. Alerts must fire whether or not anyone has the editor open.
  * ROBUSTNESS. The always-on component is kept small and nearly uncrashable.

WHAT THIS APP MAY AND MAY NOT DO (mirrors the ACL on the alert store):
  READS  : %ProgramData%\\logmon\\alerts.jsonl, URGENT.TXT, logmon_state.json
  WRITES : %APPDATA%\\logmon\\tray_marker.json   (per-user dismiss marker ONLY)
  NEVER  : writes, truncates or deletes alerts.jsonl, URGENT.TXT, logmon.cfg,
           or logmon_state.json.

DISMISS vs ACKNOWLEDGE -- two different ideas that look the same:
  * DISMISS (this app, any user, unprivileged): stop popping up notifications
    for alerts I have already seen. Writes a high-water mark to %APPDATA%.
    The alert REMAINS in alerts.jsonl. URGENT.TXT STAYS SET. The tray icon
    STAYS RED. Another user's tray still pops. Purely a personal convenience.
  * ACKNOWLEDGE (the elevated GUI, admin only): formally accept an alert. The
    GUI APPENDS an acknowledgment record (who / when / what) and clears
    URGENT.TXT. It never deletes alert records -- destroying the record of a
    tamper is the very erasure the ACLs exist to prevent.

A detected external clear of the Security log SHOULD require an administrator
to actively acknowledge it, not a user clicking an X.

Platform: Windows (tray + autostart + elevation). Runs on other platforms for
development, minus the Windows-specific bits.
"""

import json
import os
import sys
import subprocess

from PySide6.QtCore import QTimer, Qt, QRectF
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QPainter, QPixmap,
                           QBrush)
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QMenu, QMessageBox, QPushButton, QSystemTrayIcon,
                               QTextBrowser, QVBoxLayout)

APP_NAME = "logmon Alert Watcher"
RUN_KEY_NAME = "logmonTray"
POLL_MS = 15000          # how often we re-read the alert store

SEV_CRITICAL = "CRITICAL"
SEV_WARNING = "WARNING"


# =========================================================================== #
# Paths + readers.
# =========================================================================== #
# Deliberately standalone: this app does NOT import logmon.py. Per design lock
# 11/13.1, nothing external imports logmon internals. It reads the documented
# file formats (the contract), which also keeps the always-on watcher dependency
# -light and hard to break.

def logmon_dir():
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, "logmon")


def alerts_path():
    return os.path.join(logmon_dir(), "alerts.jsonl")


def urgent_path():
    return os.path.join(logmon_dir(), "URGENT.TXT")


def state_path():
    return os.path.join(logmon_dir(), "logmon_state.json")


def marker_path():
    """Per-user dismiss marker. %APPDATA% is user-writable, so this works
    unelevated -- and it means one user's dismissal never silences another's."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "logmon", "tray_marker.json")


def read_alerts():
    """Parse alerts.jsonl. Append-only JSON lines, so a partially-written final
    line is possible; skip anything unparseable rather than failing."""
    out = []
    try:
        with open(alerts_path(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return out


def read_state():
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def urgent_is_set():
    return os.path.exists(urgent_path())


def read_marker():
    try:
        with open(marker_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return int(d.get("last_shown_seq", 0))
    except Exception:
        return 0


def write_marker(seq):
    """The ONLY thing this app ever writes."""
    try:
        p = marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_shown_seq": int(seq)}, f)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


# =========================================================================== #
# Tray icon artwork (drawn, not shipped as an asset)
# =========================================================================== #
def make_icon(color, badge=False):
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor(color)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(6, 6, 52, 52), 12, 12)
    p.setPen(QColor("#ffffff"))
    f = QFont()
    f.setBold(True)
    f.setPointSize(26)
    p.setFont(f)
    p.drawText(QRectF(6, 6, 52, 52), Qt.AlignCenter, "!" if badge else "L")
    p.end()
    return QIcon(pm)


ICON_OK = "#3d7a52"        # green   - nothing outstanding
ICON_WARN = "#b8860b"      # amber   - warnings present
ICON_CRIT = "#a62828"      # red     - URGENT / unacknowledged


# =========================================================================== #
# Alert list dialog (read-only)
# =========================================================================== #
class AlertsDialog(QDialog):
    """Read-only history. Shows every alert, dismissed or not. The tray can
    never modify these -- only the elevated GUI can acknowledge them."""

    def __init__(self, alerts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("logmon - Alert History")
        self.resize(720, 460)
        lay = QVBoxLayout(self)

        hdr = QLabel("Alerts recorded by the logmon service (read-only).\n"
                     "Dismissing notifications does not clear an alert - an "
                     "administrator must acknowledge it in logmon Settings.")
        hdr.setWordWrap(True)
        lay.addWidget(hdr)

        body = QTextBrowser()
        body.setOpenExternalLinks(False)
        if not alerts:
            body.setHtml("<p style='color:#666'>No alerts recorded.</p>")
        else:
            rows = []
            for a in reversed(alerts):      # newest first
                sev = a.get("severity", "")
                color = {"CRITICAL": "#a62828",
                         "WARNING": "#b8860b"}.get(sev, "#444")
                acked = a.get("kind") == "ACKNOWLEDGEMENT"
                rows.append(
                    "<div style='margin-bottom:12px;padding:8px;"
                    "border-left:4px solid %s;background:#fafafa'>"
                    "<b style='color:%s'>%s</b> &nbsp; <code>%s</code><br>"
                    "<small>#%s &nbsp; %s &nbsp; channel: <b>%s</b></small>"
                    "<div style='margin-top:4px'>%s</div></div>"
                    % (color, color, sev if not acked else "ACK",
                       a.get("kind", ""), a.get("seq", "?"),
                       a.get("time_utc", ""), a.get("channel", "-"),
                       a.get("detail", "")))
            body.setHtml("".join(rows))
        lay.addWidget(body)

        btns = QHBoxLayout()
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)


# =========================================================================== #
# The watcher
# =========================================================================== #
class TrayWatcher:
    def __init__(self, app):
        self.app = app
        # Build the tray WITH its icon in the constructor. Setting the icon
        # after construction can fail to register with the Windows 11 shell on
        # some builds (the entry appears iconless / not at all); passing it to
        # the constructor is reliable. refresh() updates it by state thereafter.
        self.tray = QSystemTrayIcon(make_icon(ICON_OK))
        self.menu = QMenu()
        self._alerts = []
        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activate)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)
        self.refresh(startup=True)

    # -- menu ------------------------------------------------------------- #
    def _build_menu(self):
        self.act_status = QAction("Checking...")
        self.act_status.setEnabled(False)
        self.menu.addAction(self.act_status)
        self.menu.addSeparator()

        self.act_view = QAction("View Alerts...")
        self.act_view.triggered.connect(self.show_alerts)
        self.menu.addAction(self.act_view)

        self.act_dismiss = QAction("Dismiss Notifications")
        self.act_dismiss.setToolTip(
            "Stop popping up for alerts you have already seen. This does NOT "
            "clear the alert - an administrator must acknowledge it.")
        self.act_dismiss.triggered.connect(self.dismiss)
        self.menu.addAction(self.act_dismiss)

        self.menu.addSeparator()
        self.act_settings = QAction("Open logmon Settings (Administrator)...")
        self.act_settings.triggered.connect(self.open_settings)
        self.menu.addAction(self.act_settings)

        self.act_autostart = QAction("Start with Windows")
        self.act_autostart.setCheckable(True)
        self.act_autostart.setChecked(self._autostart_enabled())
        self.act_autostart.triggered.connect(self.toggle_autostart)
        self.menu.addAction(self.act_autostart)

        self.menu.addSeparator()
        act_quit = QAction("Quit")
        act_quit.triggered.connect(self.app.quit)
        self.menu.addAction(act_quit)

    # -- state ------------------------------------------------------------ #
    def _undismissed(self):
        """Alerts newer than this user's high-water mark."""
        mark = read_marker()
        return [a for a in self._alerts
                if int(a.get("seq", 0)) > mark
                and a.get("kind") != "ACKNOWLEDGEMENT"]

    def refresh(self, startup=False):
        try:
            self._alerts = read_alerts()
        except Exception:
            self._alerts = []

        pending = self._undismissed()
        urgent = urgent_is_set()

        # ICON reflects the UNACKNOWLEDGED state, not the dismissed state.
        # Dismissing silences the popup; it must NOT clear the visible signal,
        # or a user could hide a tamper alert from the next admin who looks.
        if urgent:
            self.tray.setIcon(make_icon(ICON_CRIT, badge=True))
            status = "UNACKNOWLEDGED CRITICAL ALERT - admin action required"
        elif any(a.get("severity") == SEV_WARNING for a in self._alerts):
            self.tray.setIcon(make_icon(ICON_WARN))
            status = "%d warning(s) recorded" % sum(
                1 for a in self._alerts if a.get("severity") == SEV_WARNING)
        else:
            self.tray.setIcon(make_icon(ICON_OK))
            status = "logmon: no outstanding alerts"

        st = read_state()
        errs = st.get("config_errors") or []
        if errs:
            status += "  |  %d config error(s)" % len(errs)

        self.act_status.setText(status)
        self.tray.setToolTip("%s\n%s" % (APP_NAME, status))
        self.act_dismiss.setEnabled(bool(pending))

        if pending and not startup:
            self._notify(pending)
        elif pending and startup:
            self._notify(pending)

    def _notify(self, pending):
        crit = [a for a in pending if a.get("severity") == SEV_CRITICAL]
        top = (crit or pending)[-1]
        title = ("logmon: CRITICAL - %s" % top.get("kind", "alert")
                 if crit else "logmon: %s" % top.get("kind", "alert"))
        extra = ("\n(+%d more)" % (len(pending) - 1)) if len(pending) > 1 else ""
        msg = "%s\n%s%s" % (top.get("channel", ""),
                            (top.get("detail", "") or "")[:180], extra)
        icon = (QSystemTrayIcon.Critical if crit
                else QSystemTrayIcon.Warning)
        try:
            self.tray.showMessage(title, msg, icon, 15000)
        except Exception:
            pass

    # -- actions ---------------------------------------------------------- #
    def _on_activate(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show_alerts()

    def show_alerts(self):
        dlg = AlertsDialog(self._alerts)
        dlg.exec()

    def dismiss(self):
        """Per-user, unprivileged. Suppresses MY popups. Does not touch the
        alert store and does not clear URGENT.TXT."""
        if not self._alerts:
            return
        top = max(int(a.get("seq", 0)) for a in self._alerts)
        if write_marker(top):
            QMessageBox.information(
                None, "Notifications dismissed",
                "Notifications suppressed for alerts up to #%d.\n\n"
                "The alerts themselves are NOT cleared. They remain in the "
                "alert log, the tray icon stays red, and an administrator "
                "must acknowledge them in logmon Settings." % top)
        self.refresh(startup=True)

    def open_settings(self):
        """Launch the config GUI ELEVATED, on demand. UAC prompts here -- when
        the admin actually needs it -- rather than at every login."""
        gui = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "logmon_gui.py")
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        try:
            if os.name == "nt":
                import ctypes
                # ShellExecuteW verb 'runas' = request elevation.
                rc = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", exe, '"%s"' % gui,
                    os.path.dirname(gui), 1)
                if int(rc) <= 32:
                    raise OSError("ShellExecute returned %s" % rc)
            else:
                subprocess.Popen([sys.executable, gui])
        except Exception as exc:
            QMessageBox.warning(
                None, "Could not open logmon Settings",
                "logmon Settings could not be launched.\n\n%s\n\n"
                "(The settings GUI requires Administrator rights because it "
                "writes logmon.cfg in ProgramData.)" % exc)

    # -- autostart (HKCU: unprivileged) ----------------------------------- #
    def _autostart_cmd(self):
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        return '"%s" "%s"' % (exe, os.path.abspath(__file__))

    def _autostart_enabled(self):
        if os.name != "nt":
            return False
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, RUN_KEY_NAME)
            return True
        except Exception:
            return False

    def toggle_autostart(self, checked):
        if os.name != "nt":
            return
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_SET_VALUE) as k:
                if checked:
                    winreg.SetValueEx(k, RUN_KEY_NAME, 0, winreg.REG_SZ,
                                      self._autostart_cmd())
                else:
                    try:
                        winreg.DeleteValue(k, RUN_KEY_NAME)
                    except FileNotFoundError:
                        pass
        except Exception as exc:
            QMessageBox.warning(None, "Autostart",
                                "Could not update autostart: %s" % exc)
            self.act_autostart.setChecked(self._autostart_enabled())


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)   # closing a dialog must not exit

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME,
                             "No system tray is available on this desktop.")
        return 1

    # Single instance per user: a second tray icon would double every popup.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    name = "logmon_tray_%s" % (os.environ.get("USERNAME", "user"))
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(200):
        return 0                            # already running
    server = QLocalServer()
    QLocalServer.removeServer(name)
    server.listen(name)

    watcher = TrayWatcher(app)              # MUST hold a reference: if this is
    app._logmon_watcher = watcher           # GC'd, self.tray dies and the icon
    return app.exec()                       # vanishes while the loop keeps running


if __name__ == "__main__":
    raise SystemExit(main())
