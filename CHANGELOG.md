# Changelog

All notable changes to logmon are documented here. This project adheres to
semantic-versioning intent (pre-1.0: interfaces may change between releases).

## [0.0.1] — Unreleased

Initial release. Windows Event Log archiver with tamper-evident manifests and
legally defensible retention.

### Added
- Windows service (`logmon.py`): capture via atomic
  `wevtutil cl /bu:`, per-channel hashing/manifests, zip archives, and
  legal-retention pruning.
- Provider/channel configuration model (schema v2) with three-tier policy
  resolution (channel override → provider default → global default of
  1M rotation / 1y retention).
- Archives grouped by `(rotation, retention)` — one archive per distinct due
  group; `<window>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date>.zip` naming, flat in
  the archive root.
- Evidence integrity: one-shot pre-logmon loss baseline per channel, exact
  OS-overwrite loss quantification, and a hashed manifest provenance block with
  a plain-language completeness statement.
- Tamper detection: watermark-based `EXTERNAL_CLEAR` CRITICAL alerts for any
  clear logmon did not perform.
- Durable alert store (`alerts.jsonl`, append-only, ACL-hardened) with
  `URGENT.TXT` flag.
- Elevated configuration GUI (`logmon_gui.py`) with an Event-Viewer-style
  channel tree, inline provider/channel policy editing, Enabled/Type/Status
  filters, prefix search, evidence-integrity status, and acknowledge-not-delete
  alert handling.
- Unprivileged per-user tray watcher (`logmon_tray.py`) with per-user dismiss
  markers (dismiss ≠ acknowledge).
- Test-system reset utility (`logmon_reset.py`).
- Two-file, single-writer config/state architecture; validation on load with
  errors surfaced to the GUI; last-known-good backup promoted only when a
  config validates clean.
- Global service-wide Analytic/Debug inclusion toggles.

### Known limitations
- Standalone service verification on live Windows is in progress.
- Growth-rate / time-to-full size triggering is deferred to v0.0.2 (the burst
  risk was explicitly accepted for v0.0.1).
