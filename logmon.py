"""logmon.py -- Windows Event Log Archiver (v0.0.1 skeleton).  YASDC

A Windows Event Log ARCHIVER. It captures Windows Event Log channels on a
rotation schedule (timeframe or per-log size limit), archives the extracted
data with hashing and compression, and manages legal-retention pruning of the
archived copies.

logmon is a COMPANION to the OS's Event Log system, not a replacement:
  - Does NOT configure OS Event Log settings (no `wevtutil sl` calls)
  - Does NOT interfere with WEF/WEC subscriptions
  - Does NOT create new events for archived channels

Operational model:
  1. First run per bundle: archive existing channel contents as a one-time
     "historical" dump, clear the channels, and record the rotation anchor.
  2. Poll configured channels/bundles on 5-minute interval
  3. On trigger (timeframe elapsed OR size >= (configured_limit * 0.95)):
     a. Atomically back up + clear each channel via
        `wevtutil cl <channel> /bu:<archive.evtx>`. The backup IS the archive;
        there is NO separate `wevtutil epl` export, so no events can be lost in
        a gap between an export and the clear.
     b. Hash each .evtx, write per-.evtx manifest, zip PER BUNDLE
        ('<PrimaryChannel>_<span>.zip' under the bundle's own subdir)
  4. Daily: prune each bundle's archives beyond its legal retention

Rotation, naming, compression, hashing, retention logic are LITERAL DUPLICATES
from usnmon.py + usn_common.py. logmon adapts them to operate on Windows Event
Log channels via wevtutil instead of USN journal ioctls. Preserves usnmon
behavior exactly for the copied portions.

v0.0.1 status: SKELETON. Code copied and adapted for event log operation.
GUI, complete config-file surface, standalone service verification still
required to ship v0.0.1. See LOGMON_v0.0.1_DESIGN_LOCK.md.

Time standard: ALL rotation boundaries, anchors, archive-span timestamps, and
the legal-retention horizon are computed in UTC (design lock 4.1 / 10.10). There
is no local-time mode and no timezone configuration -- every artifact logmon
handles (Event Log TimeCreated, USN journal TimeStamp, logmon manifests) is
already UTC. All wall-clock reads for this math go through _utcnow().

Platform: Windows-only at runtime (wevtutil, event log, cert store). Pure-logic
helpers can be imported for cross-platform inspection.
"""

import os
import sys
import base64
import hashlib
import json
import logging
import logging.handlers
import re
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

# --- Windows-only imports (guarded so the module can be inspected on non-Windows) ---
try:
    import win32api
    import win32service
    import win32serviceutil
    import win32event
    import servicemanager
    import winerror
    _WINDOWS = True
except Exception:
    win32api = win32service = win32serviceutil = None
    win32event = servicemanager = winerror = None
    _WINDOWS = False


# =========================================================================== #
# Identity / constants
# =========================================================================== #
SERVICE_NAME = "LogMonitorService"
SERVICE_DISPLAY = "Event Log Archiver (logmon)"

DEFAULT_ARCHIVE_DIR = r"C:\ProgramData\logmon\EVENT_LOG_ARCHIVE"
DIAG_LOG_NAME = "logmon.log"

# Contract version. Manifests carry this so downstream tools can bind to the
# hash/manifest schema. Independent of usnmon's SCHEMA_VERSION.
SCHEMA_VERSION = "1.0"

# Size trigger safety margin. Per design lock 4.2: trigger fires when current
# channel .evtx size >= (configured_threshold * 0.95). The 5% margin is against
# the operator's configured limit, NOT the OS max, so a channel configured at
# the OS max (4.0 GB) trips at 3.8 GB and never risks losing events to OS
# retention.
SIZE_TRIGGER_MARGIN = 0.95

# OS-level Event Log MaxSize hard cap for standard channels (4 GB).
EVENT_LOG_OS_MAX_BYTES = 4 * 1024 * 1024 * 1024

# Default poll cadences.
DEFAULT_POLL_INTERVAL_SEC = 300           # 5 minutes; size + timeframe check
DEFAULT_RETENTION_CHECK_HOURS = 24         # daily retention sweep
DEFAULT_CONFIG_RELOAD_INTERVAL_SEC = 300  # safety-net reload; mtime watch is primary

# Repeated-clear-failure handling. A channel that EXISTS but whose
# `wevtutil cl /bu:` keeps failing (restrictive channelAccess SDDL, an
# opted-in Analytic/Debug channel that cannot be cleared while enabled, an
# unreachable UNC backup path under the LocalSystem machine account, a held
# handle, etc.) is backed off with a growing delay and, after enough
# consecutive failures, disabled with a state DISTINCT from a missing channel
# (see _record_clear_failure). This lets the operator tell "channel is gone"
# (LOG MISSING) apart from "channel is here but won't clear" (REPEATED CLEAR
# FAILURE) -- the two need different fixes.
CLEAR_FAILURE_BACKOFF_BASE_SEC = 300          # first retry delay ~ one poll
CLEAR_FAILURE_BACKOFF_CAP_SEC = 6 * 3600      # cap the growing backoff at 6h
CLEAR_FAILURE_DISABLE_THRESHOLD = 6           # consecutive fails -> disable

logger = logging.getLogger("logmon")


# =========================================================================== #
# Time-period parsing  (LITERAL DUPLICATION from usnmon.py)
# =========================================================================== #
# The rotation/retention unit table and caps come straight from usnmon so the
# operator writes identical spec strings ("30d", "5y", "18M") for both tools.
# Sub-day units (s/m/h) are intentionally rejected for retention.
_TIMEPERIOD_UNITS = {
    "s": (1,        False),
    "m": (60,       False),
    "h": (3600,     False),
    "d": (86400,    True),       # calendar day: next-midnight boundaries
    "w": (604800,   True),       # calendar week: next-Monday-00:00 boundaries
    "t": (2592000,  False),      # 30-day "term": fixed 30*24*3600s interval
    "M": (2629800,  True),       # calendar month (avg secs; real math uses
                                 # _add_calendar_months)
    "y": (31557600, True),       # calendar year (avg secs; real math via
                                 # date arithmetic)
}

ROTATION_CAPS = {
    "s": 172800,
    "m": 2880,
    "h": 336,
    "d": 180,
    "w": 52,
    "t": 12,
    "M": 12,
    "y": 1,
}

RETENTION_CAPS = {
    "d": 3650,
    "w": 520,
    "t": 120,
    "M": 300,
    "y": 25,
}


def parse_timeperiod(text, caps):
    """Parse '<N><unit>' -> (n:int, unit:str) tuple. Returns None for blank/
    malformed input (fail-safe: an unparseable term must never trigger deletion
    or rotation). Integer magnitudes only. Case-significant: 'm'=minutes,
    'M'=calendar months. LITERAL DUPLICATION from usnmon.py."""
    if not text or not str(text).strip():
        return None
    mo = re.fullmatch(r"\s*([0-9]{1,5})\s*([smhdwtMy])\s*", str(text))
    if not mo:
        return None
    n = int(mo.group(1))
    unit = mo.group(2)
    if unit not in caps:
        return None
    if n < 1 or n > caps[unit]:
        return None
    return (n, unit)


def parse_retention(text):
    """Legal-retention term -> (n, unit) or None. Wrapper over parse_timeperiod
    with RETENTION_CAPS. LITERAL DUPLICATION from usnmon.py."""
    return parse_timeperiod(text, RETENTION_CAPS)


def parse_interval(text):
    """Rotation interval -> (n, unit) or None. Wrapper over parse_timeperiod
    with ROTATION_CAPS. LITERAL DUPLICATION from usnmon.py."""
    return parse_timeperiod(text, ROTATION_CAPS)


def _add_calendar_months(dt, months):
    """Add N calendar months to dt, clamping day to target month's last valid
    day. Leap- and month-length-aware. LITERAL DUPLICATION from usnmon.py."""
    import calendar
    total = (dt.year * 12 + (dt.month - 1)) + months
    y, m = divmod(total, 12)
    m += 1
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def _period_advance(dt, n, unit, direction=1):
    """Move dt by N units (calendar-aware for d/w/M/y, fixed-interval for
    s/m/h/t). LITERAL DUPLICATION from usnmon.py."""
    if unit not in _TIMEPERIOD_UNITS:
        return dt
    secs, is_calendar = _TIMEPERIOD_UNITS[unit]
    if is_calendar:
        if unit == "d":
            return dt + timedelta(days=direction * n)
        if unit == "w":
            return dt + timedelta(weeks=direction * n)
        if unit == "M":
            return _add_calendar_months(dt, direction * n)
        if unit == "y":
            return _add_calendar_months(dt, direction * n * 12)
    return dt + timedelta(seconds=direction * n * secs)


# =========================================================================== #
# Rotation boundary math  (LITERAL DUPLICATION from usnmon.py)
# =========================================================================== #
def _archive_format_date(dt, with_time):
    """Format a datetime for use in an archive filename. ISO-8601 style
    YYYY-MM-DD, optional -HHMMSS suffix. LITERAL DUPLICATION from usnmon.py."""
    if with_time:
        return dt.strftime("%Y-%m-%d-%H%M%S")
    return dt.strftime("%Y-%m-%d")


def _is_midnight(dt):
    """True iff datetime is exactly midnight. LITERAL DUPLICATION."""
    return (dt.hour == 0 and dt.minute == 0 and dt.second == 0
            and dt.microsecond == 0)


def _is_calendar_month_start(dt):
    """True iff datetime is 1st of month at midnight. LITERAL DUPLICATION."""
    return _is_midnight(dt) and dt.day == 1


def _next_rotation_boundary(anchor, rotate_period):
    """Given a rotation anchor and (n, unit) period, return the next boundary
    datetime. Calendar units snap to calendar boundaries. Interval units are
    anchor + N*unit_seconds. LITERAL DUPLICATION from usnmon.py."""
    if not rotate_period:
        return None
    n, unit = rotate_period
    if unit not in _TIMEPERIOD_UNITS:
        return None
    secs, is_calendar = _TIMEPERIOD_UNITS[unit]
    if not is_calendar:
        return anchor + timedelta(seconds=n * secs)
    # Calendar boundary handling: on-boundary = advance N units; off-boundary
    # = next boundary regardless of N (ramp-in close). See usnmon.py source
    # for the full rationale.
    if unit == "d":
        day0 = datetime(anchor.year, anchor.month, anchor.day)
        if _is_midnight(anchor):
            return day0 + timedelta(days=n)
        return day0 + timedelta(days=1)
    if unit == "w":
        days_until_monday = (7 - anchor.weekday()) % 7
        if days_until_monday == 0 and _is_midnight(anchor):
            return (datetime(anchor.year, anchor.month, anchor.day)
                    + timedelta(weeks=n))
        if days_until_monday == 0:
            days_until_monday = 7
        return (datetime(anchor.year, anchor.month, anchor.day)
                + timedelta(days=days_until_monday))
    if unit == "M":
        if _is_calendar_month_start(anchor):
            return _add_calendar_months(anchor, n)
        if anchor.month == 12:
            return datetime(anchor.year + 1, 1, 1)
        return datetime(anchor.year, anchor.month + 1, 1)
    if unit == "y":
        if _is_midnight(anchor) and anchor.month == 1 and anchor.day == 1:
            return _add_calendar_months(anchor, n * 12)
        return datetime(anchor.year + 1, 1, 1)
    return None


def _current_rotation_window_start(now, rotate_period):
    """Given wall-clock time and rotate_period, return the start of the calendar
    window we are currently inside. Returns None for interval units (no
    calendar-aligned window). LITERAL DUPLICATION from usnmon.py."""
    if not rotate_period:
        return None
    n, unit = rotate_period
    if unit not in _TIMEPERIOD_UNITS:
        return None
    _secs, is_calendar = _TIMEPERIOD_UNITS[unit]
    if not is_calendar:
        return None
    if unit == "d":
        return datetime(now.year, now.month, now.day)
    if unit == "w":
        monday = now - timedelta(days=now.weekday())
        return datetime(monday.year, monday.month, monday.day)
    if unit == "M":
        return datetime(now.year, now.month, 1)
    if unit == "y":
        return datetime(now.year, 1, 1)
    return None


