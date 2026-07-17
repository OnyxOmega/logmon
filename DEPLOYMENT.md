# logmon Deployment Guide

Windows only. All service commands require an **elevated** (Administrator)
prompt.

## 1. Prerequisites

- Windows 10 / 11 / Server, 64-bit Python 3.12 recommended.
- Install dependencies:

  ```
  pip install pywin32 PySide6
  pip install cryptography          :: optional, signed manifests only
  python -m pywin32_postinstall -install   :: once, elevated
  ```

## 2. Install the service

```
python logmon.py --archive C:\EVENT_LOG_ARCHIVE --startup auto install
python logmon.py start
```

- `--archive <path>` sets and persists the archive root (default
  `C:\EVENT_LOG_ARCHIVE`).
- `--startup` accepts `auto`, `delayed`, or `manual`.
- The service runs as **LocalSystem**, which it needs in order to clear the
  Security log.

Verify:

```
python logmon.py status
```

## 3. Configure

Open the elevated GUI:

```
python logmon_gui.py
```

- **Channels tab:** the Event-Viewer-style tree of every channel on the box.
  Select channels (multi-select supported), or click **Add Recommended**. Set
  rotation/retention at the channel level, or set a provider default that its
  channels inherit. Rows flagged **red** have an OS `maxSize` below the
  recommended 4 GiB and may lose events logmon cannot prevent — fix those in
  Group Policy / Event Viewer (logmon never changes OS log settings).
- **Service status tab:** global Analytic/Debug toggles and evidence-integrity
  readout.
- **Save** writes `logmon.cfg` atomically; the service reloads within
  ~5 minutes or on the next cycle.

## 4. Start the tray watcher (per user)

Unprivileged, at each login:

```
pythonw logmon_tray.py
```

Enable **Start with Windows** from its tray menu. The watcher shows alerts and
lets a user *dismiss* their own notifications; *acknowledging* an alert is an
administrative action in the GUI.

## 5. First-run baseline (important)

On first contact with each channel, logmon records a one-shot count of records
the OS destroyed **before** logmon existed. This is unrecoverable if missed.

- On a **test box**, run `python logmon_reset.py` **before** the first capture
  to start clean.
- If you need the pre-logmon baseline as evidence, copy
  `C:\ProgramData\logmon\logmon_state.json` after the first run.

## 6. Coexistence with usnmon

logmon and usnmon run independently. During the usnmon phase-out both may
archive the `FileSystem` channel (usnmon's own output channel). If both clear
it, logmon will correctly raise `EXTERNAL_CLEAR` alerts when usnmon clears
between logmon cycles — a known-benign true positive during the transition. To
avoid split archives of that channel, have exactly one tool own its clearing.

## 7. Uninstall

```
python logmon.py stop
python logmon.py remove
```

Archives under the archive root are left in place. To also clear
configuration/state on a test box, run `python logmon_reset.py`.

## 8. Files on disk

| Path | Owner | Notes |
|---|---|---|
| `C:\ProgramData\logmon\logmon.cfg` | GUI | operator config |
| `C:\ProgramData\logmon\logmon.cfg.bak` | service | last validated config |
| `C:\ProgramData\logmon\logmon_state.json` | service | runtime state/status |
| `C:\ProgramData\logmon\logmon.log` | service | diagnostic log (local time) |
| `C:\ProgramData\logmon\alerts.jsonl` | service | append-only alert store |
| `C:\ProgramData\logmon\URGENT.TXT` | service | unacknowledged-alert flag |
| `<archive_root>\*.zip` | service | archives + manifests |
