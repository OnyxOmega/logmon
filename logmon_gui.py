"""logmon_gui.py -- logmon configuration GUI (Administrator).  YASDC

The ELEVATED, on-demand, admin half of logmon's user surface. The companion
`logmon_tray.py` is the unprivileged always-on watcher.

SCHEMA v2 (2026-07-15): configuration is PROVIDER + CHANNEL, not bundles.
  * The Channels tab is the SINGLE editor (the Bundles tab is gone).
  * The tree mirrors Event Viewer -- "Windows Logs" (the 5 classics) and
    "Applications and Services Logs" (everything else, auto-foldered by
    splitting the flat channel name on '-' and '/'). The tree is DERIVED from
    the channel list every load; it is never stored.
  * Policy attaches to two OS-real objects only: PROVIDER (a channel's
    owningPublisher, derived here as the name before the last '/', or the
    Event-Viewer root when there is none) and CHANNEL. A channel override wins
    over its provider default, which wins over the global default (1M / 1y).

OWNERSHIP / GUI CONTRACT (design lock 3.2.3) -- enforced here:
  1. This app is the SOLE writer of logmon.cfg, written ATOMICALLY.
  2. It NEVER writes logmon_state.json or logmon.cfg.bak (service-owned).
  3. The service never writes logmon.cfg after install; no lost-update race.
  4. All timestamps are UTC.

ALERTS -- ACKNOWLEDGE, NEVER DELETE (unchanged): acknowledging APPENDS a record
and clears URGENT.TXT; it never truncates alerts.jsonl.

VALIDATION IS DUPLICATED, DELIBERATELY (design lock 11/13.1): the checks here
mirror logmon.validate_config rather than importing it. The service stays
authoritative and republishes problems to state.config_errors.

logmon NEVER alters OS Event Log settings (HARD RULE 3). Hostile OS config is
FLAGGED (red), never "fixed".
"""

import ctypes
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QProgressDialog, QPushButton, QTabWidget, QTextBrowser,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

APP_TITLE = "logmon Settings (Administrator)"
CONFIG_SCHEMA_VERSION = 2

RECOMMENDED_MAX_SIZE = 4 * 1024 * 1024 * 1024      # 4 GiB; below -> RED flag

COLOR_BAD = QColor("#ffd6d6")
COLOR_WARN = QColor("#fff0cc")
COLOR_OK = QColor("#e6f4ea")
COLOR_MUTED = QColor("#f0f0f0")

CLASSIC_LOGS = ["Application", "Security", "System", "Setup", "ForwardedEvents"]
ROOT_WINDOWS = "Windows Logs"
ROOT_APPSVC = "Applications and Services Logs"

POLICY_KEYS = ("rotate", "timeframe", "legal_retention", "size_limit_bytes")
GLOBAL_DEFAULTS = {"rotate": True, "timeframe": "1M", "legal_retention": "1y",
                   "size_limit_bytes": None}


# =========================================================================== #
# Paths / IO  (standalone: does NOT import logmon.py)
# =========================================================================== #
def logmon_dir():
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, "logmon")


def config_path():
    return os.path.join(logmon_dir(), "logmon.cfg")


def state_path():
    return os.path.join(logmon_dir(), "logmon_state.json")


def alerts_path():
    return os.path.join(logmon_dir(), "alerts.jsonl")


def urgent_path():
    return os.path.join(logmon_dir(), "URGENT.TXT")


def now_utc_str():
    return (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            + "Z")


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except Exception:
        return default


def read_config():
    return read_json(config_path(), {})


def read_state():
    return read_json(state_path(), {})