def _parse_anchor(s):
    """Parse 'YYYY-MM-DD HH:MM:SS' anchor string from config. Returns a naive
    datetime (representing UTC -- see _utcnow) or None. Anchors are written by
    _record_bundle_anchor from _utcnow(), so the naive value read back here is
    UTC. LITERAL DUPLICATION from usnmon.py (parse unchanged; the stored value
    is now UTC rather than local)."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# =========================================================================== #
# Hashing / manifest  (LITERAL DUPLICATION from usnmon.py)
# =========================================================================== #
def sha256_hex(data):
    """Hex SHA-256 of bytes. LITERAL DUPLICATION from usn_common.py."""
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(doc):
    """Deterministic byte form of a dict for signing/verifying. Signature
    field excluded, keys sorted, separators fixed, UTF-8. LITERAL DUPLICATION
    from usn_common.py."""
    d = {k: v for k, v in doc.items() if k != "signature"}
    return json.dumps(d, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_doc(doc, private_key):
    """Sign canonical_bytes(doc) with an RSA or ECDSA private key. Returns
    base64 signature. LITERAL DUPLICATION from usn_common.py."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, ec, rsa
    data = canonical_bytes(doc)
    if isinstance(private_key, rsa.RSAPrivateKey):
        sig = private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    elif isinstance(private_key, ec.EllipticCurvePrivateKey):
        sig = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    else:
        raise TypeError("Unsupported key type for signing")
    return base64.b64encode(sig).decode("ascii")


def _signing_key():
    """Load the private signing key from LOGMON_SIGNING_KEY env var (PEM path).
    Returns None if unset -> manifests carry hashes only (tamper-evident,
    unsigned). LITERAL DUPLICATION from usnmon.py, env var renamed."""
    pem = os.environ.get("LOGMON_SIGNING_KEY", "").strip()
    if not pem or not os.path.exists(pem):
        return None
    try:
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key)
        with open(pem, "rb") as f:
            return load_pem_private_key(f.read(), password=None)
    except Exception:
        return None


def write_manifest(target_file, bundle_name="", provenance=None):
    """Sidecar manifest: four hashes (one read pass) + optional signature.
    Never an ADS -- a sidecar survives FAT/exFAT/S3/Dropbox/email/zip transit.
    Returns the manifest path.

    LITERAL DUPLICATION of usnmon.write_manifest with month->bundle_name
    swap in the manifest schema."""
    h = {"md5": hashlib.md5(), "sha1": hashlib.sha1(),
         "sha256": hashlib.sha256(), "sha512": hashlib.sha512()}
    size = 0
    with open(target_file, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            size += len(chunk)
            for d in h.values():
                d.update(chunk)   # one read, four digests in parallel
    manifest = {
        "file": os.path.basename(target_file),
        "size": size,
        "bundle": bundle_name,
        "md5": h["md5"].hexdigest(),
        "sha1": h["sha1"].hexdigest(),
        "sha256": h["sha256"].hexdigest(),
        "sha512": h["sha512"].hexdigest(),
        "generated": now_utc_str(),
        "generator": "logmon %s" % SCHEMA_VERSION,
        "note": "md5/sha1 legacy-interop only (collision-broken); "
                "sha256/sha512 authoritative",
    }
    # CAVEATS / PROVENANCE (design lock 10.17). logmon cannot guarantee
    # completeness on a circular channel it does not control -- but it CAN
    # guarantee every gap is detected, counted, and disclosed. That is a far
    # stronger position than an unbackable completeness claim. This block is
    # inside the hashed (and optionally signed) manifest, so it is
    # tamper-evident and travels with the archive.
    if provenance:
        manifest["caveats"] = provenance
    key = _signing_key()
    if key is not None:
        try:
            manifest["signature"] = sign_doc(manifest, key)
        except Exception as exc:
            logger.error("manifest sign: %r", exc)
    mpath = target_file + ".manifest"
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return mpath


def build_zip_bundle(bundle_path, files_with_arcnames):
    """Compress *files_with_arcnames* into one zip. Each entry is
    (filesystem_path, arcname_in_zip). arcname preserves per-bundle
    subdirectory structure inside the zip (per design lock 5.2).

    Append-only: never deletes inputs here (caller handles cleanup).
    Returns the bundle path.

    ADAPTED from usn_common.build_monthly_bundle: same zipfile approach,
    but takes explicit arcnames so directory structure is preserved."""
    import zipfile
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as z:
        for src, arcname in files_with_arcnames:
            if src and os.path.exists(src):
                z.write(src, arcname=arcname)
    return bundle_path


# =========================================================================== #
# Diagnostic logging setup  (LITERAL DUPLICATION from usnmon.py)
# =========================================================================== #
def setup_logging(to_console, archive_dir):
    """Rotating file handler for the diagnostic log, optional console.

    Per design lock 8.1, the diagnostic log lives next to the config, at
    `C:\\ProgramData\\logmon\\logmon.log` -- NOT in the archive directory. The
    archive_dir argument is retained for call-site compatibility but is no
    longer where the log is written."""
    logger.setLevel(logging.INFO)
    # Diagnostic log stays in machine LOCAL time by design (operational audience:
    # admins/help desk) -- distinct from archives/retention, which are UTC
    # (design lock 8.1 / 4.1). The trailing %z stamps each line with the local
    # UTC offset (e.g. -0400), removing DST/zone ambiguity and making a line
    # trivial to cross-reference against a UTC archive. (Millisecond precision is
    # dropped as a side effect of the custom datefmt; not needed for diag logs.)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S %z")
    try:
        log_dir = os.path.dirname(_config_path())   # C:\ProgramData\logmon
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, DIAG_LOG_NAME),
            maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    if to_console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)


def now_utc_str():
    """UTC timestamp string with ms precision. LITERAL DUPLICATION."""
    return (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            + "Z")


def _utcnow():
    """Current time in UTC as a NAIVE datetime (tzinfo stripped).

    logmon computes ALL rotation boundaries, anchors, span timestamps, and the
    legal-retention horizon in UTC (design lock 4.1 / 10.10, locked 2026-07-09).
    Every wall-clock read for that math goes through this one helper instead of
    the naive local `datetime.now()` copied from usnmon, so the UTC decision
    lives in a single place.

    A naive value (representing UTC) is returned deliberately: the boundary math
    (`_next_rotation_boundary`, `_add_calendar_months`, etc.) constructs naive
    datetimes and stored anchors are naive strings, so keeping "now" naive keeps
    the whole pipeline in one representation and avoids aware/naive comparison
    errors. The point of the switch is WHICH wall clock feeds the math (UTC, not
    local); the representation stays naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# =========================================================================== #
# CONFIG + STATE: TWO FILES, SINGLE-WRITER EACH  (design lock 7.2, G1)
# =========================================================================== #
# The GUI and the service are separate processes. If both read-modify-write the
# same file, the service's stale in-memory copy will silently clobber whatever
# the operator just saved (lost update). The ONLY safe structure is: every file
# has exactly ONE writer.
#
#   logmon.cfg        OPERATOR/GUI OWNED.  Service READS ONLY (never writes,
#                     except the one-time install bootstrap, when no GUI runs).
#                     Holds: schema_version(=2), service{} (ints + global
#                     include_all_analytic/_debug), archive_root, defaults{}
#                     (global policy), providers{ <provider>: {enabled,
#                     defaults{}, channels{ <channel>: {enabled, policy} } } }.
#
#   logmon.cfg.bak    SERVICE OWNED. Last-KNOWN-GOOD snapshot. A config is only
#                     promoted here after it VALIDATES CLEAN -- an unvalidated
#                     backup is worthless as a fallback.
#
#   logmon_state.json SERVICE OWNED. GUI READS ONLY (to display status).
#                     Holds: engine{}, bundle_state{}, channel_state{},
#                     discovered_unconfigured[], disabled_channels[],
#                     config_errors[].
#
# GUI CONTRACT (both sides must honor):
#   1. GUI writes logmon.cfg ATOMICALLY (temp file + os.replace). A partial
#      write would otherwise be read mid-flight by the service.
#   2. GUI NEVER writes logmon_state.json or logmon.cfg.bak.
#   3. Service NEVER writes logmon.cfg after install.
#   4. Service publishes all validation errors to state.config_errors so the
#      GUI can surface them; it never silently ignores bad input.
# =========================================================================== #

CONFIG_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 1
STATE_FILE_NAME = "logmon_state.json"

# --------------------------------------------------------------------------- #
# Policy model (schema v2). Rules attach to two OS-real objects only:
#   PROVIDER (owningPublisher, or the synthetic bucket "Windows Logs" for the
#             classic five, or "Applications and Services Logs" for a non-classic
#             channel that has no owningPublisher), and CHANNEL (the log itself).
# The Event-Viewer-style display tree is DERIVED separately (GUI) and is not
# stored here.
#
# A policy is sparse: only the keys present override. Effective value for a
# channel resolves per key:  channel override -> provider default -> global
# default (1M / 1y). One archive is produced per distinct (rotation, retention)
# pair that is due (grouping handled by the Pass-2 archive engine).
# --------------------------------------------------------------------------- #
POLICY_KEYS = ("rotate", "timeframe", "legal_retention", "size_limit_bytes")
GLOBAL_DEFAULT_POLICY = {
    "rotate": True,
    "timeframe": "1M",          # operator direction 2026-07-15: global default
    "legal_retention": "1y",    # operator direction 2026-07-15: global default
    "size_limit_bytes": None,   # None = track each channel's OS maxSize
}


def _logmon_dir():
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, "logmon")


def _config_path():
    """logmon.cfg -- OPERATOR/GUI owned."""
    return os.path.join(_logmon_dir(), "logmon.cfg")


def _config_backup_path():
    """logmon.cfg.bak -- SERVICE owned last-KNOWN-GOOD snapshot."""
    return _config_path() + ".bak"


def _state_path():
    """logmon_state.json -- SERVICE owned."""
    return os.path.join(_logmon_dir(), STATE_FILE_NAME)


def _atomic_write_json(path, doc):
    """temp file + fsync + os.replace. The only sanctioned way to write either
    file; the GUI must use the same pattern for logmon.cfg."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.error("atomic write failed for %s: %r", path, exc)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------- #
# Config validation (G2) -- NOTHING is accepted unvalidated.
# --------------------------------------------------------------------------- #
# Every value the GUI can write is checked here. A bad value NEVER results in
# silent inertness (the old failure mode: timeframe "30 days" -> parse returns
# None -> bundle simply never rotates, with no error anywhere). Instead the
# bundle is disabled and the error is published to state.config_errors.

_SERVICE_INT_KEYS = {
    "poll_interval_sec": (30, 86400, DEFAULT_POLL_INTERVAL_SEC),
    "config_reload_interval_sec": (30, 86400,
                                   DEFAULT_CONFIG_RELOAD_INTERVAL_SEC),
    "retention_check_interval_hours": (1, 168,
                                       DEFAULT_RETENTION_CHECK_HOURS),
}

_CHANNEL_KNOWN_KEYS = set(POLICY_KEYS) | {"enabled"}


def _normalize_policy(raw, errors, ctx):
    """Validate a SPARSE policy block (used for global defaults, provider
    defaults, and channel overrides). Only keys PRESENT are validated and
    returned, so overrides stay sparse and inherit everything else. Bad values
    append an error and are dropped (the resolver then falls back). Never
    raises."""
    out = {}
    if not isinstance(raw, dict):
        errors.append("%s: policy must be an object" % ctx)
        return out
    if "rotate" in raw:
        out["rotate"] = bool(raw["rotate"])
    if "timeframe" in raw:
        tf = str(raw.get("timeframe") or "").strip()
        # Empty timeframe is only meaningful with rotate=false; validate when set.
        if tf:
            ok, res = _v_interval(tf)
            if not ok:
                errors.append("%s timeframe %r: %s" % (ctx, tf, res))
            else:
                out["timeframe"] = tf
        else:
            out["timeframe"] = ""
    if "legal_retention" in raw:
        lr = str(raw.get("legal_retention") or "").strip()
        ok, res = _v_retention(lr)     # empty = keep forever, valid
        if not ok:
            errors.append("%s legal_retention %r: %s" % (ctx, lr, res))
        else:
            out["legal_retention"] = lr
    if "size_limit_bytes" in raw:
        slb = raw.get("size_limit_bytes")
        if slb is None or slb == "":
            out["size_limit_bytes"] = None
        else:
            ok, res = _v_size_bytes(slb)
            if not ok:
                errors.append("%s size_limit_bytes %r: %s" % (ctx, slb, res))
            else:
                out["size_limit_bytes"] = res
    return out


