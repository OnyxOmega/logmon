# Third-Party Notices

logmon is licensed under PolyForm Noncommercial 1.0.0 (see `LICENSE.md`).
Copyright YASDC / Kevin Perryman.

logmon is distributed as **Python source only**. It does **not** bundle,
redistribute, or link statically against any third-party library. The
dependencies below are installed separately by the operator (via `pip`) and
remain under their own licenses. The logmon license applies to logmon's own
source files and does not purport to govern these dependencies.

---

## Runtime dependencies

### PySide6 (Qt for Python) — GUI and tray only

- **Used by:** `logmon_gui.py`, `logmon_tray.py`
- **Modules used:** `QtCore`, `QtGui`, `QtWidgets`, `QtNetwork`
- **Upstream:** The Qt Company — https://www.qt.io/qt-for-python
- **License:** LGPL v3 (also offered by The Qt Company under GPL v3 and under a
  commercial license)
- **LGPL v3 text:** https://www.gnu.org/licenses/lgpl-3.0.html

**Notes on Qt licensing as it applies here.** logmon does not ship Qt or PySide6
binaries; the operator installs PySide6 themselves and it is imported at runtime.
Because logmon does not convey the library, the LGPL's distribution obligations
are not triggered by logmon's source distribution. Operators who redistribute
Qt themselves (for example, by packaging logmon into a frozen executable with
PyInstaller or Nuitka) assume LGPL obligations for the Qt components in that
package, including providing the LGPL text, giving notice that the work uses Qt
under LGPL v3, and preserving the user's ability to replace the Qt libraries
with modified versions. Anyone doing so should review Qt's licensing directly:
https://www.qt.io/licensing/

The modules logmon uses are all available under LGPL v3. Some other Qt modules
(for example Qt Charts and Qt Data Visualization) are offered only under GPL v3
or a commercial license; logmon deliberately does not use them.

`logmon.py` (the service) does **not** import PySide6. A machine running only
the service, without the GUI or tray, has no Qt dependency at all.

### pywin32 — Windows service, Event Log, and SCM access

- **Used by:** `logmon.py`
- **Upstream:** Mark Hammond and contributors —
  https://github.com/mhammond/pywin32
- **License:** PSF-style / BSD-style permissive license (see the pywin32
  distribution for the full text)

### cryptography — optional manifest signing

- **Used by:** `logmon.py` (optional; only when `LOGMON_SIGNING_KEY` is set)
- **Upstream:** Python Cryptographic Authority —
  https://github.com/pyca/cryptography
- **License:** Apache License 2.0 OR BSD 3-Clause (dual-licensed)

If `cryptography` is not installed, logmon still produces manifests; they carry
hashes only and are unsigned. No functionality other than signature generation
is lost.

### Python standard library

- **License:** Python Software Foundation License —
  https://docs.python.org/3/license.html

`logmon_reset.py` has no third-party dependencies (standard library only).

---

## Summary of dependency licenses

| Component     | License                       | Bundled by logmon? |
|---------------|-------------------------------|--------------------|
| PySide6 / Qt  | LGPL v3 (GPL v3 / commercial also offered) | No |
| pywin32       | PSF/BSD-style permissive      | No |
| cryptography  | Apache 2.0 OR BSD 3-Clause    | No |
| Python stdlib | PSF License                   | No |

---

## Commercial use

logmon's own license (PolyForm Noncommercial 1.0.0) restricts logmon to
noncommercial use; see `COMMERCIAL.md` regarding commercial licensing of logmon
itself. Note that a commercial license for logmon covers logmon's code only.
Qt's own licensing is a separate matter between the user and The Qt Company:
LGPL v3 permits commercial use provided its conditions are met, and a Qt
commercial license is an alternative. Redistributors should satisfy themselves
as to their obligations.

Nothing in this file is legal advice.
