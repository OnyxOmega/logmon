# logmon Configuration Reference

logmon uses **two files**, each with exactly **one writer**, under
`C:\ProgramData\logmon\`:

| File | Writer | Reader | Contents |
|---|---|---|---|
| `logmon.cfg` | Operator / GUI | Service (read-only) | policy + service settings |
| `logmon.cfg.bak` | Service | Service | last **validated** config |
| `logmon_state.json` | Service | GUI (read-only) | runtime state + status |

Single-writer ownership eliminates lost-update races between the GUI and the
running service. **Edit `logmon.cfg` only via the GUI**, or by hand with the
service stopped; the service never writes it after install.

---

## `logmon.cfg` — schema v2

```json
{
  "schema_version": 2,
  "archive_root": "C:\\ProgramData\\logmon\\EVENT_LOG_ARCHIVE",
  "service": {
    "poll_interval_sec": 300,
    "config_reload_interval_sec": 300,
    "retention_check_interval_hours": 24,
    "include_all_analytic": false,
    "include_all_debug": false
  },
  "defaults": {
    "rotate": true,
    "timeframe": "1M",
    "legal_retention": "1y",
    "size_limit_bytes": null
  },
  "providers": {
    "Windows Logs": {
      "enabled": true,
      "defaults": {},
      "channels": {
        "Security": { "enabled": true, "legal_retention": "7y" },
        "System":   { "enabled": true }
      }
    },
    "Applications and Services Logs": {
      "enabled": true,
      "defaults": { "timeframe": "1M", "legal_retention": "1y" },
      "channels": {
        "FileSystem": { "enabled": true, "timeframe": "10d", "legal_retention": "7y" }
      }
    }
  }
}
```

### `service`

| Key | Meaning | Range |
|---|---|---|
| `poll_interval_sec` | trigger-evaluation cadence | 30–86400 |
| `config_reload_interval_sec` | config re-read safety net (mtime is primary) | 30–86400 |
| `retention_check_interval_hours` | retention sweep cadence | 1–168 |
| `include_all_analytic` | archive ALL Analytic channels (service-wide) | bool |
| `include_all_debug` | archive ALL Debug channels (service-wide) | bool |

Analytic/Debug inclusion is **global, all-or-none** — there is no per-channel
Analytic/Debug option.

### `defaults` and policy resolution

A **policy** is any subset of these keys:

| Key | Meaning |
|---|---|
| `rotate` | rotation on/off |
| `timeframe` | rotation interval — `10m`, `6h`, `1d`, `2w`, `1t`, `1M`, `1y` (case-significant: `m`=minutes, `M`=calendar months) |
| `legal_retention` | retention term — `90d`, `26w`, `18t`, `18M`, `7y`; empty = keep forever |
| `size_limit_bytes` | explicit size ceiling, or `null` = track the channel's OS `maxSize` |

The **effective** value for a channel is resolved per key:

```
channel override  →  provider default  →  global default (1M / 1y)
```

Overrides are **sparse**: a channel or provider only overrides the keys it
lists; everything else inherits.

### `providers` → `channels`

- The **provider** key is a channel's `owningPublisher`, or the synthetic
  buckets `Windows Logs` (the five classic logs) and
  `Applications and Services Logs` (a non-classic channel with no publisher).
- `enabled` at provider level disables the whole group; at channel level it
  disables one channel. A channel is archived only when both are enabled.
- Channel names are exact (e.g. `Microsoft-Windows-PowerShell/Operational`).
  logmon **clears** what it archives, so membership is always explicit — never
  a wildcard.

---

## Time units

| Unit | Meaning | Rotation cap | Retention cap |
|---|---|---|---|
| `s` | seconds | 172800 | — |
| `m` | minutes | 2880 | — |
| `h` | hours | 336 | — |
| `d` | calendar days | 180 | 3650 |
| `w` | calendar weeks | 52 | 520 |
| `t` | 30-day terms | 12 | 120 |
| `M` | calendar months | 12 | 300 |
| `y` | calendar years | 1 | 25 |

Sub-day units are not valid for retention. All boundaries and retention math
are **UTC**.

---

## Archive naming

Flat in the archive root:

```
<start>_to_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date>.zip
historical_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date>.zip   (first-run dump)
```

- All timestamps UTC. `ROT`/`RET` are the group's rotation/retention terms, or
  `NONE`.
- `DELETE-AFTER_<date>` is `end + effective_retention`, day-floored;
  keep-forever → `DELETE-AFTER_NEVER`. The file is pruned only once the current
  UTC date is strictly past that date, so the printed date is a safe
  "delete on/after the next day" statement.
- The manifest inside carries the authoritative `retention` block
  (`term`, `delete_after_date`, `retention_expires_utc`).
- Inside the zip, each channel has its own folder:
  `Security/Security_<ts>.evtx` + `.manifest`.

---

## `logmon_state.json` — service-owned (read-only for the GUI)

Holds `engine` info, `config_errors` (validation problems surfaced for the
GUI), `bundle_state` (rotation anchors and per-channel failure tracking, keyed
by group id), `channel_state` (per-channel watermarks, baselines, external-clear
records), `discovered_unconfigured`, `disabled_channels`, and `alert_seq`. Do
not edit.

---

## Validation

Every value is validated on load. Invalid policy values are **dropped** (the
channel falls back to provider/global defaults) and the problem is published to
`state.config_errors` for the GUI to display — nothing is ever silently inert.
`logmon.cfg.bak` is refreshed only when a config validates cleanly, so the
fallback is always known-good.