def validate_config(raw):
    """Validate a raw v2 config dict.

    Returns (cfg, errors):
      cfg    -- normalized config safe for the engine to consume. Providers /
                channels with bad policy values have those values DROPPED (the
                resolver falls back to provider/global defaults) and the problem
                is reported; nothing is silently half-run.
      errors -- human-readable strings for the GUI (state.config_errors) and the
                diagnostic log.

    v2 structure (design lock 3.2, revised 2026-07-15):
        { schema_version: 2,
          archive_root, service{ ints + include_all_analytic/_debug },
          defaults{ global policy },
          providers{ <provider>: { enabled, defaults{policy},
                                   channels{ <channel>: {enabled, policy} } } } }
    There are no 'bundles'. NO migration from v1 (operator direction: the test
    system resets); a v1 config is reported as a schema mismatch.
    """
    errors = []
    if not isinstance(raw, dict):
        return ({"schema_version": CONFIG_SCHEMA_VERSION, "providers": {},
                 "defaults": dict(GLOBAL_DEFAULT_POLICY)},
                ["config root is not a JSON object"])

    cfg = {"schema_version": CONFIG_SCHEMA_VERSION}
    ver = raw.get("schema_version", CONFIG_SCHEMA_VERSION)
    if ver != CONFIG_SCHEMA_VERSION:
        errors.append("schema_version %r != expected %d (no migration is "
                      "performed; reset the config)" % (ver,
                                                        CONFIG_SCHEMA_VERSION))

    # --- archive_root ---
    ok, res = _v_archive_dir(str(raw.get("archive_root", DEFAULT_ARCHIVE_DIR)))
    if not ok:
        errors.append("archive_root: %s (using default %s)"
                      % (res, DEFAULT_ARCHIVE_DIR))
        cfg["archive_root"] = DEFAULT_ARCHIVE_DIR
    else:
        cfg["archive_root"] = res

    # --- service{} (ints + GLOBAL analytic/debug toggles) ---
    svc_raw = raw.get("service", {})
    if not isinstance(svc_raw, dict):
        errors.append("service: must be an object; using defaults")
        svc_raw = {}
    svc = {}
    for k, (lo, hi, dflt) in _SERVICE_INT_KEYS.items():
        v = svc_raw.get(k, dflt)
        try:
            v = int(v)
            if not (lo <= v <= hi):
                raise ValueError
        except Exception:
            errors.append("service.%s: %r invalid (allowed %d-%d); using %d"
                          % (k, svc_raw.get(k), lo, hi, dflt))
            v = dflt
        svc[k] = v
    # Global Analytic/Debug inclusion (design lock 10.2, revised 2026-07-15):
    # one yes/no each, service-wide, never per-channel. Default off (both are
    # usually noise and often un-clearable direct channels).
    svc["include_all_analytic"] = bool(svc_raw.get("include_all_analytic",
                                                   False))
    svc["include_all_debug"] = bool(svc_raw.get("include_all_debug", False))
    cfg["service"] = svc

    # --- global defaults{} (complete; every POLICY_KEY resolved) ---
    gd = dict(GLOBAL_DEFAULT_POLICY)
    gd.update(_normalize_policy(raw.get("defaults", {}), errors, "defaults"))
    cfg["defaults"] = gd

    # --- providers{} ---
    provs_raw = raw.get("providers", {})
    if not isinstance(provs_raw, dict):
        errors.append("providers: must be an object")
        provs_raw = {}
    providers = {}
    for pname, praw in provs_raw.items():
        if not isinstance(praw, dict):
            errors.append("provider %r: not an object; skipped" % pname)
            continue
        p = {"enabled": bool(praw.get("enabled", True))}
        p["defaults"] = _normalize_policy(
            praw.get("defaults", {}), errors, "provider %r defaults" % pname)
        chans_raw = praw.get("channels", {})
        if not isinstance(chans_raw, dict):
            errors.append("provider %r channels: must be an object" % pname)
            chans_raw = {}
        chans = {}
        for cname, craw in chans_raw.items():
            craw = craw or {}
            if not isinstance(craw, dict):
                errors.append("channel %r in provider %r: must be an object"
                              % (cname, pname))
                continue
            for k in craw:
                if k not in _CHANNEL_KNOWN_KEYS and not k.startswith("_"):
                    errors.append("channel %r: unknown key %r" % (cname, k))
            c = {"enabled": bool(craw.get("enabled", True))}
            c.update(_normalize_policy(craw, errors, "channel %r" % cname))
            chans[str(cname).strip()] = c
        p["channels"] = chans
        providers[str(pname).strip()] = p
    cfg["providers"] = providers
    return cfg, errors


# --------------------------------------------------------------------------- #
# Effective-policy resolution (schema v2)
# --------------------------------------------------------------------------- #
def _find_channel_entry(cfg, channel):
    """Return (provider_key, provider_dict, channel_dict) for a configured
    channel, or (None, None, None). Channel names are unique across providers in
    practice (a channel has one owningPublisher); first match wins."""
    for pk, p in cfg.get("providers", {}).items():
        ch = p.get("channels", {})
        if channel in ch:
            return pk, p, ch[channel]
    return None, None, None


def channel_is_configured(cfg, channel):
    """True if the channel appears in any provider's channel set."""
    return _find_channel_entry(cfg, channel)[2] is not None


def channel_is_active(cfg, channel):
    """Configured AND enabled at BOTH the channel and provider level. Only
    active channels are archived/cleared."""
    pk, p, c = _find_channel_entry(cfg, channel)
    if c is None:
        return False
    return bool(c.get("enabled", True)) and bool(p.get("enabled", True))


def effective_channel_policy(cfg, channel):
    """Resolve the effective policy for a channel, per key:
        channel override -> provider default -> global default (1M / 1y).
    Returns a COMPLETE dict over POLICY_KEYS plus '_provider' and '_active',
    or None if the channel is not configured."""
    pk, p, c = _find_channel_entry(cfg, channel)
    if c is None:
        return None
    gd = cfg.get("defaults", GLOBAL_DEFAULT_POLICY)
    pd = p.get("defaults", {})
    eff = {}
    for k in POLICY_KEYS:
        if k in c:
            eff[k] = c[k]
        elif k in pd:
            eff[k] = pd[k]
        else:
            eff[k] = gd.get(k, GLOBAL_DEFAULT_POLICY[k])
    eff["_provider"] = pk
    eff["_active"] = (bool(c.get("enabled", True))
                      and bool(p.get("enabled", True)))
    return eff


def channel_group_key(eff):
    """The (rotation, retention) identity that decides which archive a channel
    joins. One archive is produced per distinct pair that is due (Pass-2 engine).
    A non-rotating channel has rotation None (it only archives on size); an
    empty retention ('keep forever') is preserved distinctly from a term."""
    rotation = eff.get("timeframe") if eff.get("rotate", True) else None
    retention = eff.get("legal_retention") or ""
    return (rotation, retention)


def iter_active_channels(cfg):
    """Yield (channel_name, effective_policy) for every ACTIVE channel. This is
    the enumeration the Pass-2 archive engine groups by channel_group_key()."""
    for pk, p in cfg.get("providers", {}).items():
        if not p.get("enabled", True):
            continue
        for cname, c in p.get("channels", {}).items():
            if not c.get("enabled", True):
                continue
            eff = effective_channel_policy(cfg, cname)
            if eff:
                yield cname, eff


def read_config():
    """Read + VALIDATE logmon.cfg. Never returns unvalidated data.

    Resolution order:
      1. logmon.cfg absent          -> {} (legitimate first run)
      2. logmon.cfg parses + validates CLEAN
                                    -> use it, and promote it to logmon.cfg.bak
                                       (only a clean config becomes the
                                       last-known-good -- G2)
      3. logmon.cfg parses with per-bundle errors
                                    -> use it (bad bundles disabled), publish
                                       errors, do NOT refresh the backup
      4. logmon.cfg corrupt/unparseable
                                    -> fall back to logmon.cfg.bak (itself
                                       previously validated), publish errors
      5. nothing usable             -> {} + errors
    """
    p = _config_path()
    raw = None
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.error("config unreadable (%r); falling back to last-known-good",
                     exc)
        try:
            with open(_config_backup_path(), "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            _publish_config_errors(["config unreadable (%r) and no usable "
                                    "last-known-good backup" % (exc,)])
            return {}
        cfg, errors = validate_config(raw)
        _publish_config_errors(
            ["PRIMARY CONFIG CORRUPT -- running on last-known-good backup"]
            + errors)
        return cfg

    cfg, errors = validate_config(raw)
    if errors:
        for e in errors:
            logger.error("config validation: %s", e)
        _publish_config_errors(errors)
        # Deliberately do NOT refresh logmon.cfg.bak: a backup is only useful
        # if it is known-good.
    else:
        _publish_config_errors([])
        _atomic_write_json(_config_backup_path(), raw)   # promote known-good
    return cfg


def write_config(archive_root):
    """INSTALL-TIME BOOTSTRAP ONLY -- the single sanctioned service write to
    logmon.cfg. Safe because no GUI is running during install. After install
    the service is strictly read-only on this file."""
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    raw["schema_version"] = CONFIG_SCHEMA_VERSION
    raw["archive_root"] = archive_root
    raw.setdefault("service", {
        "poll_interval_sec": DEFAULT_POLL_INTERVAL_SEC,
        "config_reload_interval_sec": DEFAULT_CONFIG_RELOAD_INTERVAL_SEC,
        "retention_check_interval_hours": DEFAULT_RETENTION_CHECK_HOURS,
        "include_all_analytic": False,
        "include_all_debug": False,
    })
    raw.setdefault("defaults", dict(GLOBAL_DEFAULT_POLICY))
    raw.setdefault("providers", {})
    raw.setdefault("_README",
                   "Edited by the logmon GUI (or by hand, carefully). The "
                   "service READS this file and never writes it. Runtime "
                   "state lives in logmon_state.json.")
    _atomic_write_json(_config_path(), raw)


def read_archive_root():
    """Configured archive root (already validated by validate_config)."""
    return read_config().get("archive_root", DEFAULT_ARCHIVE_DIR)


# --------------------------------------------------------------------------- #
# State file -- SERVICE OWNED, GUI READ-ONLY
# --------------------------------------------------------------------------- #
def read_state():
    """Read logmon_state.json. {} on any failure -- state is reconstructible,
    so unlike the config there is no backup chain. A lost state file means
    logmon re-bootstraps (a fresh historical capture), which is safe."""
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def write_state(state):
    """Write logmon_state.json atomically. SERVICE ONLY."""
    state = dict(state)
    state["schema_version"] = STATE_SCHEMA_VERSION
    state["_README"] = ("Written by the logmon SERVICE. Do not edit. The GUI "
                        "reads this for status only. Operator settings live "
                        "in logmon.cfg.")
    state["last_updated_utc"] = now_utc_str()
    return _atomic_write_json(_state_path(), state)


def _update_state(mutator):
    """Read-modify-write the state file under a mutator callback. The service
    is the sole writer, so no cross-process race exists; the in-process lock
    guards concurrent engine threads."""
    with _STATE_LOCK:
        st = read_state()
        mutator(st)
        write_state(st)


_STATE_LOCK = threading.Lock()


def _publish_config_errors(errors):
    """Surface validation errors to the GUI via the state file. An empty list
    clears them (config was fixed)."""
    try:
        _update_state(lambda st: st.__setitem__("config_errors", list(errors)))
    except Exception:
        pass


def get_bundle_state(bundle_name=None):
    """bundle_state from the STATE file (was in the config -- moved for G1)."""
    bs = read_state().get("bundle_state", {})
    if bundle_name is None:
        return bs
    return bs.get(bundle_name, {})

# =========================================================================== #
# Config validation  (LITERAL DUPLICATION from usnmon.py -- path validators)
# =========================================================================== #
_PATH_FORBIDDEN = set('<>"|*?;&`$\n\r\t\x00') | {chr(c) for c in range(32)}


def _v_archive_dir(s):
    """Validate an archive directory path. LITERAL DUPLICATION from usnmon."""
    s = s.strip().strip('"').strip("'").strip()
    if not s:
        return False, "path cannot be empty"
    bad = sorted(_PATH_FORBIDDEN & set(s))
    if bad:
        shown = "".join(c for c in bad if c.isprintable()) or "control chars"
        return False, "path contains forbidden character(s): %s" % shown
    if not re.match(r"^([A-Za-z]:\\|\\\\)", s):
        return False, "use an absolute path, e.g. C:\\EVENT_LOG_ARCHIVE"
    if ".." in s.replace("\\", "/").split("/"):
        return False, "path must not contain '..' (traversal)"
    if len(s) > 248:
        return False, "path too long (max 248 chars)"
    return True, s


