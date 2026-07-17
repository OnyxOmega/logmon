# Contributing to logmon

Thanks for your interest. logmon is licensed under
[PolyForm Noncommercial 1.0.0](LICENSE.md); by contributing you agree your
contributions are provided under the same terms (see [CLA.md](CLA.md)).

## Before you start

- Open an issue to discuss substantial changes before writing code.
- logmon is Windows-only and targets Python 3.12.
- Keep the four deliverables separate in purpose: `logmon.py` (service),
  `logmon_gui.py` (elevated config), `logmon_tray.py` (unprivileged watcher),
  `logmon_reset.py` (test reset).

## Design invariants (do not break)

These are load-bearing. A change that violates one needs explicit discussion:

1. **logmon never alters OS Event Log settings** (no `wevtutil sl`).
2. **Two files, one writer each** — the service never writes `logmon.cfg`
   after install; the GUI never writes state.
3. **An archive holds exactly one retention term** so it is always cleanly
   prunable.
4. **The pre-logmon baseline is one-shot** and captured before the first clear.
5. **Every unexplained clear is flagged** — logmon does not infer intent.
6. **Alerts are append-only**; acknowledging never deletes a record.
7. All boundary/retention math is **UTC**.

## Style

- Follow the existing style (PEP 8, descriptive names, comments that explain
  *why*).
- Validate all externally-supplied values; never fail silently.
- Include a clear rationale in the PR description and update the relevant docs
  (`CONFIG.md`, `DEPLOYMENT.md`, `CHANGELOG.md`) with your change.

## Pull requests

- One logical change per PR.
- Describe what you changed, why, and how you verified it.
- Note any deviation from the design invariants above.
