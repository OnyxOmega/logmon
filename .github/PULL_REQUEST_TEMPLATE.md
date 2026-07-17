## Summary

What does this PR change, and why?

## Component(s)

- [ ] logmon.py (service)
- [ ] logmon_gui.py (config GUI)
- [ ] logmon_tray.py (tray watcher)
- [ ] logmon_reset.py
- [ ] docs

## Design invariants

Confirm this change does not violate (or explain how it interacts with) each:

- [ ] logmon never alters OS Event Log settings (no `wevtutil sl`)
- [ ] Two files, one writer each (service never writes logmon.cfg post-install)
- [ ] An archive holds exactly one retention term
- [ ] The pre-logmon baseline is one-shot, captured before the first clear
- [ ] Every unexplained clear is flagged (no intent inference)
- [ ] Alerts are append-only; acknowledging never deletes
- [ ] All boundary/retention math is UTC

## How verified

Describe testing. Note this is a Windows-only tool; state whether it was
verified against real `wevtutil` or in a stubbed harness.

## Docs

- [ ] Updated CONFIG.md / DEPLOYMENT.md / CHANGELOG.md as needed