def _v_interval(s):
    """Validate a rotation interval spec. LITERAL DUPLICATION from usnmon."""
    s = s.strip()
    if not s:
        return True, ""
    if not re.fullmatch(r"[0-9]+[smhdwtMy]", s):
        return False, ("use e.g. 10m, 6h, 1d, 2w, 1t, 1M, 1y "
                       "(m=minutes, M=calendar months); empty = default")
    return (True, s) if parse_interval(s) is not None else \
        (False, "magnitude out of range; check ROTATION_CAPS for per-unit max")


def _v_retention(s):
    """Validate a legal retention spec. LITERAL DUPLICATION from usnmon."""
    s = s.strip().strip('"').strip("'")
    if not s:
        return True, ""
    if not re.fullmatch(r"[0-9]+[dwtMy]", s):
        return False, ("use e.g. 25y, 18M, 90d, 18t, 26w, or empty to keep "
                       "everything (M=calendar months; m=minutes not valid)")
    return (True, s) if parse_retention(s) is not None else \
        (False, "bad term (or out of range); use e.g. 25y, 18M, 90d, 18t, 26w")


def _v_size_bytes(n):
    """Sanity-check an OPTIONAL per-bundle size limit, in bytes.

    IMPORTANT (design lock 10.14): this is NOT the operative ceiling. The real
    ceiling is PER CHANNEL and is read from the OS (`wevtutil gl` -> maxSize).
    EVENT_LOG_OS_MAX_BYTES (4 GiB) is only the theoretical upper bound for a
    standard channel and is used here purely to reject absurd input; on real
    hosts the actual maxSize is far smaller (Security/System/Application were
    measured at 20 MiB). The effective threshold is computed per channel at
    trigger time by effective_size_limit().

    None/absent is LEGAL and means "use each channel's OS maxSize" -- the
    recommended default, because it can never produce an unreachable threshold.
    """
    if n is None:
        return True, None
    try:
        n = int(n)
    except Exception:
        return False, "size must be an integer number of bytes"
    if n <= 0:
        return False, "size must be positive"
    if n > EVENT_LOG_OS_MAX_BYTES:
        return False, ("size %d exceeds the 4 GiB theoretical channel maximum"
                       % n)
    return True, n


