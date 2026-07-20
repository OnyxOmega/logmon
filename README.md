# logmon

**Tamper-evident Windows Event Log archiver with legally defensible retention.**

`logmon` is a Windows service that captures Windows Event Log channels on a
rotation schedule, archives them as hashed (and optionally signed) zip archives
with per-channel manifests, detects tampering and pre-capture loss, and prunes
archives according to per-channel legal-retention rules.

It is a **companion** to the OS Event Log system, not a replacement. It never
alters OS log configuration.

> **Status:** v0.0.1 (pre-release). Validated on real hardware for the capture,
> hashing, baseline, and tamper-detection paths; standalone service
> verification on Windows is in progress.

---

## What logmon does

- **Captures** configured Event Log channels via a single atomic
  `wevtutil cl <channel> /bu:<archive>` (backup-and-clear) — proven lossless on
  real hardware.
- **Archives** by `(rotation, retention)` group: one zip per distinct policy
  group per due cycle, each with per-channel `.evtx` files and hashed manifests
  (MD5/SHA-1 for interop, SHA-256/SHA-512 authoritative; optional RSA/ECDSA
  signatures).
- **Proves completeness or discloses its absence.** Every manifest carries a
  provenance block stating the channel's OS configuration, the exact count of
  any records destroyed by OS circular-overwrite, and a plain-language
  completeness statement.
- **Detects tampering.** A one-shot pre-logmon loss baseline is captured on
  first contact with each channel; any later clear that logmon did not perform
  raises a CRITICAL `EXTERNAL_CLEAR` alert (logmon cannot know intent, so it
  flags every unexplained clear).
- **Retains legally.** Archives are named with an explicit `DELETE-AFTER` date
  and pruned only after that date passes — the filename is a truthful,
  self-describing retention statement even off-system.

## What logmon does NOT do

- **Never** alters OS Event Log settings (no `wevtutil sl`).
- **Never** interferes with WEF/WEC subscriptions.
- **Never** produces events into the channels it archives.
- Not real-time; it operates on scheduled polling cycles.
- Windows only. No cross-platform runtime.

---

## Components

| File | Role | Privilege |
|---|---|---|
| `logmon.py` | The Windows service (capture, hash, archive, retention, alerts) | LocalSystem |
| `logmon_gui.py` | Configuration editor — channels, providers, retention, alerts | Administrator (elevated) |
| `logmon_tray.py` | Per-user system-tray alert watcher | Unprivileged |
| `logmon_reset.py` | Test-system reset utility | Administrator |

The GUI and tray are deliberately separate: the config editor must elevate to
write `logmon.cfg`, while the alert watcher runs unprivileged at every login
and is read-only on everything logmon owns.

---

## Requirements

- **Windows** (Windows 10 / 11 / Server; 64-bit Python recommended)
- **Python 3.12**
- **`pywin32`** — required for the service
- **`PySide6`** — required for the GUI and tray
- **`cryptography`** — optional, only for signed manifests

```
pip install pywin32 PySide6
pip install cryptography          # only if you want signed manifests
python -m pywin32_postinstall -install   # elevated prompt, once, after pywin32
```

`logmon_reset.py` has no third-party dependencies.

---

## Quick start

From an **elevated** prompt:

```
:: install and start the service
python logmon.py --archive C:\EVENT_LOG_ARCHIVE --startup auto install
python logmon.py start

:: open the configuration editor (elevated)
python logmon_gui.py
```

Then, per user, start the unprivileged alert watcher (it can auto-start at
login from its tray menu):

```
pythonw logmon_tray.py
```

In the GUI's **Channels** tab, select the channels you want archived (or use
**Add Recommended**), set rotation/retention at the provider or channel level,
and **Save**. The service reloads within ~5 minutes (or on the next cycle).

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full install/uninstall guide and
coexistence notes, and [CONFIG.md](CONFIG.md) for the configuration schema.

---

## How it is configured

Two files, each with exactly **one writer**, to eliminate lost-update races
between the GUI and the service:

- **`C:\ProgramData\logmon\logmon.cfg`** — operator/GUI owned. The service only
  reads it.
- **`C:\ProgramData\logmon\logmon_state.json`** — service owned. The GUI reads
  it for status.

Policy attaches to two OS-real objects: a **Provider** (a channel's
`owningPublisher`, or the Event-Viewer buckets *Windows Logs* /
*Applications and Services Logs*) and a **Channel**. A channel override wins
over its provider default, which wins over the global default (1M rotation /
1y retention). Full schema in [CONFIG.md](CONFIG.md).

---

## Security & evidence integrity

- Archives and manifests are hashed; manifests can be signed.
- The pre-logmon loss baseline is captured once and is unrecoverable if missed —
  run `logmon_reset.py` before the first capture on a test box, and preserve
  `logmon_state.json` after the first run if you need that baseline as evidence.
- Alerts are written append-only (`alerts.jsonl`), ACL-hardened, and mirrored
  into the hashed archive manifest — the manifest is the authoritative record.
- Acknowledging an alert **appends** an acknowledgement; it never deletes the
  record of what happened.

Report vulnerabilities per [SECURITY.md](SECURITY.md).

---

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). Free for personal, educational,
charitable, research, and government use. Commercial use requires a separate
license — see [COMMERCIAL.md](COMMERCIAL.md).

Copyright YASDC / Kevin Perryman.

### Third-party dependencies

logmon ships as Python source and bundles no third-party libraries; the
dependencies below are installed separately by the operator and remain under
their own licenses. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for
details.

| Component     | License                                    |
|---------------|--------------------------------------------|
| PySide6 / Qt  | LGPL v3 (GPL v3 / commercial also offered) |
| pywin32       | PSF/BSD-style permissive                   |
| cryptography  | Apache 2.0 OR BSD 3-Clause (optional)      |

The service (`logmon.py`) does not import PySide6 — only the GUI and tray do.