def read_alerts():
    out = []
    try:
        with open(alerts_path(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        return []
    return out


def write_config_atomic(cfg):
    """GUI CONTRACT rule 1: atomic, always."""
    p = config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return os.name != "nt"


def fmt_bytes(n):
    if not n:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return str(n)


# =========================================================================== #
# wevtutil readers (read-only)
# =========================================================================== #
def enumerate_channels():
    try:
        out = subprocess.run(["wevtutil", "el"], capture_output=True,
                             text=True, timeout=60)
        if out.returncode != 0:
            return []
        return sorted(l.strip() for l in out.stdout.splitlines() if l.strip())
    except Exception:
        return []


def channel_os_config(channel):
    """Read-only `wevtutil gl`. Returns {} on failure."""
    try:
        out = subprocess.run(["wevtutil", "gl", channel], capture_output=True,
                             text=True, timeout=20)
        if out.returncode != 0:
            return {}
        md = {}
        for line in out.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                md[k.strip()] = v.strip()

        def _b(k):
            return str(md.get(k, "")).strip().lower() == "true"
        try:
            ms = int(md.get("maxSize", 0))
        except Exception:
            ms = 0
        return {"enabled": _b("enabled"), "type": md.get("type", ""),
                "retention": _b("retention"), "auto_backup": _b("autoBackup"),
                "max_size": ms,
                "owning_publisher": md.get("owningPublisher", "")}
    except Exception:
        return {}


class ChannelScanner(QThread):
    """`wevtutil gl` is one subprocess PER CHANNEL (~1,270), so scanning must
    never run on the UI thread."""
    progress = Signal(int, int, str)
    done = Signal(dict)

    def __init__(self, channels):
        super().__init__()
        self._channels = channels
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        res = {}
        n = len(self._channels)
        for i, ch in enumerate(self._channels):
            if self._cancel:
                break
            res[ch] = channel_os_config(ch)
            self.progress.emit(i + 1, n, ch)
        self.done.emit(res)


# =========================================================================== #
# Provider derivation + display tree (DERIVED, never stored)
# =========================================================================== #
def channel_provider(chan):
    """The provider (owningPublisher) a channel's policy is stored under.
    Derived from the name so no OS scan is needed:
      - classic five            -> 'Windows Logs'
      - name contains '/'       -> the part before the last '/' (owningPublisher)
      - otherwise               -> 'Applications and Services Logs'
    """
    if chan in CLASSIC_LOGS:
        return ROOT_WINDOWS
    if "/" in chan:
        return chan.rsplit("/", 1)[0]
    return ROOT_APPSVC


def channel_tree_path(chan):
    """Return (root, [folder segments], leaf_label) for the Event-Viewer tree.
    Splits the flat channel name on '-' and '/', exactly like Event Viewer."""
    if chan in CLASSIC_LOGS:
        return ROOT_WINDOWS, [], chan
    segs = [s for s in re.split(r"[-/]", chan) if s]
    if len(segs) <= 1:
        return ROOT_APPSVC, [], chan
    return ROOT_APPSVC, segs[:-1], segs[-1]


# =========================================================================== #
# Policy validation + resolution (mirror logmon; see docstring)
# =========================================================================== #
ROTATION_CAPS = {"s": 172800, "m": 2880, "h": 336, "d": 180, "w": 52,
                 "t": 12, "M": 12, "y": 1}
RETENTION_CAPS = {"d": 3650, "w": 520, "t": 120, "M": 300, "y": 25}


def _parse_period(text, caps):
    if not text or not str(text).strip():
        return None
    mo = re.fullmatch(r"\s*([0-9]{1,5})\s*([smhdwtMy])\s*", str(text))
    if not mo:
        return None
    n, unit = int(mo.group(1)), mo.group(2)
    if unit not in caps or n < 1 or n > caps[unit]:
        return None
    return (n, unit)


def valid_rotation(tf):
    return _parse_period(tf, ROTATION_CAPS) is not None


def valid_retention(lr):
    return (not lr) or (_parse_period(lr, RETENTION_CAPS) is not None)


def effective_policy(cfg, chan):
    """Resolve per key: channel override -> provider default -> global default.
    Returns {key: (value, source)} where source in {'channel','provider',
    'global'}."""
    prov = channel_provider(chan)
    p = (cfg.get("providers", {}) or {}).get(prov, {})
    c = (p.get("channels", {}) or {}).get(chan)
    gd = dict(GLOBAL_DEFAULTS)
    gd.update(cfg.get("defaults") or {})
    pd = p.get("defaults", {}) or {}
    out = {}
    for k in POLICY_KEYS:
        if c and k in c:
            out[k] = (c[k], "channel")
        elif k in pd:
            out[k] = (pd[k], "provider")
        else:
            out[k] = (gd.get(k), "global")
    return out


def channel_cfg_entry(cfg, chan):
    prov = channel_provider(chan)
    p = (cfg.get("providers", {}) or {}).get(prov, {})
    return (p.get("channels", {}) or {}).get(chan)


def channel_active(cfg, chan):
    prov = channel_provider(chan)
    p = (cfg.get("providers", {}) or {}).get(prov, {})
    c = (p.get("channels", {}) or {}).get(chan)
    if not c:
        return False
    return bool(c.get("enabled", True)) and bool(p.get("enabled", True))


def set_channel_policy(cfg, chan, enabled, rot_override, ret_override):
    """Write a channel's policy into providers[provider][channels][chan].
    rot_override/ret_override: string to set as override, or None to inherit
    (key omitted). Empty-string retention override means 'keep forever' (an
    explicit override), distinct from None (inherit)."""
    prov = channel_provider(chan)
    providers = cfg.setdefault("providers", {})
    p = providers.setdefault(prov, {"enabled": True, "defaults": {},
                                    "channels": {}})
    p.setdefault("channels", {})
    c = {"enabled": bool(enabled)}
    if rot_override is not None:
        c["timeframe"] = rot_override
    if ret_override is not None:
        c["legal_retention"] = ret_override
    p["channels"][chan] = c


def remove_channel_policy(cfg, chan):
    prov = channel_provider(chan)
    p = (cfg.get("providers", {}) or {}).get(prov, {})
    if p and chan in p.get("channels", {}):
        del p["channels"][chan]


def set_provider_defaults(cfg, provider, rot_default, ret_default, enabled):
    providers = cfg.setdefault("providers", {})
    p = providers.setdefault(provider, {"enabled": True, "defaults": {},
                                        "channels": {}})
    p["enabled"] = bool(enabled)
    d = {}
    if rot_default is not None:
        d["timeframe"] = rot_default
    if ret_default is not None:
        d["legal_retention"] = ret_default
    p["defaults"] = d


def provider_defaults(cfg, provider):
    p = (cfg.get("providers", {}) or {}).get(provider, {})
    return p.get("defaults", {}) or {}, bool(p.get("enabled", True))


# =========================================================================== #
# Curated recommendations (schema v2): channel -> (rotation, retention).
# Explicit channels only (logmon CLEARS what it archives; a false match is
# destructive). Only channels that EXIST on the box are applied.
# =========================================================================== #
CURATED_CHANNELS = {
    "Security": ("1M", "7y"),
    "System": ("1M", "1y"),
    "Application": ("1M", "1y"),
    "Setup": ("1M", "1y"),
    "ForwardedEvents": ("1M", "1y"),
    "Windows PowerShell": ("1M", "7y"),
    "Microsoft-Windows-PowerShell/Operational": ("1M", "7y"),
    "Microsoft-Windows-LSA/Operational": ("1M", "7y"),
    "Microsoft-Windows-NTLM/Operational": ("1M", "7y"),
    "Microsoft-Windows-Windows Defender/Operational": ("1M", "7y"),
    "Microsoft-Windows-CodeIntegrity/Operational": ("1M", "7y"),
    "Microsoft-Windows-AppLocker/EXE and DLL": ("1M", "7y"),
    "Microsoft-Windows-AppLocker/MSI and Script": ("1M", "7y"),
    "Microsoft-Windows-TaskScheduler/Operational": ("1M", "7y"),
    "Microsoft-Windows-WMI-Activity/Operational": ("1M", "7y"),
    "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational":
        ("1M", "7y"),
    "Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational":
        ("1M", "7y"),
    "Microsoft-Windows-SMBServer/Security": ("1M", "1y"),
    "Microsoft-Windows-SMBServer/Operational": ("1M", "1y"),
    "Microsoft-Windows-SMBClient/Operational": ("1M", "1y"),
    "Microsoft-Windows-SmbClient/Security": ("1M", "1y"),
    "Microsoft-Windows-GroupPolicy/Operational": ("1M", "1y"),
    "Microsoft-Windows-User Profile Service/Operational": ("1M", "1y"),
    "Microsoft-Windows-Kernel-PnP/Configuration": ("1M", "1y"),
    "Microsoft-Windows-PrintService/Operational": ("1M", "1y"),
    "Microsoft-Windows-PrintService/Admin": ("1M", "1y"),
}


# =========================================================================== #
# Channels tab -- the single editor (Bundles tab removed, schema v2)
# =========================================================================== #
COLS = ["Name", "Provider", "Enabled", "Type", "Max size", "OS retention",
        "Rotation", "Retention", "Status"]
(C_NAME, C_PROV, C_EN, C_TYPE, C_MAX, C_OSRET, C_ROT, C_RET, C_STATUS) = \
    range(len(COLS))

TYPE_CHOICES = ["Admin", "Operational", "Analytic", "Debug", "(unknown)"]
STATUS_CHOICES = ["OK", "unconfigured", "disabled", "RED"]

# Tree-item data roles. UserRole on col 0 holds a leaf's full channel name
# (None for folders/roots). PROV_ROLE marks a node as an editable PROVIDER --
# set on the two Event-Viewer roots, which ARE providers in the policy model.
PROV_ROLE = Qt.UserRole + 1


class _MultiFilter(QToolButton):
    """A dropdown of checkable options for column filtering (Type, Status)."""
    def __init__(self, label, choices, on_change):
        super().__init__()
        self.setText(label)
        self.setPopupMode(QToolButton.InstantPopup)
        self._menu = QMenu(self)
        self._acts = {}
        for c in choices:
            a = QAction(c, self, checkable=True)
            a.setChecked(True)
            a.triggered.connect(lambda _=False: on_change())
            self._menu.addAction(a)
            self._acts[c] = a
        self.setMenu(self._menu)

    def selected(self):
        return {c for c, a in self._acts.items() if a.isChecked()}


class ChannelsTab(QWidget):
    def __init__(self, parent_win):
        super().__init__()
        self.win = parent_win
        self.osinfo = {}
        self._leaves = []           # (QTreeWidgetItem, channel_name)

        v = QVBoxLayout(self)
        note = QLabel(
            "<b>logmon never changes OS log settings.</b> It reads and reports "
            "them. Rows in <span style='background:#ffd6d6'>red</span> have an "
            "OS configuration that will lose events logmon cannot prevent -- "
            "fix those in Group Policy / Event Viewer. Policy is set per "
            "<b>channel</b>, or per <b>provider</b> (inherited by its channels).")
        note.setWordWrap(True)
        v.addWidget(note)

        # -- filter row --
        row = QHBoxLayout()
        row.addWidget(QLabel("Enabled:"))
        self.cmb_enabled = QComboBox()
        self.cmb_enabled.addItems(["Both", "Yes", "No"])
        self.cmb_enabled.currentIndexChanged.connect(self._apply_filters)
        row.addWidget(self.cmb_enabled)
        self.f_type = _MultiFilter("Type", TYPE_CHOICES, self._apply_filters)
        row.addWidget(self.f_type)
        self.f_status = _MultiFilter("Status", STATUS_CHOICES,
                                     self._apply_filters)
        row.addWidget(self.f_status)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("Search (starts with)...")
        self.ed_search.textChanged.connect(self._apply_filters)
        row.addWidget(self.ed_search, 2)
        b_scan = QPushButton("Scan OS settings")
        b_scan.clicked.connect(self.scan)
        row.addWidget(b_scan)
        b_rec = QPushButton("Add Recommended")
        b_rec.clicked.connect(self.add_recommended)
        row.addWidget(b_rec)
        v.addLayout(row)

        # -- tree --
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setSectionResizeMode(C_NAME, QHeaderView.Stretch)
        self.tree.itemSelectionChanged.connect(self._on_select)
        v.addWidget(self.tree, 1)

        # -- edit panel --
        self.panel = self._build_panel()
        v.addWidget(self.panel)

        self.lbl = QLabel("")
        v.addWidget(self.lbl)

    # ------------------------------------------------------------------ #
    def _build_panel(self):
        gb = QGroupBox("Policy for selected channel(s)")
        outer = QVBoxLayout(gb)

        self.sel_label = QLabel("Select a channel in the tree.")
        self.sel_label.setWordWrap(True)
        outer.addWidget(self.sel_label)

        row1 = QHBoxLayout()
        self.chk_enabled = QCheckBox("Archive this channel (enabled)")
        row1.addWidget(self.chk_enabled)
        row1.addStretch(1)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        self.chk_rot = QCheckBox("Override rotation:")
        self.ed_rot = QLineEdit()
        self.ed_rot.setPlaceholderText("e.g. 1M, 10d, 6h")
        self.chk_ret = QCheckBox("Override retention:")
        self.ed_ret = QLineEdit()
        self.ed_ret.setPlaceholderText("e.g. 7y, 90d; empty = keep forever")
        row2.addWidget(self.chk_rot)
        row2.addWidget(self.ed_rot)
        row2.addSpacing(16)
        row2.addWidget(self.chk_ret)
        row2.addWidget(self.ed_ret)
        outer.addLayout(row2)

        self.eff_label = QLabel("")
        self.eff_label.setStyleSheet("color:#555")
        outer.addWidget(self.eff_label)

        row3 = QHBoxLayout()
        self.b_apply_channel = QPushButton("Apply to selected")
        self.b_apply_channel.clicked.connect(self.apply_channel)
        self.b_remove_channel = QPushButton("Remove from config (stop "
                                            "archiving)")
        self.b_remove_channel.clicked.connect(self.remove_channel)
        row3.addWidget(self.b_apply_channel)
        row3.addWidget(self.b_remove_channel)
        row3.addStretch(1)
        outer.addLayout(row3)
        # channel-only widgets, toggled off when a provider node is selected
        self._channel_widgets = [
            self.chk_enabled, self.chk_rot, self.ed_rot, self.chk_ret,
            self.ed_ret, self.b_apply_channel, self.b_remove_channel]

        # provider defaults sub-section
        self.prov_box = QGroupBox("Provider default (applies to all channels "
                                  "under this provider unless overridden)")
        pv = QHBoxLayout(self.prov_box)
        self.chk_prov_en = QCheckBox("Provider enabled")
        pv.addWidget(self.chk_prov_en)
        pv.addWidget(QLabel("Rotation:"))
        self.ed_prov_rot = QLineEdit()
        self.ed_prov_rot.setPlaceholderText("inherit global")
        pv.addWidget(self.ed_prov_rot)
        pv.addWidget(QLabel("Retention:"))
        self.ed_prov_ret = QLineEdit()
        self.ed_prov_ret.setPlaceholderText("inherit global")
        pv.addWidget(self.ed_prov_ret)
        b_prov = QPushButton("Apply provider default")
        b_prov.clicked.connect(self.apply_provider)
        pv.addWidget(b_prov)
        outer.addWidget(self.prov_box)
        # Shown when a provider/root is selected: the channels under it that
        # carry their own override and therefore will NOT follow a change to
        # this group's default (the compliance "here's what won't move" list).
        self.prov_info = QLabel("")
        self.prov_info.setWordWrap(True)
        self.prov_info.setStyleSheet("color:#555")
        outer.addWidget(self.prov_info)
        return gb

    # ------------------------------------------------------------------ #
    def reload(self):
        cfg = self.win.cfg
        cs = self.win.state.get("channel_state", {})
        for ch, d in cs.items():
            if d.get("os_config") and ch not in self.osinfo:
                self.osinfo[ch] = d["os_config"]

        self.tree.clear()
        self._leaves = []
        roots = {}

        def get_root(name):
            if name not in roots:
                it = QTreeWidgetItem([name])
                f = it.font(C_NAME)
                f.setBold(True)
                it.setFont(C_NAME, f)
                # The two roots ARE providers ("Windows Logs" /
                # "Applications and Services Logs"). Mark them so selecting a
                # root lets the operator set that group's global defaults.
                it.setData(C_NAME, PROV_ROLE, name)
                it.setText(C_PROV, "(provider — select to set group defaults)")
                self.tree.addTopLevelItem(it)
                roots[name] = it
            return roots[name]

        # deterministic order: classic root first
        get_root(ROOT_WINDOWS)
        get_root(ROOT_APPSVC)

        red = warn = 0
        for ch in self.win.all_channels:
            root_name, folders, leaf = channel_tree_path(ch)
            parent = get_root(root_name)
            for seg in folders:
                child = None
                for i in range(parent.childCount()):
                    if (parent.child(i).text(C_NAME) == seg
                            and parent.child(i).data(C_NAME, Qt.UserRole)
                            is None):
                        child = parent.child(i)
                        break
                if child is None:
                    child = QTreeWidgetItem([seg])
                    parent.addChild(child)
                parent = child
            item = QTreeWidgetItem([leaf])
            item.setData(C_NAME, Qt.UserRole, ch)     # full channel name = leaf
            parent.addChild(item)
            self._leaves.append((item, ch))
            st = self._fill_leaf(item, ch)
            if st == "RED":
                red += 1
            elif st == "unconfigured":
                warn += 1

        self.tree.expandToDepth(0)
        self.lbl.setText(
            "%d channels  |  %d flagged RED (OS config will lose events)  |  "
            "%d unconfigured" % (len(self.win.all_channels), red, warn))
        self._apply_filters()

    def _fill_leaf(self, item, ch):
        cfg = self.win.cfg
        osc = self.osinfo.get(ch, {})
        prov = channel_provider(ch)
        eff = effective_policy(cfg, ch)
        active = channel_active(cfg, ch)
        configured = channel_cfg_entry(cfg, ch) is not None

        def eff_txt(key):
            val, src = eff[key]
            val = "keep forever" if (key == "legal_retention" and not val) \
                else (val if val else "-")
            return "%s%s" % (val, "" if src == "channel" else "  (inh)")

        status, color = "unconfigured", COLOR_WARN
        if osc and not osc.get("enabled", True):
            status, color = "disabled", COLOR_MUTED
        elif configured and active:
            if osc and osc.get("max_size", 0) < RECOMMENDED_MAX_SIZE \
                    and osc.get("max_size", 0) > 0:
                status, color = "RED", COLOR_BAD
            else:
                status, color = "OK", COLOR_OK
        elif configured and not active:
            status, color = "disabled", COLOR_MUTED

        vals = {
            C_PROV: prov,
            C_EN: ("yes" if active else "no"),
            C_TYPE: (osc.get("type") or "(unknown)") if osc else "(unknown)",
            C_MAX: fmt_bytes(osc.get("max_size", 0)) if osc else "?",
            C_OSRET: ("circular overwrite" if osc and not osc.get("retention")
                      else "stop when full" if osc else "?"),
            C_ROT: eff_txt("timeframe"),
            C_RET: eff_txt("legal_retention"),
            C_STATUS: status,
        }
        for col, txt in vals.items():
            item.setText(col, str(txt))
        for col in range(len(COLS)):
            item.setBackground(col, QBrush(color))
        # Make channel-level OVERRIDES visually distinct: bold the rotation /
        # retention cell when the value comes from the channel itself (not
        # inherited). These are exactly the channels a group-default change will
        # NOT move -- see the provider editor's "won't move" list.
        for col, key in ((C_ROT, "timeframe"), (C_RET, "legal_retention")):
            if eff[key][1] == "channel":
                f = item.font(col)
                f.setBold(True)
                item.setFont(col, f)
        item.setData(C_STATUS, Qt.UserRole, status)
        return status

    # ------------------------------------------------------------------ #
    def _selected_channels(self):
        out = []
        for it in self.tree.selectedItems():
            ch = it.data(C_NAME, Qt.UserRole)
            if ch:
                out.append(ch)
        return out

    def _provider_override_summary(self, prov):
        """Return HTML describing which channels under provider `prov` carry
        their own rotation/retention override -- the channels a change to this
        group's default will NOT move."""
        chans = (self.win.cfg.get("providers", {})
                 .get(prov, {}).get("channels", {}))
        rows = []
        for ch in sorted(chans):
            entry = chans[ch] or {}
            parts = []
            if "timeframe" in entry:
                parts.append("rotation %s" % entry["timeframe"])
            if "legal_retention" in entry:
                parts.append("retention %s" % (entry["legal_retention"]
                                               or "keep forever"))
            if parts:
                rows.append("&nbsp;&nbsp;• <b>%s</b> — %s"
                            % (ch, ", ".join(parts)))
        if not rows:
            return ("<small>No channels in this group override its defaults — "
                    "a change here applies to every channel under it.</small>")
        return ("<small><b>%d channel(s) override this group's default</b> and "
                "will NOT change when you edit it (overrides are deliberate and "
                "persist):<br>%s</small>" % (len(rows), "<br>".join(rows)))

    def _selected_provider_node(self):
        """If exactly one selected item is a provider node (a marked root),
        return its provider name; else None."""
        items = self.tree.selectedItems()
        if len(items) == 1:
            p = items[0].data(C_NAME, PROV_ROLE)
            if p and items[0].data(C_NAME, Qt.UserRole) is None:
                return p
        return None

    def _set_channel_section_enabled(self, on):
        for w in self._channel_widgets:
            w.setEnabled(on)

    def _on_select(self):
        # Case 1: a provider node (one of the two roots) is selected -> edit
        # that group's global defaults; the channel section does not apply.
        prov_node = self._selected_provider_node()
        if prov_node is not None:
            self._prov_context = prov_node
            self._set_channel_section_enabled(False)
            self.prov_box.setEnabled(True)
            self.prov_box.setTitle(
                "Global defaults for \"%s\" (inherited by its channels unless "
                "a channel overrides)" % prov_node)
            self.sel_label.setText(
                "<b>%s</b><br><small>provider group — set rotation/retention "
                "defaults here; every channel under it inherits unless it has "
                "its own override.</small>" % prov_node)
            self.eff_label.setText("")
            pd, pen = provider_defaults(self.win.cfg, prov_node)
            self.chk_prov_en.setChecked(pen)
            self.ed_prov_rot.setText(pd.get("timeframe", ""))
            self.ed_prov_ret.setText(pd.get("legal_retention", ""))
            self.prov_info.setText(
                self._provider_override_summary(prov_node))
            return

        # Case 2: channel(s) selected -> channel editor + the channel's provider
        # defaults.
        chans = self._selected_channels()
        self._set_channel_section_enabled(True)
        self.prov_info.setText("")
        self.prov_box.setTitle("Provider default (applies to all channels "
                               "under this provider unless overridden)")
        if not chans:
            self._prov_context = None
            self.sel_label.setText("Select a channel, or a top-level group "
                                   "(Windows Logs / Applications and Services "
                                   "Logs) to set group defaults.")
            self.eff_label.setText("")
            self.prov_box.setEnabled(False)
            return
        self.prov_box.setEnabled(True)
        ch = chans[0]
        cfg = self.win.cfg
        c = channel_cfg_entry(cfg, ch) or {}
        eff = effective_policy(cfg, ch)
        prov = channel_provider(ch)
        self._prov_context = prov
        if len(chans) == 1:
            self.sel_label.setText(
                "<b>%s</b><br><small>provider: %s</small>" % (ch, prov))
        else:
            self.sel_label.setText(
                "<b>%d channels selected.</b> Apply will set all of them "
                "(shown: %s)." % (len(chans), ch))
        self.chk_enabled.setChecked(bool(c.get("enabled", True))
                                    if c else True)
        self.chk_rot.setChecked("timeframe" in c)
        self.ed_rot.setText(c.get("timeframe", "") if c else "")
        self.chk_ret.setChecked("legal_retention" in c)
        self.ed_ret.setText(c.get("legal_retention", "") if c else "")
        self.eff_label.setText(
            "Effective now: rotation <b>%s</b> (%s), retention <b>%s</b> (%s)"
            % (eff["timeframe"][0], eff["timeframe"][1],
               eff["legal_retention"][0] or "keep forever",
               eff["legal_retention"][1]))
        pd, pen = provider_defaults(cfg, prov)
        self.chk_prov_en.setChecked(pen)
        self.ed_prov_rot.setText(pd.get("timeframe", ""))
        self.ed_prov_ret.setText(pd.get("legal_retention", ""))

    def apply_channel(self):
        chans = self._selected_channels()
        if not chans:
            return
        rot = self.ed_rot.text().strip() if self.chk_rot.isChecked() else None
        ret = self.ed_ret.text().strip() if self.chk_ret.isChecked() else None
        if rot is not None and not valid_rotation(rot):
            QMessageBox.warning(self, "Invalid rotation",
                                "Rotation %r is not valid (e.g. 1M, 10d, 6h)."
                                % rot)
            return
        if ret is not None and not valid_retention(ret):
            QMessageBox.warning(self, "Invalid retention",
                                "Retention %r is not valid (e.g. 7y, 90d; "
                                "empty = keep forever)." % ret)
            return
        for ch in chans:
            set_channel_policy(self.win.cfg, ch, self.chk_enabled.isChecked(),
                               rot, ret)
        self.win.mark_dirty()
        self.reload()

    def remove_channel(self):
        chans = self._selected_channels()
        if not chans:
            return
        if QMessageBox.question(
                self, "Remove from config",
                "Stop archiving %d channel(s)? Existing archives are kept."
                % len(chans)) != QMessageBox.Yes:
            return
        for ch in chans:
            remove_channel_policy(self.win.cfg, ch)
        self.win.mark_dirty()
        self.reload()

    def apply_provider(self):
        prov = getattr(self, "_prov_context", None)
        if not prov:
            return
        rot = self.ed_prov_rot.text().strip() or None
        ret = self.ed_prov_ret.text().strip() or None
        if rot is not None and not valid_rotation(rot):
            QMessageBox.warning(self, "Invalid rotation", "Bad rotation.")
            return
        if ret is not None and not valid_retention(ret):
            QMessageBox.warning(self, "Invalid retention", "Bad retention.")
            return
        set_provider_defaults(self.win.cfg, prov, rot, ret,
                              self.chk_prov_en.isChecked())
        self.win.mark_dirty()
        self.reload()

    # ------------------------------------------------------------------ #
    def _apply_filters(self):
        want_en = self.cmb_enabled.currentText()
        types = self.f_type.selected()
        statuses = self.f_status.selected()
        prefix = self.ed_search.text().strip().lower()
        for item, ch in self._leaves:
            osc = self.osinfo.get(ch, {})
            active = channel_active(self.win.cfg, ch)
            typ = (osc.get("type") or "(unknown)") if osc else "(unknown)"
            typ = typ if typ in TYPE_CHOICES else "(unknown)"
            status = item.data(C_STATUS, Qt.UserRole) or "unconfigured"
            show = True
            if want_en == "Yes" and not active:
                show = False
            if want_en == "No" and active:
                show = False
            if typ not in types:
                show = False
            if status not in statuses:
                show = False
            if prefix and not ch.lower().startswith(prefix):
                show = False
            item.setHidden(not show)
        # hide folder/root nodes with no visible leaf descendants
        for i in range(self.tree.topLevelItemCount()):
            self._prune_empty(self.tree.topLevelItem(i))

    def _prune_empty(self, node):
        if node.data(C_NAME, Qt.UserRole) is not None:
            return not node.isHidden()          # a leaf
        any_visible = False
        for i in range(node.childCount()):
            if self._prune_empty(node.child(i)):
                any_visible = True
        node.setHidden(not any_visible)
        return any_visible

    # ------------------------------------------------------------------ #
    def scan(self, background=False):
        """Read OS settings for every channel via `wevtutil gl` (threaded).
        background=True runs quietly (no modal dialog) for the automatic scan on
        load; the manual Scan button runs with a cancelable progress dialog."""
        chans = self.win.all_channels
        if not chans:
            if not background:
                QMessageBox.warning(self, "Scan", "No channels enumerated.")
            return
        if getattr(self, "_scanner", None) is not None \
                and self._scanner.isRunning():
            return                              # a scan is already running
        self._scanner = ChannelScanner(chans)

        if background:
            self.lbl.setText("Scanning OS settings in the background...")

            def bg_progress(i, n, ch):
                if i % 50 == 0 or i == n:
                    self.lbl.setText("Scanning OS settings... (%d/%d)" % (i, n))
            self._scanner.progress.connect(bg_progress)

            def bg_finished(res):
                self.osinfo.update({k: v for k, v in res.items() if v})
                self.reload()
            self._scanner.done.connect(bg_finished)
            self._scanner.start()
            return

        dlg = QProgressDialog("Reading OS settings (wevtutil gl)...",
                              "Cancel", 0, len(chans), self)
        dlg.setWindowModality(Qt.WindowModal)
        self._scanner.progress.connect(
            lambda i, n, ch: (dlg.setValue(i),
                              dlg.setLabelText("Reading %s (%d/%d)"
                                               % (ch, i, n))))
        dlg.canceled.connect(self._scanner.cancel)

        def finished(res):
            self.osinfo.update({k: v for k, v in res.items() if v})
            dlg.close()
            self.reload()
        self._scanner.done.connect(finished)
        self._scanner.start()

    def auto_scan_if_needed(self):
        """Kick off a one-time background OS scan the first time we have a
        channel list but no OS info yet. Cheap on later loads (osinfo cached)."""
        if not self.osinfo and self.win.all_channels:
            self.scan(background=True)

    def add_recommended(self):
        live = set(self.win.all_channels)
        if not live:
            QMessageBox.warning(self, "Recommended",
                                "No channels enumerated (run on Windows).")
            return
        added = 0
        for ch, (rot, ret) in CURATED_CHANNELS.items():
            if ch not in live:
                continue
            if channel_cfg_entry(self.win.cfg, ch) is not None:
                continue
            set_channel_policy(self.win.cfg, ch, True, rot, ret)
            added += 1
        self.win.mark_dirty()
        self.reload()
        QMessageBox.information(
            self, "Recommended",
            "Added %d recommended channel(s) that exist on this machine.\n"
            "Review rotation/retention before saving. logmon will ARCHIVE and "
            "CLEAR these on their schedule." % added)


class AlertsTab(QWidget):
    def __init__(self, parent_win):
        super().__init__()
        self.win = parent_win
        v = QVBoxLayout(self)
        self.body = QTextBrowser()
        v.addWidget(self.body)
        row = QHBoxLayout()
        self.lbl = QLabel("")
        row.addWidget(self.lbl, 1)
        b_ack = QPushButton("Acknowledge all alerts")
        b_ack.clicked.connect(self.acknowledge)
        row.addWidget(b_ack)
        v.addLayout(row)

    def reload(self):
        alerts = read_alerts()
        urgent = os.path.exists(urgent_path())
        acked = max([int(a.get("data", {}).get("acknowledged_through", 0))
                     for a in alerts
                     if a.get("kind") == "ACKNOWLEDGEMENT"] or [0])
        rows = []
        for a in reversed(alerts):
            sev = a.get("severity", "")
            kind = a.get("kind", "")
            is_ack = kind == "ACKNOWLEDGEMENT"
            color = {"CRITICAL": "#a62828", "WARNING": "#b8860b"}.get(
                sev, "#3d7a52")
            seq = int(a.get("seq", 0))
            tag = ""
            if not is_ack and seq <= acked:
                tag = ("<span style='color:#3d7a52'>&nbsp;[acknowledged]"
                       "</span>")
            rows.append(
                "<div style='margin-bottom:10px;padding:8px;border-left:4px "
                "solid %s;background:#fafafa'><b style='color:%s'>%s</b> "
                "<code>%s</code>%s<br><small>#%s &nbsp; %s &nbsp; %s</small>"
                "<div>%s</div></div>"
                % (color, color, "ACK" if is_ack else sev, kind, tag,
                   a.get("seq", "?"), a.get("time_utc", ""),
                   a.get("channel", "-"), a.get("detail", "")))
        self.body.setHtml("".join(rows) or
                          "<p style='color:#666'>No alerts recorded.</p>")
        self.lbl.setText(
            ("<b style='color:#a62828'>URGENT: unacknowledged alert(s)</b>"
             if urgent else "No outstanding alerts.")
            + "  (%d record(s))" % len(alerts))

    def acknowledge(self):
        alerts = [a for a in read_alerts()
                  if a.get("kind") != "ACKNOWLEDGEMENT"]
        if not alerts:
            QMessageBox.information(self, "Acknowledge", "No alerts.")
            return
        top = max(int(a.get("seq", 0)) for a in alerts)
        who = os.environ.get("USERNAME", "unknown")
        if QMessageBox.question(
                self, "Acknowledge alerts",
                "Acknowledge all alerts through #%d as '%s'?\n\n"
                "This APPENDS an acknowledgement record and clears the URGENT "
                "flag.\nIt does NOT delete any alert - the record of what "
                "happened is preserved." % (top, who)) != QMessageBox.Yes:
            return
        rec = {"seq": top + 1, "time_utc": now_utc_str(), "severity": "INFO",
               "kind": "ACKNOWLEDGEMENT", "channel": "-",
               "detail": "Alerts through #%d acknowledged by %s" % (top, who),
               "data": {"acknowledged_through": top, "acknowledged_by": who}}
        try:
            # APPEND ONLY. Never truncate: destroying the record of a tamper is
            # the exact erasure the ACLs exist to prevent.
            with open(alerts_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(urgent_path()):
                os.remove(urgent_path())
        except PermissionError:
            QMessageBox.critical(
                self, "Acknowledge",
                "Permission denied writing the alert log.\n\nThe alert store "
                "is ACL'd SYSTEM-write / Administrators-modify. Run this GUI "
                "as Administrator.")
            return
        except Exception as exc:
            QMessageBox.critical(self, "Acknowledge", "Failed: %s" % exc)
            return
        self.reload()




# =========================================================================== #
# Service Status tab -- status readout + GLOBAL Analytic/Debug toggles
# =========================================================================== #
class ServiceStatusTab(QWidget):
    def __init__(self, parent_win):
        super().__init__()
        self.win = parent_win
        v = QVBoxLayout(self)

        gb = QGroupBox("Global channel-type policy (service-wide)")
        gv = QVBoxLayout(gb)
        note = QLabel(
            "Analytic and Debug channels are high-volume trace logs and are "
            "SKIPPED by default. These are all-or-nothing, service-wide "
            "switches -- there is no per-channel option (that would mean a "
            "decision on up to ~1,270 channels).")
        note.setWordWrap(True)
        gv.addWidget(note)
        self.chk_analytic = QCheckBox("Archive ALL Analytic channels")
        self.chk_debug = QCheckBox("Archive ALL Debug channels")
        self.chk_analytic.toggled.connect(self._toggled)
        self.chk_debug.toggled.connect(self._toggled)
        gv.addWidget(self.chk_analytic)
        gv.addWidget(self.chk_debug)
        v.addWidget(gb)

        self.body = QTextBrowser()
        v.addWidget(self.body, 1)

    def _toggled(self):
        svc = self.win.cfg.setdefault("service", {})
        svc["include_all_analytic"] = self.chk_analytic.isChecked()
        svc["include_all_debug"] = self.chk_debug.isChecked()
        self.win.mark_dirty()

    def load(self):
        svc = self.win.cfg.get("service", {})
        self.chk_analytic.blockSignals(True)
        self.chk_debug.blockSignals(True)
        self.chk_analytic.setChecked(bool(svc.get("include_all_analytic",
                                                  False)))
        self.chk_debug.setChecked(bool(svc.get("include_all_debug", False)))
        self.chk_analytic.blockSignals(False)
        self.chk_debug.blockSignals(False)

    def set_html(self, html):
        self.body.setHtml(html)


# =========================================================================== #
# Main window
# =========================================================================== #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1140, 760)
        self.cfg = {}
        self.state = {}
        self.all_channels = []
        self._dirty = False

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.channels_tab = ChannelsTab(self)
        self.alerts_tab = AlertsTab(self)
        self.status_tab = ServiceStatusTab(self)

        self.tabs.addTab(self.channels_tab, "Channels")
        self.tabs.addTab(self.alerts_tab, "Alerts")
        self.tabs.addTab(self.status_tab, "Service status")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        bar = QWidget()
        h = QHBoxLayout(bar)
        self.lbl_dirty = QLabel("")
        f = QFont()
        f.setBold(True)
        self.lbl_dirty.setFont(f)
        h.addWidget(self.lbl_dirty, 1)
        for text, fn in (("Reload", self.load_all), ("Save", self.save)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        self.statusBar().addPermanentWidget(bar, 1)

        self.load_all()

    def mark_dirty(self):
        self._dirty = True
        self.lbl_dirty.setText("Unsaved changes")
        self.lbl_dirty.setStyleSheet("color:#b8860b")

    def _on_tab_changed(self, index):
        """When the operator returns to the Channels tab, redraw it so any OS
        scan results or config edits made elsewhere are reflected. This is a
        cheap redraw (reload), not a re-scan; use the Scan button to re-read OS
        settings."""
        if self.tabs.widget(index) is self.channels_tab:
            self.channels_tab.reload()

    def load_all(self):
        self.cfg = read_config()
        self.cfg["schema_version"] = CONFIG_SCHEMA_VERSION
        self.cfg.setdefault("providers", {})
        self.cfg.setdefault("defaults", dict(GLOBAL_DEFAULTS))
        self.cfg.setdefault("service", {
            "poll_interval_sec": 300, "config_reload_interval_sec": 300,
            "retention_check_interval_hours": 24,
            "include_all_analytic": False, "include_all_debug": False})
        self.cfg.setdefault("archive_root",
                            r"C:\ProgramData\logmon\EVENT_LOG_ARCHIVE")
        self.state = read_state()
        if not self.all_channels:
            self.all_channels = enumerate_channels()
        self._dirty = False
        self.lbl_dirty.setText("")
        self.channels_tab.reload()
        self.alerts_tab.reload()
        self.status_tab.load()
        self._render_status()
        self.channels_tab.auto_scan_if_needed()

    def _render_status(self):
        st = self.state
        errs = st.get("config_errors") or []
        parts = []
        if errs:
            parts.append(
                "<h3 style='color:#a62828'>Service reported %d config "
                "problem(s)</h3><ul>%s</ul>"
                % (len(errs), "".join("<li>%s</li>" % e for e in errs)))
        else:
            parts.append("<h3 style='color:#3d7a52'>Config accepted by the "
                         "service</h3>")
        eng = st.get("engine", {})
        if not os.path.exists(state_path()):
            parts.append(
                "<div style='padding:10px;background:#fff0cc;border-left:4px "
                "solid #b8860b'><b>logmon service is not installed or has never "
                "run on this machine.</b><br>No service state file "
                "(<code>logmon_state.json</code>) exists yet. Install and start "
                "the service, then click <b>Reload</b>.<br><br>"
                "<small>From an elevated prompt:<br>"
                "&nbsp;&nbsp;<code>python logmon.py --startup auto install</code>"
                "<br>&nbsp;&nbsp;<code>python logmon.py start</code></small>"
                "</div>")
        elif not eng:
            parts.append(
                "<div style='padding:10px;background:#fff0cc;border-left:4px "
                "solid #b8860b'><b>Service state file exists, but the service "
                "has not recorded an install/start yet.</b><br>Click "
                "<b>Reload</b> after starting it.<br>"
                "<small>Last state update: %s</small></div>"
                % (st.get("last_updated_utc") or "never"))
        else:
            parts.append(
                "<p><b>Service:</b> start type <b>%s</b> &nbsp;|&nbsp; "
                "installed %s &nbsp;|&nbsp; state updated %s</p>"
                % (eng.get("service_start_type") or "unknown",
                   eng.get("install_time_utc") or "unknown",
                   st.get("last_updated_utc") or "unknown"))

        cs = st.get("channel_state", {})
        if cs:
            rows = ["<h3>Archive integrity (per channel)</h3><table "
                    "border=1 cellpadding=4 cellspacing=0>"
                    "<tr><th>Channel</th><th>Destroyed BEFORE logmon</th>"
                    "<th>Destroyed since baseline</th><th>Last cleared by "
                    "logmon (UTC)</th><th>External clears</th>"
                    "<th>Resets after reboot</th></tr>"]
            for ch, d in sorted(cs.items()):
                ext = len(d.get("external_clears", []))
                rst = len(d.get("watermark_resets", []))
                rows.append(
                    "<tr><td>%s</td><td align=right>%s</td>"
                    "<td align=right>%s</td><td>%s</td>"
                    "<td align=right style='color:%s'><b>%d</b></td>"
                    "<td align=right style='color:%s'>%d</td></tr>"
                    % (ch,
                       "{:,}".format(d.get(
                           "baseline_destroyed_before_logmon", 0)),
                       "{:,}".format(d.get("overwritten_since_baseline", 0)),
                       d.get("last_cleared_by_logmon_utc") or "-",
                       "#a62828" if ext else "#3d7a52", ext,
                       "#b8860b" if rst else "#3d7a52", rst))
            rows.append(
                "</table><p><small><b>External clears</b> = watermark fell "
                "while logmon was running with no reboot to explain it "
                "(CRITICAL). <b>Resets after reboot</b> = watermark fell across "
                "a shutdown, consistent with the boot rather than a live clear "
                "(WARNING, recorded for review -- an offline clear during "
                "shutdown would look the same).</small></p>")
            parts.append("".join(rows))

        unconf = st.get("discovered_unconfigured") or []
        if unconf:
            parts.append(
                "<h3>Unconfigured channels discovered (%d)</h3>"
                "<p><small>Not archived until configured on the Channels "
                "tab.</small></p><p>%s</p>"
                % (len(unconf), ", ".join(unconf[:60])
                   + (" ..." if len(unconf) > 60 else "")))
        self.status_tab.set_html("".join(parts))

    def _validate_v2(self):
        """Mirror logmon.validate_config's per-channel/-provider policy checks.
        Returns a list of problem strings."""
        problems = []
        for prov, p in (self.cfg.get("providers", {}) or {}).items():
            pd = p.get("defaults", {}) or {}
            if pd.get("timeframe") and not valid_rotation(pd["timeframe"]):
                problems.append("provider %s default rotation %r invalid"
                                % (prov, pd["timeframe"]))
            if pd.get("legal_retention") and not valid_retention(
                    pd["legal_retention"]):
                problems.append("provider %s default retention %r invalid"
                                % (prov, pd["legal_retention"]))
            for ch, c in (p.get("channels", {}) or {}).items():
                if "timeframe" in c and not valid_rotation(c["timeframe"]):
                    problems.append("channel %s rotation %r invalid"
                                    % (ch, c["timeframe"]))
                if "legal_retention" in c and not valid_retention(
                        c["legal_retention"]):
                    problems.append("channel %s retention %r invalid"
                                    % (ch, c["legal_retention"]))
        return problems

    def save(self):
        problems = self._validate_v2()
        if problems:
            QMessageBox.critical(
                self, "Cannot save",
                "Fix these before saving:\n\n  - " + "\n  - ".join(problems)
                + "\n\nAn invalid rotation would never rotate; an invalid "
                  "retention would never prune. The service drops such values "
                  "rather than running them silently.")
            return
        cfg = dict(self.cfg)
        cfg["schema_version"] = CONFIG_SCHEMA_VERSION
        cfg["_README"] = ("Edited by the logmon GUI. The service READS this "
                          "file and never writes it. Runtime state lives in "
                          "logmon_state.json.")
        try:
            write_config_atomic(cfg)
        except PermissionError:
            QMessageBox.critical(
                self, "Save failed",
                "Permission denied writing logmon.cfg.\nRun this GUI as "
                "Administrator.")
            return
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._dirty = False
        self.lbl_dirty.setText("Saved - service reloads within ~5 minutes")
        self.lbl_dirty.setStyleSheet("color:#3d7a52")

    def closeEvent(self, ev):
        if self._dirty and QMessageBox.question(
                self, "Unsaved changes",
                "Discard unsaved changes?") != QMessageBox.Yes:
            ev.ignore()
            return
        ev.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    if not is_admin():
        QMessageBox.warning(
            None, APP_TITLE,
            "Not running as Administrator.\n\nlogmon.cfg lives in ProgramData "
            "and cannot be saved without elevation. You can browse, but Save "
            "will fail.")
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