# =========================================================================== #
# Event Log channel operations  (NEW for logmon -- wevtutil-based)
# =========================================================================== #
def enumerate_channels():
    """List all Event Log channel names via `wevtutil el`. Returns a list of
    channel-name strings. Empty list on failure (a failure is a hard problem
    reported to the caller — logmon cannot operate without channel enumeration)."""
    try:
        out = subprocess.run(["wevtutil", "el"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            logger.error("wevtutil el failed: rc=%d stderr=%s",
                         out.returncode, out.stderr.strip())
            return []
        return [line.strip() for line in out.stdout.splitlines()
                if line.strip()]
    except Exception as exc:
        logger.error("wevtutil el exception: %r", exc)
        return []


def get_channel_metadata(channel):
    """Return dict of channel metadata via `wevtutil gl <channel>`. Empty
    dict on failure. Parsed as key: value pairs. Used to check enabled
    state, file path (for size polling), and channel type (Operational vs
    Analytical vs Debug)."""
    try:
        out = subprocess.run(["wevtutil", "gl", channel],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {}
        result = {}
        for line in out.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip()
        return result
    except Exception as exc:
        logger.error("wevtutil gl %s exception: %r", channel, exc)
        return {}


def get_channel_info(channel):
    """Parse `wevtutil gli <channel>` into a dict. {} on failure.

    Fields (verified against a live host, design lock Appendix A.4):
        creationTime, lastAccessTime, lastWriteTime  (ISO-8601 UTC strings)
        fileSize            int, bytes
        numberOfLogRecords  int, records currently held
        oldestRecordNumber  int, EventRecordID of the oldest surviving record

    `oldestRecordNumber` is the ONLY authoritative loss counter (HARD RULE 2):
    it advances by exactly the number of records the OS has purged. Purging is
    BYTE-driven, not record-driven, so loss can never be inferred from
    numberOfLogRecords or fileSize deltas.
    """
    try:
        out = subprocess.run(["wevtutil", "gli", channel],
                             capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return {}
        info = {}
        for line in out.stdout.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if k in ("fileSize", "numberOfLogRecords", "oldestRecordNumber",
                     "attributes"):
                try:
                    info[k] = int(v)
                except ValueError:
                    pass
            elif k in ("creationTime", "lastAccessTime", "lastWriteTime"):
                info[k] = v
        return info
    except Exception as exc:
        logger.error("wevtutil gli %s exception: %r", channel, exc)
        return {}


def newest_record_number(info):
    """Derive the newest EventRecordID held: oldest + count - 1.
    Returns 0 for an empty/unknown log. A DECREASE in this value across
    observations means records vanished -- i.e. the log was CLEARED."""
    o = info.get("oldestRecordNumber", 0)
    n = info.get("numberOfLogRecords", 0)
    if not o or not n:
        return 0
    return o + n - 1


def channel_file_size(channel):
    """Current on-disk size in bytes. 0 on failure (safe: never trips the
    size threshold)."""
    return get_channel_info(channel).get("fileSize", 0)


def channel_os_config(channel):
    """The OS-governed reality of a channel, from `wevtutil gl`. This is what
    logmon CANNOT control and therefore MUST disclose (design lock 10.17).

    Returns a normalized dict; {} on failure. logmon NEVER writes these values
    (HARD RULE 3 -- no `wevtutil sl`, ever). It reads and reports them."""
    md = get_channel_metadata(channel)
    if not md:
        return {}

    def _b(key):
        return str(md.get(key, "")).strip().lower() == "true"

    cfg = {
        "enabled": _b("enabled"),
        "type": md.get("type", ""),
        "retention": _b("retention"),
        "auto_backup": _b("autoBackup"),
    }
    try:
        cfg["max_size"] = int(md.get("maxSize", 0))
    except Exception:
        cfg["max_size"] = 0
    return cfg


def channel_exists(channel):
    """True if the channel exists at OS level. Uses `wevtutil gl`. False on
    failure (safe: caller treats false as 'log missing' per design lock 10.7)."""
    md = get_channel_metadata(channel)
    return bool(md)


def channel_is_empty(channel):
    """True if the channel has zero events at the OS level. Uses
    `wevtutil qe <channel> /c:1` (query 1 event). False if any event exists
    or if we can't determine. Per design lock 10.8: empty channels are
    skipped."""
    try:
        out = subprocess.run(["wevtutil", "qe", channel, "/c:1"],
                             capture_output=True, text=True, timeout=15)
        # Any output at all = at least one event.
        return not out.stdout.strip()
    except Exception:
        return False   # safe default: assume non-empty (still archive)


def extract_and_clear(channel, out_evtx_path):
    """Atomically back up + clear the channel via
    `wevtutil cl <channel> /bu:<out_evtx_path>`.

    The backup produced by the clear IS the archived copy. There is no
    separate `wevtutil epl` export step: `cl /bu:` captures exactly the set of
    events it clears in one OS-level operation, so no events can be lost in the
    gap that a two-step export-then-clear would open. If the backup write
    fails, wevtutil does not clear the channel, so a failure leaves the OS log
    fully intact.

    Returns True on success, False on failure. Failure paths log to the
    diagnostic log and leave the OS log untouched.
    """
    try:
        # Defensive: `cl /bu:` refuses to overwrite an existing backup file.
        # Clear any stale target left behind by a prior crashed run.
        if os.path.exists(out_evtx_path):
            try:
                os.remove(out_evtx_path)
            except Exception:
                logger.error("stale backup target %s could not be removed; "
                             "skipping %s this cycle", out_evtx_path, channel)
                return False
        out = subprocess.run(
            ["wevtutil", "cl", channel, "/bu:" + out_evtx_path],
            capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            logger.error("wevtutil cl %s /bu:%s failed: rc=%d stderr=%s. "
                         "Channel NOT cleared (data safe).",
                         channel, out_evtx_path, out.returncode,
                         out.stderr.strip())
            return False
        if not os.path.exists(out_evtx_path):
            logger.error("wevtutil cl %s reported success but produced no "
                         "backup at %s; treating as failure", channel,
                         out_evtx_path)
            return False
        return True
    except Exception as exc:
        logger.error("extract_and_clear %s exception: %r", channel, exc)
        return False


# =========================================================================== #
# Bundle resolution  (NEW for logmon)
# =========================================================================== #
def _note_disabled_channel(channel):
    """Record a configured-but-OS-disabled channel, logged once and persisted
    to state.disabled_channels for the GUI (design lock 10.13). Kept SEPARATE
    from missing_channels and discovered_unconfigured: it is a distinct
    condition with a distinct remedy (the operator must enable it at the OS)."""
    known = set(read_state().get("disabled_channels", []))
    if channel in known:
        return
    known.add(channel)
    _update_state(lambda st: st.__setitem__("disabled_channels",
                                            sorted(known)))
    logger.warning("channel %s is DISABLED at the OS (enabled: false); it "
                   "produces no events and will not be archived. logmon does "
                   "not change OS log settings -- enable it via Event Viewer "
                   "or Group Policy if you want it captured.", channel)


def _clear_disabled_channel(channel):
    """Drop a channel from disabled_channels once it is enabled again."""
    known = set(read_state().get("disabled_channels", []))
    if channel not in known:
        return
    known.discard(channel)
    _update_state(lambda st: st.__setitem__("disabled_channels",
                                            sorted(known)))
    logger.info("channel %s is enabled again; resuming archival", channel)


def should_skip_channel(channel, include_analytic=False, include_debug=False):
    """Per design lock 10.2 (revised 2026-07-15): default = skip Analytic +
    Debug channels, and always skip channels DISABLED at the OS. The include
    flags are GLOBAL (service-wide), passed in from
    cfg['service']['include_all_analytic'] / ['include_all_debug'].

    Returns a human-readable REASON string if the channel should be skipped, or
    None if it should be archived. (Returning the reason lets the caller log
    accurately -- previously a disabled channel was mislogged as a 'type
    filter' skip.)"""
    md = get_channel_metadata(channel)

    # DISABLED channels (design lock 10.13). `wevtutil gl` reports
    # `enabled: false`. A disabled channel produces NO events, so archiving it
    # is pointless -- and attempting `cl /bu:` on it burns a subprocess every
    # cycle and can feed spurious REPEATED CLEAR FAILURE backoff. Skip it and
    # surface it for the operator.
    #
    # logmon CANNOT enable it: that requires `wevtutil sl /e:true`, which is
    # forbidden (HARD RULE 3). The operator enables it out-of-band via Event
    # Viewer or Group Policy, after which logmon picks it up automatically.
    if md and str(md.get("enabled", "")).strip().lower() == "false":
        _note_disabled_channel(channel)
        return "disabled at OS (enabled: false)"
    if md:
        _clear_disabled_channel(channel)

    if include_analytic and include_debug:
        return None   # user opted into everything
    # Channel type appears under "type:" in gl output. Common values:
    # Operational, Admin, Analytic, Debug.
    ctype = md.get("type", "").lower()
    if ctype == "analytic" and not include_analytic:
        return "Analytic channel (global include_all_analytic is off)"
    if ctype == "debug" and not include_debug:
        return "Debug channel (global include_all_debug is off)"
    return None


# =========================================================================== #
# ALERT STORE (design lock 10.19) -- durable, append-only, SERVICE-written
# =========================================================================== #
# A tamper indicator that sits unread in a log file is worthless. The service
# cannot draw UI (Session 0 isolation), so it PERSISTS alerts and a user-mode
# surface (tray helper / GUI) displays them.
#
# DURABILITY: the alert must be at least as durable as the tamper it reports.
# An attacker who clears the Security log must not be able to trivially erase
# the evidence that they did.
#   1. Append-only JSON lines (never an overwritten blob).
#   2. ACL'd SYSTEM-write / Users-read (best effort; logged if it fails).
#   3. ALSO written into the archive manifest, which is hashed and optionally
#      signed -- so if the alert file is destroyed, the tamper-evident record
#      still survives inside the archive.
# The tray helper is the CONVENIENCE surface; the MANIFEST is AUTHORITATIVE.

ALERTS_FILE_NAME = "alerts.jsonl"
URGENT_FILE_NAME = "URGENT.TXT"

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_WARNING = "WARNING"


def _alerts_path():
    return os.path.join(_logmon_dir(), ALERTS_FILE_NAME)


def _urgent_path():
    return os.path.join(_logmon_dir(), URGENT_FILE_NAME)


def _harden_alert_acl(path):
    """SYSTEM full control, Administrators MODIFY, Users READ-ONLY.

    Purpose: a NON-ADMIN user cannot erase the alert record. Administrators need
    MODIFY because the elevated GUI must APPEND acknowledgment records (it never
    truncates).

    HONEST LIMIT OF THIS CONTROL: clearing the Security log requires
    SeSecurityPrivilege -- i.e. the attacker who triggers an EXTERNAL_CLEAR alert
    is ALREADY an administrator, and can therefore also tamper with this file.
    No ACL can prevent that. The ACL raises the bar against non-admin actors;
    the DURABLE evidence is the copy written into the archive MANIFEST, which is
    hashed, optionally signed, and (properly deployed) shipped OFF-BOX. That is
    the authoritative record -- alerts.jsonl is the operational surface.

    Non-fatal: logged and continued if it fails (non-NTFS, non-Windows)."""
    if not _WINDOWS:
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r",
                        "/grant:r", "SYSTEM:(F)",
                        "/grant:r", "Administrators:(M)",
                        "/grant:r", "Users:(R)"],
                       capture_output=True, text=True, timeout=15)
    except Exception as exc:
        logger.warning("could not harden ACL on %s: %r", path, exc)


def raise_alert(severity, kind, channel, detail, data=None):
    """Append a durable alert record and flag URGENT.TXT for the tray helper.

    Returns the alert dict so callers can embed it in the archive manifest
    (the authoritative copy).
    """
    # Monotonic sequence number. The tray helper keeps a per-user high-water
    # mark against this, so "have I shown this alert?" is unambiguous and
    # survives the alert file being rolled (a line index would not).
    seq_holder = {"n": 0}

    def _bump(st):
        seq_holder["n"] = int(st.get("alert_seq", 0)) + 1
        st["alert_seq"] = seq_holder["n"]
    try:
        _update_state(_bump)
    except Exception:
        pass

    alert = {
        "seq": seq_holder["n"],
        "time_utc": now_utc_str(),
        "severity": severity,
        "kind": kind,
        "channel": channel,
        "detail": detail,
    }
    if data:
        alert["data"] = data
    try:
        os.makedirs(_logmon_dir(), exist_ok=True)
        p = _alerts_path()
        existed = os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:      # APPEND-ONLY
            f.write(json.dumps(alert, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if not existed:
            _harden_alert_acl(p)
        if severity == SEVERITY_CRITICAL:
            with open(_urgent_path(), "w", encoding="utf-8") as f:
                f.write("%s  %s  %s\n%s\n"
                        % (alert["time_utc"], severity, channel, detail))
            _harden_alert_acl(_urgent_path())
    except Exception as exc:
        logger.error("could not persist alert (%s/%s): %r", kind, channel, exc)

    if severity == SEVERITY_CRITICAL:
        logger.error("ALERT [%s] %s: %s", severity, channel, detail)
    else:
        logger.warning("ALERT [%s] %s: %s", severity, channel, detail)
    return alert


# =========================================================================== #
# CHANNEL STATE: watermarks, loss quantification, tamper detection
# (design lock 10.16 / 10.18)
# =========================================================================== #
# Empirically established (Appendix A.5): after a clear, the live log RESETS to
# oldestRecordNumber = 1. It advances ONLY when the OS purges records.
# Therefore, at any capture:
#
#       records destroyed during this period  =  oldestRecordNumber - 1
#
# and a DECREASE in the newest record number, when logmon did NOT clear, means
# SOMEBODY ELSE CLEARED THE LOG.
#
# Note: a clear -- by logmon OR by an attacker -- writes an identical Event 1102.
# 1102 alone therefore CANNOT distinguish them. The watermark plus logmon's own
# last_cleared_by_logmon_utc is what disambiguates.


def get_channel_state(channel=None):
    """channel_state from the state file. Keyed by CHANNEL, not by bundle: the
    watermark is a property of the OS channel, so reassigning or renaming a
    bundle must never reset a channel's loss history."""
    cs = read_state().get("channel_state", {})
    if channel is None:
        return cs
    return cs.get(channel, {})


def observe_channel(channel, at_capture=False):
    """Read a channel's live counters, compare against the stored watermark,
    and record/report any loss or tampering.

    Returns a dict:
        {info, os_config, first_contact, baseline_destroyed,
         destroyed_this_period, external_clear, alerts[]}

    MUST be called BEFORE extract_and_clear() so the pre-capture watermark is
    observed while the evidence still exists.
    """
    info = get_channel_info(channel)
    oscfg = channel_os_config(channel)
    result = {"info": info, "os_config": oscfg, "first_contact": False,
              "baseline_destroyed": None, "destroyed_this_period": 0,
              "external_clear": None, "alerts": [], "is_first_archive": True}
    if not info:
        return result

    oldest = info.get("oldestRecordNumber", 0)
    count = info.get("numberOfLogRecords", 0)
    newest = newest_record_number(info)
    prev = get_channel_state(channel)
    now_s = now_utc_str()

    if not prev:
        # ---- FIRST CONTACT. Capturable exactly ONCE, ever. ----
        # Everything already destroyed here was destroyed BEFORE logmon existed.
        # This draws a permanent line between pre-existing loss (not ours) and
        # anything afterward. If not captured now, it is gone forever.
        destroyed_before = max(0, oldest - 1)
        result["first_contact"] = True
        result["baseline_destroyed"] = destroyed_before
        result["is_first_archive"] = True
        logger.info("channel %s FIRST CONTACT: baseline oldestRecordNumber=%d, "
                    "numberOfLogRecords=%d -> %d record(s) were ALREADY "
                    "DESTROYED by OS circular overwrite before logmon ran",
                    channel, oldest, count, destroyed_before)
        if destroyed_before > 0:
            result["alerts"].append(raise_alert(
                SEVERITY_WARNING, "PRE_LOGMON_LOSS", channel,
                "%d record(s) were already destroyed by OS circular overwrite "
                "before logmon began archiving this channel. This is the "
                "historical baseline, not a logmon gap." % destroyed_before,
                {"baseline_oldest_record": oldest,
                 "records_held_at_baseline": count,
                 "os_config": oscfg}))

        def _m(st):
            st.setdefault("channel_state", {})[channel] = {
                "baseline_oldest_record": oldest,
                "baseline_records_held": count,
                "baseline_seen_utc": now_s,
                "baseline_destroyed_before_logmon": destroyed_before,
                "last_oldest_record": oldest,
                "last_newest_record": newest,
                "last_record_count": count,
                "last_observed_utc": now_s,
                "last_cleared_by_logmon_utc": None,
                "overwritten_since_baseline": 0,
                "external_clears": [],
                "os_config": oscfg,
            }
        _update_state(_m)
        return result

    # Baseline + first-archive status ALWAYS come from stored state, never from
    # the transient first_contact flag: the per-poll observation may already have
    # established the baseline before the capture path runs, and the manifest
    # must still report it.
    result["baseline_destroyed"] = prev.get("baseline_destroyed_before_logmon")
    result["is_first_archive"] = prev.get("last_cleared_by_logmon_utc") is None

    prev_oldest = prev.get("last_oldest_record", 0)
    prev_newest = prev.get("last_newest_record", 0)

    # ---- TAMPER: newest record number went BACKWARD without a logmon clear ----
    if prev_newest and newest and newest < prev_newest:
        detail = ("EXTERNAL CLEAR DETECTED: newest EventRecordID fell from %d "
                  "to %d without logmon clearing this channel. The log was "
                  "cleared by something other than logmon. Clearing an audit "
                  "log is a recognized anti-forensic action. NOTE: Event 1102 "
                  "cannot distinguish this from a logmon clear -- the watermark "
                  "can." % (prev_newest, newest))
        result["external_clear"] = {"prev_newest": prev_newest,
                                    "observed_newest": newest,
                                    "detected_utc": now_s}
        result["alerts"].append(raise_alert(
            SEVERITY_CRITICAL, "EXTERNAL_CLEAR", channel, detail,
            {"prev_newest_record": prev_newest,
             "observed_newest_record": newest,
             "prev_last_observed_utc": prev.get("last_observed_utc"),
             "last_cleared_by_logmon_utc":
                 prev.get("last_cleared_by_logmon_utc")}))

        def _m(st):
            cs = st.setdefault("channel_state", {}).setdefault(channel, {})
            cs.setdefault("external_clears", []).append(
                result["external_clear"])
        _update_state(_m)

    # ---- LOSS: oldest advanced -> the OS purged exactly that many records ----
    purged = 0
    if prev_oldest and oldest > prev_oldest:
        purged = oldest - prev_oldest
        logger.warning("channel %s: OS circular overwrite destroyed %d "
                       "record(s) since last observation (oldestRecordNumber "
                       "%d -> %d)", channel, purged, prev_oldest, oldest)

    # Records destroyed within the CURRENT retention period. After a logmon
    # clear the log restarts at 1, so (oldest - 1) is exactly the loss since
    # that clear.
    destroyed_this_period = max(0, oldest - 1) if prev.get(
        "last_cleared_by_logmon_utc") else 0
    result["destroyed_this_period"] = destroyed_this_period

    if at_capture and destroyed_this_period > 0:
        result["alerts"].append(raise_alert(
            SEVERITY_WARNING, "OVERWRITE_LOSS", channel,
            "%d record(s) were destroyed by OS circular overwrite during this "
            "archive period, BEFORE logmon could capture them. The channel's "
            "OS maxSize (%d bytes) with retention=%s is too small for its event "
            "rate. logmon cannot prevent this (it never alters OS log "
            "configuration); it detects and discloses it."
            % (destroyed_this_period, oscfg.get("max_size", 0),
               oscfg.get("retention")),
            {"records_destroyed": destroyed_this_period,
             "oldest_record_at_capture": oldest,
             "os_config": oscfg}))

    def _m(st):
        cs = st.setdefault("channel_state", {}).setdefault(channel, {})
        cs["last_oldest_record"] = oldest
        cs["last_newest_record"] = newest
        cs["last_record_count"] = count
        cs["last_observed_utc"] = now_s
        cs["os_config"] = oscfg
        if purged:
            cs["overwritten_since_baseline"] = int(
                cs.get("overwritten_since_baseline", 0)) + purged
    _update_state(_m)
    return result


def record_logmon_clear(channel):
    """Mark that LOGMON cleared this channel. Resets the watermark to the
    post-clear reality (oldest=1) so the next observation does not misread
    logmon's own clear as an external one."""
    now_s = now_utc_str()

    def _m(st):
        cs = st.setdefault("channel_state", {}).setdefault(channel, {})
        cs["last_cleared_by_logmon_utc"] = now_s
        cs["last_oldest_record"] = 1
        cs["last_newest_record"] = 0
        cs["last_record_count"] = 0
        cs["last_observed_utc"] = now_s
    _update_state(_m)


# =========================================================================== #
# Unconfigured-channel discovery (NEW for logmon -- design lock 3.4 / 9.1)
# =========================================================================== #
# =========================================================================== #
# Rotation event  (NEW for logmon -- one archive PER bundle / primary channel)
# =========================================================================== #
def _record_bundle_anchor(bundle_name, when):
    """Persist a bundle's rotation_anchor. This is the per-primary-channel
    'last archived at' timestamp future timeframe checks compute boundaries
    from. `when` is a naive-UTC datetime (from _utcnow() / a UTC boundary), so
    the stored string is UTC. Read-modify-write on the config, atomic."""
    def _m(st):
        st.setdefault("bundle_state", {}).setdefault(bundle_name, {})[
            "rotation_anchor"] = when.strftime("%Y-%m-%d %H:%M:%S")
    _update_state(_m)


def _build_provenance(channel, obs, end_dt, size_limit=None,
                      retention_str=None):
    """Assemble the manifest CAVEATS block (design lock 10.17).

    States plainly what the OS -- not logmon -- governed about this channel, and
    exactly how many events were lost before capture. An auditor opening this
    archive years later can see the channel was a 20 MiB circular log logmon did
    not control, and whether records were destroyed before logmon reached them.

    retention_str is the effective legal-retention term for this channel's
    archive ('7y', '' for keep-forever). The delete-after date is computed here
    and mirrored into the archive filename; THIS manifest copy is authoritative
    (design lock 3.2.4 / naming rule 2026-07-15).
    """
    info = obs.get("info", {})
    oscfg = obs.get("os_config", {})
    prov = {
        "captured_utc": end_dt.strftime("%Y-%m-%d %H:%M:%S") + "Z",
        "capture_method": "wevtutil cl <channel> /bu:<archive>  "
                          "(atomic backup-and-clear; verified lossless)",
        "channel": channel,
        "os_channel_config": oscfg,
        "at_capture": {
            "oldest_record_number": info.get("oldestRecordNumber"),
            "number_of_log_records": info.get("numberOfLogRecords"),
            "newest_record_number": newest_record_number(info) or None,
            "file_size": info.get("fileSize"),
        },
        "records_destroyed_by_os_this_period":
            obs.get("destroyed_this_period", 0),
        "completeness_statement": None,
    }
    # What ceiling actually governed this capture (design lock 10.14). The OS
    # maxSize always wins: an operator-configured limit above it is clamped,
    # because a threshold above the OS ceiling can never be reached.
    _osmax = oscfg.get("max_size", 0)
    if _osmax:
        prov["size_trigger"] = {
            "os_max_size": _osmax,
            "effective_limit": _osmax if size_limit is None
                               else min(int(size_limit), _osmax),
            "configured_limit": size_limit,
            "clamped_to_os_max": bool(
                size_limit is not None and int(size_limit) > _osmax),
            "margin": SIZE_TRIGGER_MARGIN,
        }
    # The pre-logmon baseline is ALWAYS disclosed: it is the permanent line
    # between loss that predates logmon and loss on logmon's watch.
    if obs.get("baseline_destroyed") is not None:
        prov["records_destroyed_before_logmon"] = obs.get("baseline_destroyed")
    if obs.get("is_first_archive"):
        prov["first_archive_of_channel"] = True
    if obs.get("external_clear"):
        prov["EXTERNAL_CLEAR_DETECTED"] = obs["external_clear"]

    lost = obs.get("destroyed_this_period", 0)
    if obs.get("is_first_archive"):
        prov["completeness_statement"] = (
            "First capture of this channel. It contains the events present at "
            "capture time. %d record(s) had ALREADY been destroyed by OS "
            "circular overwrite before logmon began archiving; those events are "
            "not recoverable and were never in logmon's custody."
            % (obs.get("baseline_destroyed") or 0))
        prov["records_destroyed_by_os_this_period"] = 0
    elif lost:
        prov["completeness_statement"] = (
            "INCOMPLETE: %d record(s) generated during this period were "
            "destroyed by OS circular overwrite before logmon captured them. "
            "The channel's OS maxSize is too small for its event rate. logmon "
            "does not alter OS log configuration; it detects and discloses the "
            "gap." % lost)
    else:
        prov["completeness_statement"] = (
            "COMPLETE: no OS overwrite loss detected for this period. Every "
            "record written to the channel since the previous logmon capture is "
            "present in this archive.")
    if oscfg and not oscfg.get("retention"):
        prov["caveat_circular_overwrite"] = (
            "This channel is configured retention=false (circular overwrite, "
            "oldest-first). The OS silently discards the oldest events when the "
            "log reaches maxSize (%d bytes). logmon archives on a schedule and "
            "on size, but cannot prevent loss that occurs between polls."
            % oscfg.get("max_size", 0))

    # Legal-retention / delete-after (authoritative copy; the filename mirrors
    # the date for convenience only).
    da_date, da_expires = _delete_after(end_dt, retention_str)
    prov["retention"] = {
        "term": retention_str or "",
        "delete_after_date": da_date,
        "retention_expires_utc": da_expires,
        "note": ("Authoritative retention record. The archive filename repeats "
                 "the DELETE-AFTER date for convenience; this hashed manifest "
                 "is the authority. Deleting before that date, once the archive "
                 "has left logmon's custody, is the holder's responsibility."),
    }
    return prov


def _cleanup_work_dir(work_dir):
    """Remove per-extraction working directory once files are in the zip."""
    try:
        for f in os.listdir(work_dir):
            try:
                os.remove(os.path.join(work_dir, f))
            except Exception:
                pass
        os.rmdir(work_dir)
    except Exception:
        pass


def _mark_bundle_channel_missing(bundle_name, channel):
    """Record LOG MISSING per design lock 10.7: disable further attempts on
    this channel until operator corrects. State stored under
    bundle_state[<bundle>].missing_channels."""
    bs = get_bundle_state(bundle_name)
    missing = set(bs.get("missing_channels", []))
    if channel not in missing:
        missing.add(channel)

        def _m(st):
            st.setdefault("bundle_state", {}).setdefault(
                bundle_name, {})["missing_channels"] = sorted(missing)
        _update_state(_m)
        logger.error("LOG MISSING: channel %s in bundle %s marked as "
                     "missing; will not be attempted until operator "
                     "corrects", channel, bundle_name)


# --------------------------------------------------------------------------- #
# Repeated-clear-failure handling (NEW for logmon -- DISTINCT from LOG MISSING)
# --------------------------------------------------------------------------- #
# A missing channel is gone; a clear-failed channel is present but un-clearable.
# The two live in separate bundle_state lists (missing_channels vs
# clear_failed_channels) and log distinct messages so the operator applies the
# right fix.
def _clear_failure_backoff_until(count, now):
    """Growing backoff: delay doubles each consecutive failure, capped at
    CLEAR_FAILURE_BACKOFF_CAP_SEC. Returns the next-retry datetime."""
    delay = min(CLEAR_FAILURE_BACKOFF_BASE_SEC * (2 ** max(0, count - 1)),
                CLEAR_FAILURE_BACKOFF_CAP_SEC)
    return now + timedelta(seconds=delay)


def _channel_clear_suppressed(bundle_prev_state, channel, now):
    """Return a human-readable reason if the channel should be skipped THIS
    cycle because of clear-failure state -- either disabled (too many
    consecutive failures) or still inside its backoff window -- else ''.

    Pure read against a bundle_state snapshot; no I/O."""
    if channel in set(bundle_prev_state.get("clear_failed_channels", [])):
        return "disabled: repeated clear failure"
    rec = bundle_prev_state.get("clear_failures", {}).get(channel)
    if rec:
        nxt = _parse_anchor(rec.get("next_retry"))
        if nxt is not None and now < nxt:
            return "clear-failure backoff until %s" % rec.get("next_retry")
    return ""


def _record_clear_failure(bundle_name, channel):
    """Increment a channel's consecutive-clear-failure counter, set a growing
    backoff, and -- once CLEAR_FAILURE_DISABLE_THRESHOLD consecutive failures
    is reached -- disable the channel via a DISTINCT 'clear_failed_channels'
    state (separate from 'missing_channels').

    Deliberately named apart from the missing-channel path: a REPEATED CLEAR
    FAILURE means the channel is present but cannot be cleared (channelAccess
    SDDL, Analytic/Debug status, unreachable backup path, held handle), which
    the operator resolves differently from a channel that has gone missing."""
    now = _utcnow()
    holder = {"rec": None, "disabled_now": False}

    def _m(st):
        bs = st.setdefault("bundle_state", {}).setdefault(bundle_name, {})
        failures = bs.setdefault("clear_failures", {})
        rec = failures.get(channel, {"count": 0})
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["last_failure"] = now.strftime("%Y-%m-%d %H:%M:%S")
        rec.setdefault("first_failure", rec["last_failure"])
        rec["next_retry"] = _clear_failure_backoff_until(
            rec["count"], now).strftime("%Y-%m-%d %H:%M:%S")
        failures[channel] = rec
        if rec["count"] >= CLEAR_FAILURE_DISABLE_THRESHOLD:
            cfd = set(bs.get("clear_failed_channels", []))
            if channel not in cfd:
                cfd.add(channel)
                bs["clear_failed_channels"] = sorted(cfd)
                holder["disabled_now"] = True
        bs["clear_failures"] = failures
        holder["rec"] = rec
    _update_state(_m)
    rec = holder["rec"]
    disabled_now = holder["disabled_now"]
    if disabled_now:
        logger.error("REPEATED CLEAR FAILURE: channel %s in bundle %s failed "
                     "to clear %d consecutive times; DISABLED until operator "
                     "corrects. Channel exists but cannot be cleared -- check "
                     "channelAccess/SDDL, Analytic/Debug status, reachability "
                     "of the archive backup path, and any process holding the "
                     "log. (Distinct from LOG MISSING: the channel is present, "
                     "not gone.)", channel, bundle_name, rec["count"])
    else:
        logger.warning("clear failure #%d for channel %s in bundle %s; backing "
                       "off, next retry after %s", rec["count"], channel,
                       bundle_name, rec["next_retry"])


def _reset_clear_failure(bundle_name, channel):
    """On a successful clear, drop the channel's failure record and lift the
    REPEATED CLEAR FAILURE disable if it was set. No-op if nothing was
    recorded."""
    bs = get_bundle_state(bundle_name)
    lifting = channel in bs.get("clear_failed_channels", [])
    if channel not in bs.get("clear_failures", {}) and not lifting:
        return

    def _m(st):
        b = st.setdefault("bundle_state", {}).setdefault(bundle_name, {})
        b.get("clear_failures", {}).pop(channel, None)
        cfd = b.get("clear_failed_channels", [])
        if channel in cfd:
            b["clear_failed_channels"] = [c for c in cfd if c != channel]
    _update_state(_m)
    if lifting:
        logger.info("channel %s in bundle %s cleared successfully; lifting "
                    "REPEATED CLEAR FAILURE disable", channel, bundle_name)


# =========================================================================== #
# Trigger evaluation  (NEW for logmon)
# =========================================================================== #
def _time_trigger_boundary(bundle_name, bundle_cfg, bundle_state, now):
    """Return the rotation boundary that has fired since the last archive, or
    None if the timeframe trigger has not fired.

    The returned boundary (NOT wall-clock `now`) is used as the archive's
    end_dt, so a calendar rotation detected shortly after the boundary snaps
    back to the boundary instant. Example: a 1M rotation whose boundary is
    00:00 on the 1st, detected by the 00:05 poll, names the archive for the
    month that just closed rather than the 00:05 wall-clock time.

    Uses the same _next_rotation_boundary math as usnmon (LITERAL duplication
    of the calendar-boundary logic). Returns None when the bundle has no anchor
    yet -- the first run is handled by the historical bootstrap in
    evaluate_active_channels, which seeds the anchor."""
    if not bundle_cfg.get("rotate", False):
        return None
    tf = parse_interval(bundle_cfg.get("timeframe", ""))
    if not tf:
        return None
    anchor = _parse_anchor(
        bundle_state.get(bundle_name, {}).get("rotation_anchor"))
    if anchor is None:
        return None
    boundary = _next_rotation_boundary(anchor, tf)
    if boundary is None:
        return None
    return boundary if now >= boundary else None


def effective_size_limit(channel, configured_limit, os_max=None):
    """Resolve the OPERATIVE size ceiling for ONE channel (design lock 10.14).

    The OS enforces a per-channel `maxSize`; logmon never changes it (HARD RULE
    3). Measured on a live host: Security/System/Application = 20 MiB,
    Windows PowerShell = 15 MiB -- NOT the 4 GiB the old global validator
    assumed. Two failures follow from ignoring that:

      * configured_limit is None  -> the OLD code disabled the size trigger
        entirely. On a 20 MiB CIRCULAR channel with a 30d timeframe, the log
        fills in ~13 days, wraps, and sheds events silently for the rest of the
        period. Now None means "use the channel's own maxSize".

      * configured_limit > channel maxSize -> the threshold is UNREACHABLE. A
        3.5 GiB limit on a 20 MiB channel can never trip, so the channel churns
        forever under circular overwrite. Now it is CLAMPED to maxSize and the
        operator is warned once.

    Returns (limit_bytes, was_clamped) or (None, False) if the OS max is
    unknown and no explicit limit was configured (cannot infer a ceiling; the
    size trigger is skipped for that channel and the timeframe still applies).
    """
    if os_max is None:
        os_max = channel_os_config(channel).get("max_size", 0)

    if configured_limit is None:
        if not os_max:
            return None, False
        return int(os_max), False

    configured_limit = int(configured_limit)
    if os_max and configured_limit > os_max:
        if channel not in _UNREACHABLE_WARNED:
            _UNREACHABLE_WARNED.add(channel)
            logger.warning(
                "channel %s: configured size_limit_bytes=%d EXCEEDS the OS "
                "maxSize of %d. That threshold could never be reached, so the "
                "size trigger would never fire and the channel would churn "
                "under OS overwrite. Clamping to %d. Fix the bundle's size "
                "limit (or set it to null to always track the OS maxSize). "
                "logmon does not alter OS log settings.",
                channel, configured_limit, os_max, os_max)
        return int(os_max), True
    return configured_limit, False


# =========================================================================== #
# SCHEMA-v2 ARCHIVE ENGINE (Pass 2, 2026-07-15) -- grouped by (rotation,
# retention). One archive+manifest set per distinct due pair; DELETE-AFTER
# naming; flat archive root.
# =========================================================================== #
# Each (rotation, retention) group is treated as a "bundle" for the purposes of
# the tested state machinery (rotation anchor, missing-channel and clear-failure
# tracking) by using the group id string as the state key. A channel has exactly
# one effective policy, hence exactly one group, so this keying is unambiguous.


def _group_id(rotation, retention):
    """Stable state key for a (rotation, retention) group. rotation is the
    timeframe string ('1M') or None (rotate off); retention is the term ('7y')
    or '' (keep forever)."""
    return "rot-%s_ret-%s" % (rotation or "none", retention or "forever")


def _delete_after(end_dt, retention_str):
    """Compute the DELETE-AFTER calendar date for an archive covering up to
    end_dt under retention_str.

    Returns (date_str, expires_utc_iso):
      - retention set    -> ('YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SSZ')
      - keep-forever     -> ('NEVER', None)

    The date is day-floored. The pruner deletes only once the CURRENT UTC date
    is STRICTLY GREATER than this date (i.e. from the following midnight), so:
      (a) the printed date is a safe 'you may delete on/after the next day'
          guarantee for a human reading the filename, and
      (b) logmon never prunes before the precise expiry instant, which is always
          <= end-of-printed-day.
    """
    term = parse_retention(retention_str) if retention_str else None
    if not term:
        return "NEVER", None
    n, unit = term
    expires = _period_advance(end_dt, n, unit, direction=1)
    return (expires.strftime("%Y-%m-%d"),
            expires.strftime("%Y-%m-%d %H:%M:%S") + "Z")


def _group_archive_basename(end_dt, start_dt, rotation_str, retention_str,
                            historical):
    """Archive filename base (no extension), flat in the archive root.

    Schema-v2 naming (design lock, 2026-07-15, tag added 2026-07-16): the
    filename states the COVERAGE WINDOW, the group's POLICY (rotation + retention
    terms), and the DELETE-AFTER date -- NOT the contents (channels are named by
    their own .evtx inside the zip). The rot-/ret- tag is the group identity, so
    two distinct groups written in the same cycle can never collide, and an
    operator can read the retention term straight off the name.

        <start>_to_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date|NEVER>
        historical_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date|NEVER>

    ROT is the rotation term or 'NONE' (rotate off); RET is the retention term
    or 'NONE' (keep forever, which always pairs with DELETE-AFTER_NEVER).
    """
    da_date, _ = _delete_after(end_dt, retention_str)
    tag = "rot-%s_ret-%s" % (rotation_str or "NONE", retention_str or "NONE")
    if historical:
        return "historical_%s_%s_DELETE-AFTER_%s" % (
            _archive_format_date(end_dt, True), tag, da_date)
    return "%s_to_%s_%s_DELETE-AFTER_%s" % (
        _archive_format_date(start_dt, True),
        _archive_format_date(end_dt, True), tag, da_date)


def _size_archive_basename(chan, end_dt, start_dt, rotation_str,
                           retention_str):
    """Archive filename base for a SINGLE-channel, SIZE-triggered rotation.

    A size trip is a per-channel condition (one log filled its cap), so unlike a
    time rotation it does not sweep the whole group. The name leads with the
    channel so these are visually distinct from group time-archives, and carries
    the same policy tag + DELETE-AFTER date the pruner keys off:

        <Channel>_size_<start>_to_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date>

    `chan` is sanitized (/ and \\ -> _) for filesystem safety.
    """
    da_date, _ = _delete_after(end_dt, retention_str)
    tag = "rot-%s_ret-%s" % (rotation_str or "NONE", retention_str or "NONE")
    safe_chan = chan.replace("/", "_").replace("\\", "_")
    return "%s_size_%s_to_%s_%s_DELETE-AFTER_%s" % (
        safe_chan,
        _archive_format_date(start_dt, True),
        _archive_format_date(end_dt, True), tag, da_date)


def group_size_tripped_channels(cfg, group, all_channels_cached):
    """Return the list of channels in the group that have reached their OWN
    effective size threshold (per-channel maxSize clamp, design lock 10.14).

    A size trip is per-channel: only the channel(s) that filled up are rotated,
    NOT the whole group (a chatty channel must not drag its group-mates or reset
    the group's time anchor). Time rotations remain group-wide.
    """
    gid = _group_id(group["rotation"], group["retention"])
    gstate = get_bundle_state(gid)
    missing = set(gstate.get("missing_channels", []))
    now = _utcnow()
    tripped = []
    for chan in group["channels"]:
        if chan in missing:
            continue
        if _channel_clear_suppressed(gstate, chan, now):
            continue
        eff = effective_channel_policy(cfg, chan)
        if not eff:
            continue
        limit, clamped = effective_size_limit(chan, eff.get("size_limit_bytes"))
        if not limit:
            continue
        size = channel_file_size(chan)
        if size >= int(limit * SIZE_TRIGGER_MARGIN):
            logger.info("group %s: channel %s size trigger fired "
                        "(size=%d threshold=%d effective_limit=%d%s)",
                        gid, chan, size, int(limit * SIZE_TRIGGER_MARGIN),
                        limit, " CLAMPED to OS maxSize" if clamped else "")
            tripped.append(chan)
    return tripped


def _extract_channel_for_archive(chan, work_dir, end_dt, cfg, gid,
                                 retention_str, prev, now, inc_analytic,
                                 inc_debug, missing):
    """Extract + clear ONE channel and write its manifest into work_dir.

    Shared by the group (time) archiver and the single-channel (size) archiver
    so the lossless capture path lives in exactly one place. Returns
    (evtx_path, manifest_path, safe_chan) on success, or None if the channel was
    skipped or its capture failed (all reasons logged here).
    """
    if chan in missing:
        return None
    suppressed = _channel_clear_suppressed(prev, chan, now)
    if suppressed:
        logger.info("skipping %s in %s (%s)", chan, gid, suppressed)
        return None
    skip_reason = should_skip_channel(chan, inc_analytic, inc_debug)
    if skip_reason:
        logger.info("skipping %s in %s (%s)", chan, gid, skip_reason)
        return None
    if not channel_exists(chan):
        logger.error("channel %s missing at archive time; skipping", chan)
        _mark_bundle_channel_missing(gid, chan)
        return None
    if channel_is_empty(chan):
        logger.info("skipping %s in %s (empty channel)", chan, gid)
        return None
    # OBSERVE BEFORE CLEARING -- one-shot baseline + tamper watermark.
    obs = observe_channel(chan, at_capture=True)

    safe_chan = chan.replace("/", "_").replace("\\", "_")
    evtx_fname = "%s_%s.evtx" % (safe_chan, _archive_format_date(end_dt, True))
    evtx_path = os.path.join(work_dir, evtx_fname)
    if not extract_and_clear(chan, evtx_path):
        _record_clear_failure(gid, chan)
        return None
    _reset_clear_failure(gid, chan)
    record_logmon_clear(chan)

    eff = effective_channel_policy(cfg, chan) or {}
    provenance = _build_provenance(chan, obs, end_dt,
                                   size_limit=eff.get("size_limit_bytes"),
                                   retention_str=retention_str)
    manifest_path = write_manifest(evtx_path, bundle_name=gid,
                                   provenance=provenance)
    return evtx_path, manifest_path, safe_chan


def archive_one_group(archive_root, cfg, group, rotate_period, end_dt,
                      reason="scheduled", historical=False,
                      inc_analytic=False, inc_debug=False):
    """Extract + clear every active channel in ONE (rotation, retention) group
    and zip them into a single archive placed FLAT in archive_root, then record
    the group's rotation anchor.

    Every channel in the group shares one retention term, so the whole archive
    is cleanly prunable on that term (the one invariant that must never break).

    This is the TIME/historical path (group-wide). Size trips are handled
    per-channel by archive_one_size_channel and never enter here.
    """
    gid = _group_id(group["rotation"], group["retention"])
    retention_str = group["retention"]
    os.makedirs(archive_root, exist_ok=True)
    work_dir = os.path.join(archive_root, "._logmon_work_%s" % gid)
    os.makedirs(work_dir, exist_ok=True)

    prev = get_bundle_state(gid)
    start_anchor = _parse_anchor(prev.get("rotation_anchor"))
    start_dt = start_anchor or end_dt

    base = _group_archive_basename(end_dt, start_dt, group["rotation"],
                                   retention_str, historical)
    zip_path = os.path.join(archive_root, base + ".zip")
    if os.path.exists(zip_path):
        i = 2
        while os.path.exists(os.path.join(archive_root,
                                          base + "_dup%d.zip" % i)):
            i += 1
        zip_path = os.path.join(archive_root, base + "_dup%d.zip" % i)

    missing = set(prev.get("missing_channels", []))
    now = _utcnow()
    extracted_files = []
    for chan in group["channels"]:
        res = _extract_channel_for_archive(
            chan, work_dir, end_dt, cfg, gid, retention_str, prev, now,
            inc_analytic, inc_debug, missing)
        if not res:
            continue
        evtx_path, manifest_path, safe_chan = res
        # Per-channel folder inside the zip (browsable; mirrors Event Viewer's
        # doubled naming, e.g. Security/Security_<ts>.evtx).
        arc_evtx = "%s/%s" % (safe_chan, os.path.basename(evtx_path))
        arc_mfst = "%s/%s" % (safe_chan, os.path.basename(manifest_path))
        extracted_files.append((evtx_path, arc_evtx))
        extracted_files.append((manifest_path, arc_mfst))

    if not extracted_files:
        logger.warning("group %s: no channels extracted; no zip written", gid)
        _cleanup_work_dir(work_dir)
        if historical:
            _record_bundle_anchor(gid, end_dt)   # don't re-dump every cycle
        return None

    try:
        build_zip_bundle(zip_path, extracted_files)
        _cleanup_work_dir(work_dir)
    except Exception as exc:
        logger.error("zip build failed for group %s: %r", gid, exc)
        return None

    _record_bundle_anchor(gid, end_dt)
    logger.info("archive: group=%s reason=%s channels=%d zip=%s",
                gid, reason, len(group["channels"]), os.path.basename(zip_path))
    return zip_path


def archive_one_size_channel(archive_root, cfg, group, chan, end_dt,
                             inc_analytic=False, inc_debug=False):
    """Archive a SINGLE channel that tripped its size cap, into its own zip.

    A size trip is a per-channel condition, so only this channel is extracted
    and cleared -- its group-mates are untouched, and the group's TIME anchor is
    NOT reset (the monthly/period boundary still lands where the calendar says).
    The archive is still tagged with the group's rotation+retention so it prunes
    on the same term and the pruner's DELETE-AFTER logic is unchanged.
    """
    gid = _group_id(group["rotation"], group["retention"])
    retention_str = group["retention"]
    os.makedirs(archive_root, exist_ok=True)
    work_dir = os.path.join(archive_root, "._logmon_work_%s" % gid)
    os.makedirs(work_dir, exist_ok=True)

    prev = get_bundle_state(gid)
    missing = set(prev.get("missing_channels", []))
    now = _utcnow()

    # Coverage start = the last time logmon cleared THIS channel (its true
    # per-channel span), falling back to the group anchor, then to end_dt.
    cst = get_channel_state(chan)
    start_dt = (_parse_anchor(cst.get("last_cleared_by_logmon_utc"))
                or _parse_anchor(prev.get("rotation_anchor"))
                or end_dt)

    res = _extract_channel_for_archive(
        chan, work_dir, end_dt, cfg, gid, retention_str, prev, now,
        inc_analytic, inc_debug, missing)
    if not res:
        _cleanup_work_dir(work_dir)
        return None
    evtx_path, manifest_path, safe_chan = res

    base = _size_archive_basename(chan, end_dt, start_dt, group["rotation"],
                                  retention_str)
    zip_path = os.path.join(archive_root, base + ".zip")
    if os.path.exists(zip_path):
        i = 2
        while os.path.exists(os.path.join(archive_root,
                                          base + "_dup%d.zip" % i)):
            i += 1
        zip_path = os.path.join(archive_root, base + "_dup%d.zip" % i)

    arc_evtx = "%s/%s" % (safe_chan, os.path.basename(evtx_path))
    arc_mfst = "%s/%s" % (safe_chan, os.path.basename(manifest_path))
    try:
        build_zip_bundle(zip_path, [(evtx_path, arc_evtx),
                                    (manifest_path, arc_mfst)])
        _cleanup_work_dir(work_dir)
    except Exception as exc:
        logger.error("size-archive zip build failed for %s: %r", chan, exc)
        return None

    # NOTE: deliberately does NOT call _record_bundle_anchor -- a size trip must
    # not move the group's time boundary.
    logger.info("archive: channel=%s group=%s reason=size zip=%s",
                chan, gid, os.path.basename(zip_path))
    return zip_path


def log_unconfigured_discoveries_v2(all_channels, cfg):
    """Surface channels present on the box but matched by no provider/channel
    config. Logged once each; persisted to state.discovered_unconfigured for the
    GUI. (schema-v2 replacement for the bundle-selector discovery.)"""
    configured = set()
    for _pk, p in cfg.get("providers", {}).items():
        configured.update(p.get("channels", {}).keys())
    current = set(all_channels) - configured
    known = set(read_state().get("discovered_unconfigured", []))
    for chan in sorted(current - known):
        logger.info("unconfigured channel discovered: %s (matched by no "
                    "provider/channel; assign it via the GUI to begin "
                    "archiving)", chan)
    if current != known:
        _update_state(lambda st: st.__setitem__("discovered_unconfigured",
                                                sorted(current)))


def evaluate_active_channels(archive_root, cfg):
    """One poll cycle (schema v2). Group active channels by (rotation,
    retention); produce at most one archive per due group.

    For each group:
      - First run (no anchor): historical bootstrap of the group's channels,
        then seed the group anchor.
      - Time trigger: archive the closed period, end_dt snapped to the boundary.
      - Size trigger: archive now (extra archive within the period) if any
        channel in the group reached its own size threshold.
    """
    now = _utcnow()
    all_channels_cached = enumerate_channels()
    svc = cfg.get("service", {})
    inc_analytic = bool(svc.get("include_all_analytic", False))
    inc_debug = bool(svc.get("include_all_debug", False))

    if all_channels_cached:
        log_unconfigured_discoveries_v2(all_channels_cached, cfg)

    # Build (rotation, retention) groups from active channels.
    groups = {}
    for chan, eff in iter_active_channels(cfg):
        rotation, retention = channel_group_key(eff)
        gid = _group_id(rotation, retention)
        g = groups.setdefault(gid, {"rotation": rotation,
                                    "retention": retention, "channels": []})
        g["channels"].append(chan)

    # Observe every active channel EVERY poll (tamper/loss detection between
    # captures), not only at capture time.
    for chan, _eff in iter_active_channels(cfg):
        try:
            observe_channel(chan, at_capture=False)
        except Exception as exc:
            logger.error("observe_channel %s failed: %r", chan, exc)

    for gid, g in groups.items():
        rotation = g["rotation"]
        rotate_period = parse_interval(rotation) if rotation else None
        gstate = get_bundle_state(gid)

        anchor = _parse_anchor(gstate.get("rotation_anchor"))
        if anchor is None:
            logger.info("group %s: first run, archiving historical contents "
                        "(%d channel(s))", gid, len(g["channels"]))
            archive_one_group(archive_root, cfg, g, rotate_period, now,
                              reason="historical", historical=True,
                              inc_analytic=inc_analytic, inc_debug=inc_debug)
            continue

        boundary = None
        if rotation:
            boundary = _time_trigger_boundary(
                gid, {"rotate": True, "timeframe": rotation},
                {gid: gstate}, now)
        if boundary is not None:
            logger.info("group %s: time trigger fired (boundary=%s)",
                        gid, boundary.strftime("%Y-%m-%d %H:%M:%S"))
            archive_one_group(archive_root, cfg, g, rotate_period, boundary,
                              reason="scheduled",
                              inc_analytic=inc_analytic, inc_debug=inc_debug)
            continue

        # Size trips are per-channel: archive ONLY the channel(s) that filled,
        # each into its own zip. The group's time anchor is left untouched so a
        # chatty channel can't drag its group-mates or shift the calendar
        # boundary.
        for chan in group_size_tripped_channels(cfg, g, all_channels_cached):
            archive_one_size_channel(archive_root, cfg, g, chan, now,
                                     inc_analytic=inc_analytic,
                                     inc_debug=inc_debug)


def prune_by_delete_after(archive_root):
    """Legal-retention sweep (schema v2). Flat archive root: delete any archive
    file whose DELETE-AFTER date has fully passed (current UTC date strictly
    greater than the printed date). DELETE-AFTER_NEVER is never pruned.

    The delete-after date is read from the filename; the authoritative copy is
    in each manifest. This replaces the v1 per-subdir span-regex pruner: the
    delete date is now stated explicitly in the name, so pruning is exact and
    self-consistent with the promise printed on the file.
    """
    rx = re.compile(r"_DELETE-AFTER_(\d{4}-\d{2}-\d{2}|NEVER)")
    today = _utcnow().date()
    deleted = 0
    try:
        entries = os.listdir(archive_root)
    except Exception:
        return 0
    for f in entries:
        if not f.lower().endswith((".zip", ".manifest", ".evtx")):
            continue
        m = rx.search(f)
        if not m or m.group(1) == "NEVER":
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except Exception:
            continue
        if today > d:
            try:
                os.remove(os.path.join(archive_root, f))
                deleted += 1
                logger.info("legal-retention: pruned %s (delete-after %s "
                            "passed)", f, m.group(1))
            except Exception as exc:
                logger.error("legal-retention: failed to prune %s: %r", f, exc)
    return deleted


# =========================================================================== #
# Config reload watcher  (NEW for logmon -- mtime watch + poll safety net)
# =========================================================================== #
class ConfigReloader:
    """Watches config file mtime for changes. Provides current-config accessor
    that reloads on demand. Per design lock 3.3: on-demand + poll-interval
    combined."""

    def __init__(self):
        self._path = _config_path()
        self._last_mtime = 0
        self._last_reload = 0
        self._cfg = {}
        self._lock = threading.Lock()

    def get_config(self, force=False):
        """Return current config, reloading if mtime changed or poll interval
        elapsed. force=True bypasses the interval check."""
        with self._lock:
            try:
                mtime = os.path.getmtime(self._path)
            except Exception:
                mtime = 0
            now = time.time()
            reload_interval = self._cfg.get("service", {}).get(
                "config_reload_interval_sec",
                DEFAULT_CONFIG_RELOAD_INTERVAL_SEC)
            should_reload = (
                force
                or mtime != self._last_mtime
                or (now - self._last_reload) > reload_interval)
            if should_reload:
                self._cfg = read_config()
                self._last_mtime = mtime
                self._last_reload = now
            return self._cfg


# =========================================================================== #
# Retention scheduler  (NEW for logmon -- uses copied prune logic)
# =========================================================================== #
class RetentionScheduler:
    """Runs prune_by_delete_after on a daily cadence. Per design lock 6.2 (v2:
    flat delete-after pruning)."""

    def __init__(self, archive_root):
        self._archive_root = archive_root
        self._last_run = 0

    def maybe_run(self, cfg):
        interval_hours = cfg.get("service", {}).get(
            "retention_check_interval_hours", DEFAULT_RETENTION_CHECK_HOURS)
        interval_sec = interval_hours * 3600
        now = time.time()
        if (now - self._last_run) < interval_sec:
            return
        try:
            deleted = prune_by_delete_after(self._archive_root)
            if deleted:
                logger.info("retention sweep: deleted %d file(s)", deleted)
        except Exception as exc:
            logger.error("retention sweep failed: %r", exc)
        self._last_run = now


# =========================================================================== #
# Main engine loop  (NEW for logmon -- structured like usnmon.run_engine but
# operating on Event Log channels via wevtutil)
# =========================================================================== #
def run_engine(should_stop, archive_root):
    """logmon service main loop. Runs until should_stop() returns True.

    Each cycle:
      - Reload config if mtime changed
      - Evaluate all bundles for triggers, run rotation event on tripped
      - Run retention sweep if daily interval elapsed
      - Sleep until next poll interval

    Poll interval default is 5 minutes; overridable via config
    poll_interval_sec.
    """
    reloader = ConfigReloader()
    retention = RetentionScheduler(archive_root)

    logger.info("logmon engine started; archive_root=%s", archive_root)
    while not should_stop():
        try:
            cfg = reloader.get_config()
            poll_sec = cfg.get("service", {}).get(
                "poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC)
            if cfg.get("providers"):
                evaluate_active_channels(archive_root, cfg)
            retention.maybe_run(cfg)
        except Exception as exc:
            logger.exception("engine cycle failed: %r", exc)

        # Interruptible sleep: check stop signal every second.
        slept = 0
        while slept < poll_sec and not should_stop():
            time.sleep(1)
            slept += 1

    logger.info("logmon engine stopping")


# =========================================================================== #
# Windows service wrapper  (LITERAL DUPLICATION from usnmon.py -- renamed)
# =========================================================================== #
if _WINDOWS and win32serviceutil is not None:
    class LogMonitorService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.h_stop = win32event.CreateEvent(None, 0, 0, None)
            self._archive_root = read_archive_root()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.h_stop)

        def should_stop(self):
            return win32event.WaitForSingleObject(self.h_stop, 0) == \
                win32event.WAIT_OBJECT_0

        def SvcDoRun(self):
            setup_logging(False, self._archive_root)
            run_engine(self.should_stop, self._archive_root)


    def query_service_start_type():
        """Read the service's ACTUAL configured start mode from Windows.
        LITERAL DUPLICATION from usnmon.py, service name adapted."""
        import win32service as _ws
        try:
            scm = _ws.OpenSCManager(None, None, _ws.SC_MANAGER_CONNECT)
            try:
                h = _ws.OpenService(scm, SERVICE_NAME,
                                    _ws.SERVICE_QUERY_CONFIG)
                try:
                    cfg = _ws.QueryServiceConfig(h)
                    start = cfg[1]
                    if start == _ws.SERVICE_AUTO_START:
                        try:
                            delayed = _ws.QueryServiceConfig2(
                                h, _ws.SERVICE_CONFIG_DELAYED_AUTO_START_INFO)
                            return "delayed" if delayed else "auto"
                        except Exception:
                            return "auto"
                    if start == _ws.SERVICE_DEMAND_START:
                        return "manual"
                    if start == _ws.SERVICE_DISABLED:
                        return "disabled"
                    return "unknown"
                finally:
                    _ws.CloseServiceHandle(h)
            finally:
                _ws.CloseServiceHandle(scm)
        except Exception:
            return "unknown"


# =========================================================================== #
# CLI subcommands (skeleton -- v0.0.1 shipping will add console + editor)
# =========================================================================== #
def run_console_debug():
    """Console engine run for debugging. LITERAL PATTERN from usnmon."""
    archive_root = read_archive_root()
    setup_logging(True, archive_root)
    stop_flag = {"stop": False}

    def should_stop():
        return stop_flag["stop"]

    try:
        run_engine(should_stop, archive_root)
    except KeyboardInterrupt:
        stop_flag["stop"] = True


def main():
    """CLI entrypoint. LITERAL PATTERN from usnmon.main() -- subcommand
    dispatch, --archive parsed as a pywin32-compatible pre-flag, then pywin32
    HandleCommandLine for service ops.

    NOTE: usnmon's --log-interval pre-flag is deliberately NOT carried over.
    It set a GLOBAL default rotation cadence, but logmon requires a `timeframe`
    on every rotating bundle (validate_config disables a bundle that lacks one),
    so a global fallback could never fire. Keeping it would have meant an
    unvalidated raw config key that silently did nothing. Rotation cadence is
    per bundle, set in the GUI.

    v0.0.1 skeleton: implements enough for install/start/stop/uninstall +
    debug. Full config editor GUI (P5) still required to ship v0.0.1."""
    args = sys.argv[1:]

    # --archive <path>: archive root override, persisted to config.
    archive_root = DEFAULT_ARCHIVE_DIR
    if "--archive" in args:
        i = args.index("--archive")
        archive_root = args[i + 1]
        del args[i:i + 2]
        write_config(archive_root)

    if args and args[0] == "debug":
        run_console_debug()
        return

    if _WINDOWS and win32serviceutil is not None:
        sys.argv = [sys.argv[0]] + args
        if not args:
            try:
                servicemanager.Initialize()
                servicemanager.PrepareToHostSingle(LogMonitorService)
                servicemanager.StartServiceCtrlDispatcher()
            except Exception:
                win32serviceutil.HandleCommandLine(LogMonitorService)
        else:
            is_install = "install" in args
            if is_install:
                write_config(archive_root)
            win32serviceutil.HandleCommandLine(LogMonitorService)
            if is_install:
                try:
                    st = query_service_start_type()

                    def _m(state):
                        eng = state.setdefault("engine", {})
                        eng["service_start_type"] = st
                        eng.setdefault("install_time_utc", now_utc_str())
                    _update_state(_m)
                except Exception as exc:
                    logger.debug("could not record start type: %r", exc)
    else:
        print("logmon: Windows required for service/capture. Use 'debug' on "
              "Windows. (Imported OK on this platform.)")


if __name__ == "__main__":
    main()
