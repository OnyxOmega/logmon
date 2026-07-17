# logmon v0.0.1 Design Lock

**Status:** Design locked 2026-06-28; skeleton implemented; **revised 2026-07-06
and 2026-07-09** (see Revision History).
**Date locked:** 2026-06-28
**Date revised:** 2026-07-09
**Source:** Design discussion 2026-06-28 between operator (Kevin Perryman, YASDC)
and Claude; revisions from implementation review 2026-07-06 and the UTC
standardization decision 2026-07-09.
**License:** PolyForm Noncommercial 1.0.0
**Repository:** github.com/OnyxOmega/logmon (new repository)
**Author:** YASDC / Kevin Perryman
**Python:** 3.12 (to confirm operator's environment)
**Platform:** Windows-only

---

## Revision History

### 2026-07-17b — Per-channel size rotation (decouple chatty channels)

Real-hardware v2 archives revealed the trigger coupling predicted in review: a
single chatty channel (WMI-Activity, 1 MB OS cap, refills in ~5 min) tripping its
size cap rotated and cleared its ENTIRE (rotation, retention) group -- sweeping
Security into a mid-period archive at 0.3% full and resetting the group's monthly
anchor. Lossless, but it fragmented Security across many tiny archives and shifted
its calendar boundary.

**Change (operator-approved):** a size trip is now a PER-CHANNEL event. Only the
channel that filled is extracted, cleared, and zipped -- into its own archive
named `<Channel>_size_<start>_to_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date>.zip`.
Its group-mates are untouched and the group's TIME anchor is NOT moved, so the
period (e.g. 1M) boundary still lands where the calendar says. TIME and historical
rotations remain group-wide (all members are genuinely due together). The
per-channel capture path was refactored into one shared helper
(`_extract_channel_for_archive`) used by both the group and size archivers, so the
proven lossless path is not duplicated. `group_should_trigger_size` (bool) became
`group_size_tripped_channels` (returns the tripped list). The DELETE-AFTER token is
unchanged, so the retention pruner needs no change and recognizes the new names.

Rationale on grouping: high-volume "recommended" channels are NOT given artificial
cycles to isolate them. Grouping stays policy-driven (rotation, retention); the
per-channel size behavior already prevents a chatty channel from disturbing its
group-mates. Volume is tuned at the OS layer (raise the channel's maxSize via Event
Viewer/GPO), which logmon reads but never sets.

### 2026-07-17 — Real-hardware feedback: GUI improvements + skip-message fix

First live-hardware run (~24h, minus ~12h downtime) on DESKTOP-8JVDVEQ, uploaded
by the operator. **Validated on real `wevtutil`:** EXTERNAL_CLEAR tamper
detection fired correctly when usnmon cleared the FileSystem channel between
cycles (watermark 13429 -> 233, CRITICAL raised, URGENT.TXT written); 15
pre-logmon baselines captured; 409 archives produced; size triggers fired
(WMI-Activity); and the v2 config saved from the new GUI is well-formed with
correct provider grouping. (The running service in that log was still the v1
build, which correctly rejected the v2 config at the end — v2 service deploy is
the next step.)

Changes made from the feedback:

- **[logmon.py] Skip-reason logging fixed.** `should_skip_channel` now returns a
  human-readable REASON (or None) instead of a bare bool, and the archive loop
  logs it. Real-hardware logs showed OS-DISABLED channels (ForwardedEvents,
  LSA/Operational, TaskScheduler/Operational, etc.) being logged as skipped by
  "channel type filter" when the real reason was "disabled at OS (enabled:
  false)". Behavior was correct; the message was misleading. Now accurate.
- **[GUI] Auto-scan OS settings on load** (one-time, background thread — no modal
  dialog), and **auto-reload the Channels tab on re-entry** (cheap redraw; the
  Scan button still forces a fresh OS read).
- **[GUI] Global defaults at the two roots.** "Windows Logs" and "Applications
  and Services Logs" are now selectable provider nodes; selecting one edits that
  group's default rotation/retention, inherited by its channels unless a channel
  overrides. (These roots were already providers in the data model; this exposes
  them in the tree.)
- **[GUI] "Evidence Integrity" table renamed to "Archive Integrity"** on the
  Service Status page — logmon archives and rotates retention; the archives are
  usable as evidence but that is not their framing.

**Override visibility (operator compliance point).** Overrides deliberately
persist when a provider/global default changes — a later default change must not
silently stomp a channel set on purpose (e.g. Security at 7y). The flip side: on
a regulation-driven default change, the overridden channels are exactly the ones
that will NOT move, so the GUI now surfaces them. Selecting a group (root
provider) lists the channels under it that carry an override and won't follow the
change ("here's what won't move"); channel-level overrides also render bold in
the Rotation/Retention columns, while inherited values stay plain with "(inh)".
Behavior unchanged; visibility added.

Minor observed: one transient `wevtutil gl` TimeoutExpired on
RDPClient/Operational over two days (handled gracefully — returns {}). Many
curated channels are DISABLED at the OS on this box (operational info; the GUI
surfaces them red/disabled).

### 2026-07-15e — PASS 4 of 4: global toggles, v2 doc rewrite, cleanup

Completes the schema-v2 Channels redesign.

- **Global Analytic/Debug toggles** on the GUI Service Status tab
  (`include_all_analytic` / `include_all_debug`), one yes/no each, service-wide.
- **Design-doc bodies rewritten to v2:** §3.2.1 (providers/channels schema),
  §5 (archive grouping by (rotation, retention) + DELETE-AFTER naming), §6
  (delete-after pruning), §10.1 (selection superseded).
- **Dormant v1 code removed** from logmon.py (per-bundle archive engine,
  selector matching, span-regex pruner) — the live path is the v2 grouped
  engine.
- **Full end-to-end regression** re-run against the finished four-pass system:
  v2 resolution, two-group historical bootstrap, per-group time triggers, the
  868,287 pre-logmon baseline, manifest hashes + retention block, tamper
  detection (EXTERNAL_CLEAR + URGENT.TXT), delete-after pruning (expired removed,
  NEVER kept, in-term kept), and G1 (service never writes logmon.cfg). All green.

**Dup-naming item RESOLVED (2026-07-16):** the archive filename now carries the
group's `rot-<ROT>_ret-<RET>` tag, so two groups with the same retention but
different rotation written in the same cycle produce distinct names (no `_dup`
collision). Verified against the exact scenario that previously collided. This
also exposes the retention term on the filename for at-a-glance sorting. The tag
is policy, not contents, so it stays consistent with the naming rule; the pruner
still keys only off the unchanged `DELETE-AFTER_<date>` token.

### 2026-07-15d — PASS 3 of 4: GUI Channels tab (schema v2), Bundles tab removed

`logmon_gui.py` rewritten to the provider/channel model. Headless-tested against
the real 1,270-channel list; the GUI's config round-trips to a v2 config the
service validates with ZERO errors and resolves identically.

- **Bundles tab removed.** The Channels tab is the single editor.
- **Derived Event-Viewer tree** (never stored): "Windows Logs" (5 classics) and
  "Applications and Services Logs" (everything else, auto-foldered by splitting
  the flat channel name on '-' and '/'). Verified: `Microsoft-Windows-AAD/`
  `Operational` -> Microsoft > Windows > AAD > Operational.
- **Provider derived from the name** (no scan needed): part before the last '/'
  (owningPublisher), else the Event-Viewer root. `FileSystem`/`Dell` ->
  "Applications and Services Logs".
- **Inline policy editing** with inherited-vs-override indicators: channel-level
  override, and a provider-default section (applies to all channels under that
  provider) -- three-tier resolution (channel -> provider -> global 1M/1y) shown
  live.
- **Filters (#3):** Enabled Yes/No/Both; Type multi-select; Status multi-select;
  prefix ("starts with") search. Tree hides non-matching leaves and empty
  folders.
- **Curated recommendations** reworked to v2 (channel -> (rotation, retention));
  only channels present on the box are added.
- **Save validates** per-channel/-provider policy (mirrors the service) and
  writes an atomic v2 config.

Global Analytic/Debug toggles land on Service Status in Pass 4, along with the
doc-body rewrite and removal of the dormant v1 code.

### 2026-07-15c — PASS 2 of 4: (rotation, retention)-grouped archive engine

Reactivates archiving under schema v2. **logmon.py only.** Light helper checks
run; full archive-cycle regression deferred to the end of Pass 4 (resource
budget).

- **Grouped archiving.** `evaluate_active_channels()` groups active channels by
  `(rotation, retention)` and produces at most ONE archive per due group per
  cycle. Each group is treated as a "bundle" for the tested state machinery
  (rotation anchor, missing-channel, clear-failure) by using the group id string
  as the state key -- a channel has exactly one effective policy, so this keying
  is unambiguous, and it reuses the existing, verified state functions unchanged.
- **One retention term per archive preserved.** Every channel in a group shares
  a retention term, so each archive is cleanly prunable -- the one invariant that
  must never break. Grouping by (rotation, retention) generalizes the original
  per-primary-channel model; it does not reintroduce the mixed-retention
  EVTX_LOGS problem.
- **DELETE-AFTER naming (flat root).** `<start>_to_<end>_DELETE-AFTER_<date>.zip`
  (or `historical_<end>_DELETE-AFTER_<date>.zip`), all UTC, delete-after date
  day-floored, `DELETE-AFTER_NEVER` for keep-forever. The filename states the
  coverage window + safe-delete date, not the contents (channels are named by
  their own .evtx inside the zip, each in its own folder).
- **Manifest gains a `retention` block** (`term`, `delete_after_date`,
  `retention_expires_utc`) -- the authoritative copy; the filename mirrors the
  date for convenience.
- **Retention sweep rewritten** (`prune_by_delete_after`): flat root, reads the
  DELETE-AFTER date from each filename, deletes only when the current UTC date is
  strictly greater (honors "deletable from the following midnight"; never prunes
  before the precise expiry). Replaces the v1 per-subdir span-regex pruner.
- **Discovery v2** (`log_unconfigured_discoveries_v2`): surfaces channels
  matched by no provider/channel config.

Dormant v1 functions (`archive_one_bundle`, `evaluate_all_bundles`,
`bundle_should_trigger_size`, `resolve_bundle_channels`,
`prune_legal_retention_all`, discovery) are left in place, unreferenced by the
live path, to be removed in the Pass-4 cleanup.

### 2026-07-15b — PASS 1 of 4: schema v2 (providers/channels) + resolution

First of the planned 4-pass Channels redesign. **logmon.py core only; no archive
behavior changed yet** (the archive engine is Pass 2). Independently tested.

- **Config schema v2.** `bundles{}` is replaced by `providers{}` +
  global `defaults{}`. Rules attach to two OS-real objects only: PROVIDER
  (`owningPublisher`, or the synthetic buckets "Windows Logs" / "Applications
  and Services Logs") and CHANNEL. Structure:
  `providers.<provider>.{enabled, defaults{policy}, channels.<channel>.{enabled, policy}}`.
  Policy keys: `rotate, timeframe, legal_retention, size_limit_bytes`.
- **Effective-value resolver** (`effective_channel_policy`): per key,
  channel override -> provider default -> global default (1M / 1y). Sparse
  overrides. Verified across all three tiers.
- **`iter_active_channels` + `channel_group_key`**: enumerate active channels
  and their `(rotation, retention)` group. Pass 2 produces one archive per
  distinct group per due cycle (the "grouped dump" model).
- **Global Analytic/Debug** (`service.include_all_analytic` / `_debug`): one
  yes/no each, service-wide, never per-channel. `should_skip_channel` reads them.
- **Global defaults 1M / 1y**; **no v1->v2 migration** (test system resets);
  bootstrap writes a v2 skeleton.
- **G2 preserved**: bad policy values are dropped with errors and the resolver
  falls back, rather than silently breaking.

Pass-2 archive-engine functions (`archive_one_bundle`, `evaluate_all_bundles`,
etc.) are DORMANT under v2 (no `bundles` key -> they loop nothing, non-crashing)
until Pass 2 rewrites them to the grouped model. Full §3.2/§5/§6 doc-body
rewrite lands with Pass 4.

### 2026-07-15 — GUI bug fix: channels landed in the wrong bundle

**Reported from real use:** selecting a channel on the Channels tab and clicking
"Add" placed it into the `Application` bundle even though the operator never
selected `Application`.

**Root cause (confirmed, not a selector/regex issue):** the add target was
`BundleEditor._current`, which `reload()` auto-set to row 0 via
`setCurrentRow(0)`. Bundles are sorted alphabetically, so "Application" was
always the silent default. The confirmation dialog said "add to the selected
bundle" without NAMING it, so nothing revealed the mistarget. Explicit-list
matching was never involved — verified that no selector could match
`FileSystem`/`Dell*` (none contain "application").

**Fix:** the Channels tab now has an explicit **"Add to bundle:"** picker,
defaulting to "-- choose a bundle --". Adding with no target chosen is refused;
the confirmation dialog names the target bundle and previews the channels; and
the add writes to that NAMED bundle (`add_channels_to`) rather than depending on
the editor's current selection. Verified: with no target, channels do not leak
into any bundle; with a target chosen, they land only in that bundle.

### 2026-07-14 — GUI usability (operator feedback)

- **Bundle tab: size-limit control removed.** Per §10.14, the size ceiling is
  ALWAYS each channel's OS `maxSize` (an operator value above it is unreachable
  and gets clamped anyway), so exposing a picker only invited misconfiguration.
  The GUI now always writes `size_limit_bytes: null` (= track OS maxSize). Any
  explicit value in a hand-edited config is preserved, not overwritten.
- **Channels tab: sortable columns.** Click a header to sort, click again to
  toggle ascending/descending. Sorting is disabled during table repopulation so
  rows don't land in the wrong order mid-fill.
- **Service Status tab: "not installed" detection.** Previously rendered
  "? ? ?" when the service had never run. Now distinguishes three states: (a)
  no `logmon_state.json` -> "service not installed or has never run" + the
  install/start commands; (b) state file present but no `engine` block ->
  "installed but not started yet"; (c) running -> the real start type / install
  time / evidence-integrity table.

### 2026-07-13g — DESIGN COMPLETE for v0.0.1. Ready for P4 (Windows testing).

Closing pass: finish the designed scope so P4 tests a WHOLE system, rather than
testing a partial one and then having the last additions break what was already
signed off.

- **Item 3 CLOSED — disabled channels (§10.13).** The service now reads
  `enabled` from `wevtutil gl`, SKIPS disabled channels (they produce no events,
  and attempting `cl /bu:` on them burned a subprocess per cycle and could feed
  spurious REPEATED CLEAR FAILURE backoff), surfaces them in
  `state.disabled_channels` (separate from missing/unconfigured — a distinct
  condition with a distinct remedy), and **auto-recovers** when the operator
  enables them. logmon cannot enable them itself: that needs `wevtutil sl`
  (HARD RULE 3).
- **§10.11 RESOLVED — `engine_start_anchor` REMOVED.** Never read or written,
  and unnecessary: the per-bundle historical bootstrap already establishes each
  bundle's anchor. Also removed **`rotate_interval_default`** and the
  **`--log-interval`** CLI pre-flag: they set a GLOBAL rotation default, but
  `timeframe` is REQUIRED per rotating bundle, so the fallback could never fire.
  They were an unvalidated raw config key that silently did nothing.
- **§10.3, §10.4, §10.5, §10.9 RESOLVED** — failure handling, schema versioning,
  state file format, and bundle-resolution refresh cadence are all now specified
  and match the code.

**Item 6 (growth-rate triggering) DEFERRED to v0.0.2 — deliberately, not by
oversight.** The ORIGINAL design (§4.2, operator direction Q3c) explicitly
ACCEPTED the burst risk: *"if log is growing that fast, there is something else
going on."* Implementing it now would be scope creep beyond the v0.0.1 design
lock. The residual risk is documented, and logmon detects and discloses any loss
it cannot prevent (§10.16 / §10.17).

**Open items register: ZERO items outstanding for v0.0.1.**

Deliverables: `logmon.py` (service), `logmon_gui.py` (elevated config GUI),
`logmon_tray.py` (unprivileged alert watcher), `logmon_reset.py` (test-system
reset).

**Everything to date has been tested against STUBS on Linux.** No code has yet
run against a real `wevtutil` on Windows. Note that BOTH of the last two defects
(the Analytic type-string bug and the 4 GiB/`null` size-limit bugs) were
"the code disagreed with reality" failures that only surfaced when real commands
were run. P4 is now the highest-value next step.

### 2026-07-13f — D2 CLOSED: per-channel maxSize clamp (§4.2, §10.14)

Two real defects fixed:

1. **The global 4 GiB ceiling was fiction.** The operative `maxSize` is
   PER CHANNEL and read from the OS (Security = 20 MiB, not 4 GiB). An explicit
   `size_limit_bytes` above a channel's `maxSize` produced an **unreachable
   threshold** — the size trigger could never fire. Now clamped to the channel's
   real ceiling, with a warning naming the fix.
2. **`size_limit_bytes: null` DISABLED the size trigger entirely** — and `null`
   is the GUI's default. On a 20 MiB circular Security log with a 30d timeframe,
   the log fills in ~13 days and would have shed events silently for the
   remaining ~17 days of every cycle. `null` now means "track this channel's OS
   maxSize".

`effective_size_limit()` resolves the ceiling per channel; the manifest now
records `caveats.size_trigger` (os_max_size / configured / effective /
clamped_to_os_max) so an auditor can see exactly which ceiling governed a
capture. Verified against the real measured values (20 MiB / 15 MiB / 1 MiB).

### 2026-07-13e — GUI (`logmon_gui.py`) built; P5 substantially complete

Elevated, admin-only config editor. Companion to the unprivileged
`logmon_tray.py`.

**GUI CONTRACT enforced and verified end-to-end:** the GUI writes `logmon.cfg`
ATOMICALLY and nothing else; it never writes `logmon_state.json` or
`logmon.cfg.bak`. Round-trip test: **GUI saves -> service `validate_config()`
accepts with ZERO errors -> 13 enabled bundles.**

- **Curated bundles (§10.1 CLOSED).** 13 shipped bundles built from explicit
  channel lists, derived from the real 1,270-channel audit: Security, System,
  Application, Setup, PowerShell, Authentication, Defense, Execution,
  RemoteAccess, FileSharing, Policy, Devices, Forwarded. **Only channels that
  actually exist on the machine are added.** FileSharing deliberately carries
  BOTH Microsoft spellings (`SMBClient/Operational` AND `SmbClient/Security`) —
  proof that explicit lists absorb vendor naming inconsistency where patterns
  cannot.
- **Validation mirrors the service (G2).** Bad `timeframe`/`legal_retention`
  BLOCK the save with an explanation, rather than silently producing a bundle
  that never rotates or never prunes. Verified: bad config is refused, file not
  written.
- **Channels tab.** Shows every channel with its OS-governed reality (type,
  enabled, `maxSize`, retention policy). **RED flag on any assigned channel
  below the recommended 4 GiB**, escalating when `retention=false` (circular
  overwrite -> "EVENTS WILL BE LOST"). Disabled channels greyed with "DISABLED
  at OS". Unconfigured channels amber. `wevtutil gl` scanning runs on a worker
  thread with progress (1,270 subprocesses must never block the UI).
- **Alerts tab — ACKNOWLEDGE, never delete.** Acknowledging APPENDS an
  acknowledgment record (who/when/through-which-seq) and clears `URGENT.TXT`.
  Verified: alert count GREW 1 -> 2, the original EXTERNAL_CLEAR record survives,
  the file was appended not truncated.
- **Service status tab.** Surfaces `config_errors` from the service (so
  GUI/service validator drift is caught, not silently trusted), plus the
  evidence-integrity table: destroyed-before-logmon baseline, destroyed since
  baseline, last logmon clear, and external-clear count per channel.

**ACL CORRECTION (bug fixed).** `_harden_alert_acl` granted Administrators
READ-only, which would have made acknowledgment IMPOSSIBLE. Now
SYSTEM:(F) / Administrators:(M) / Users:(R). Documented honestly: clearing the
Security log requires SeSecurityPrivilege, so an attacker triggering an
EXTERNAL_CLEAR is ALREADY admin and can tamper with `alerts.jsonl` regardless.
No ACL can prevent that — which is exactly why the durable evidence is the copy
inside the hashed/signed archive MANIFEST, shipped off-box.

**Validation is duplicated between GUI and service by design** (design lock
11/13.1: nothing external imports logmon internals). The service stays
authoritative and publishes `config_errors`, so drift is surfaced.

### 2026-07-13d — Tray helper (`logmon_tray.py`) — item 10 closed

**Separate app, not "the GUI minimized to tray."** Decided on privilege grounds:
the config GUI must write `logmon.cfg` in ProgramData and therefore needs
elevation; the watcher must run at every login, unelevated. Bundling them would
leave an **elevated process running permanently in the user's session with the
power to silently rewrite the archival config** — unacceptable in a forensic
tool. Also: the GUI is an on-demand editor, the watcher is a 24/7 daemon; alerts
must fire whether or not anyone has the editor open.

**Privilege split (enforced, verified):**

| | Tray (unprivileged, per-user) | GUI (elevated, admin) |
|---|---|---|
| read `alerts.jsonl` | yes | yes |
| write `alerts.jsonl` | **NEVER** | appends ACK records only |
| `URGENT.TXT` | reads | clears |
| dismiss marker | writes `%APPDATA%` | — |

**DISMISS vs ACKNOWLEDGE — two different ideas:**
- **Dismiss** (tray, any user): suppresses *that user's* popups via a
  high-water mark on the alert `seq`, in `%APPDATA%`. The alert **remains** in
  the store, **`URGENT.TXT` stays set**, the **icon stays red**, and other users'
  trays still pop. A personal convenience only.
- **Acknowledge** (elevated GUI, admin): appends an acknowledgment record
  (who/when/what) and clears `URGENT.TXT`. **It must never DELETE alert records**
  — destroying the record of a tamper is the exact erasure the ACLs exist to
  prevent. Bound the file by rolling it into the archive tree, never by
  truncating in place.

A detected external clear of the Security log **should** require an
administrator to actively acknowledge it, not a user clicking an X.

**Also added:** monotonic `seq` on every alert (state-backed), so the tray's
high-water mark is unambiguous and survives the alert file being rolled.

**Verified:** tray reads alerts and never mutates them (sha256 unchanged);
dismissal is per-user (a second user still gets popped); `URGENT.TXT` survives
dismissal; tray writes only under `%APPDATA%`; icons render; single-instance
guard; autostart via HKCU (unprivileged).

### 2026-07-13c — Item 9 implemented: evidence integrity (9 + 7 + 8 + tamper alerts)

Implemented ahead of the GUI because **the baseline is one-shot**: it can only be
captured at logmon's first contact with a channel, before the first clear.

- **`channel_state` watermarks (§10.18)** — top-level, keyed by CHANNEL (not
  bundle), written on change. `observe_channel()` runs **before every capture**
  AND **on every poll** (an external clear between captures would otherwise be
  invisible).
- **Pre-logmon baseline** — captured once, permanently. On the audited host:
  **868,287 Security records already destroyed before logmon existed.** Drawn as
  a hard line between loss that predates logmon and loss on its watch.
- **Overwrite-loss quantification (§10.16)** — `records destroyed =
  oldestRecordNumber - 1`. Exact, not estimated.
- **External-clear / tamper detection (§10.18)** — a DECREASE in the newest
  EventRecordID with no logmon clear proves someone else wiped the log. Raises a
  **CRITICAL** alert. (Event 1102 cannot distinguish an attacker's clear from
  logmon's; the watermark can.)
- **Manifest caveats/provenance (§10.17)** — every archive now carries the
  channel's OS config (`maxSize`, `retention`, `autoBackup`, `enabled`, `type`),
  its record counters at capture, the exact loss count, and a plain-language
  **completeness statement** (COMPLETE / INCOMPLETE / first-capture). Inside the
  hashed, optionally signed manifest — so it is tamper-evident and travels with
  the archive.
- **Alert store (§10.19, service side)** — append-only `alerts.jsonl` +
  `URGENT.TXT` flag, ACL-hardened (SYSTEM-write / Users-read, best effort) so an
  attacker cannot trivially erase evidence of their own clear. The tray helper
  (user-mode visible surface) is **still to be built**.

**Position statement:** logmon cannot guarantee completeness on a circular
channel it does not control. It guarantees that **every gap is detected,
counted, and disclosed** — a materially stronger legal position than an
unbackable completeness claim.

### 2026-07-13b — TIER 0 GUI CONTRACT implemented (G1/G2/G3 + D1)

Code changes applied and verified. The service is now safe to accept GUI input.

- **G1 — Two files, single writer each (§3.2, §7.2).** `logmon.cfg` is
  operator/GUI-owned and the service NEVER writes it after install; runtime state
  moved to service-owned `logmon_state.json`. This eliminates the lost-update race
  in which the service's 8 read-modify-write sites would silently clobber operator
  edits. **§7.2 restored to the original 2026-06-28 design** (the 2026-07-09 edit
  that embedded state in the config was a mistake — see that section).
- **G2 — Validation on load (§3.2.4).** Every GUI-writable value is validated.
  Invalid bundles are FORCED disabled with an `_error` string rather than running
  silently inert; errors are published to `state.config_errors` for GUI display.
  **`logmon.cfg.bak` is refreshed only when a config validates CLEAN** — a
  known-good backup must be known good.
- **G3 — Schema fully defined and locked (§3.2).** Nested `service{}` block;
  `channels` explicit list is the PRIMARY membership mechanism (`channel_selector`
  demoted to an opt-in escape hatch); `size_limit_bytes: null` = use the channel's
  OS `maxSize`.
- **D1 — Analytic skip bug FIXED (§10.2).** `"analytical"` -> `"analytic"`, and
  the config key `include_analytical` -> `include_analytic` to match Microsoft's
  vocabulary. Verified: Analytic and Debug now both skip; Admin/Operational do not.
- **Diagnostic log rotation (§8.1):** corrected to size-only (5 MB x 3), which is
  what the code does and is sufficient for our own log.

Still open: D2 (per-channel `maxSize` clamp, §10.14) and open items 3, 6-10.

### 2026-07-13 — `wevtutil` field audit; open items registered (NO CODE WRITTEN)

A live-host audit of `wevtutil el` / `gl` / `gli` / `cl /bu:` produced hard
evidence that revised several assumptions. **No code was changed this pass** —
all findings were registered as open items (§10.0) and have since been resolved
(see later 2026-07-13 entries).

**Locked this pass:**
- **§11 — `wevtutil sl` NEVER invoked. Re-affirmed and closed** after explicitly
  reconsidering raising `maxSize` / setting `autoBackup`. logmon adapts to the
  environment; it does not reconfigure it.
- **§12.1 — HARD RULES adopted:** (1) never parse `wevtutil` text output for
  timestamps — it mislabels LOCAL time as `Z`; XML only. (2) loss is measured
  only from the `oldestRecordNumber` delta (purging is byte-driven). (3) no `sl`.

**Confirmed empirically (Appendix A):**
- **`cl /bu:` is PROVEN lossless** — backup matched the live log byte-for-byte
  (20,975,616) *and* record-for-record (21,943), verified three independent ways.
  Retroactively validates removing the `epl` export.
- **Capture does not mutate channel config** — `gl` identical before/after.
- **The Analytic skip is BROKEN** (§10.2): code compares to `"analytical"`;
  `wevtutil` reports `Analytic`. The skip never fires.
- **The 4 GB size ceiling is fiction** (§10.14): real `maxSize` is per-channel and
  is **20 MiB** on Security/System/Application.
- **97.5% of this host's Security events were already destroyed** by OS circular
  overwrite before logmon existed (868,202 of 890,208 records).
- **Record numbering resets to 1 on clear** — basis for exact loss accounting
  (§10.16) and external-clear/tamper detection (§10.18).

**Registered as open items (3, 4, 6, 7, 8, 9, 10 in §10.0):** disabled-channel
handling, per-channel `maxSize` clamp (**must-fix**), growth-rate triggering,
overwrite-loss detection, manifest caveats/provenance, per-channel watermarks,
and a durable tamper-alert store with a user-mode visible surface.

### 2026-07-09 — post-audit triage (operator-approved)

Dispositions from the doc-vs-code discrepancy audit. Code changes applied and
verified this pass:
- **Config resilience (§7.2, §7.3).** `_write_config_atomic` now keeps a
  last-good `logmon.cfg.bak`; `read_config` falls back to it when the primary
  config exists but is corrupt (absent = legitimate first-run empty). Prevents a
  corrupt config from silently halting all archiving.
- **Diagnostic log location (§8.1).** Now written to
  `C:\ProgramData\logmon\logmon.log` (config dir), not the archive dir.
- **Archive root default (§5.1).** Default is now
  `C:\ProgramData\logmon\EVENT_LOG_ARCHIVE`.
- **Unconfigured-channel discovery (§3.4).** Channels matching no bundle are
  logged once and persisted under `discovered_unconfigured` for the GUI.
- **Diagnostic log `%z` millisecond note (§8.1, §11).**

Documentation-only dispositions:
- **Service name (§7.1).** Correct name is `LogMonitorService` (doc fixed to
  match code; code unchanged).
- **State file (§7.2).** Doc rewritten to mirror the code — state embedded in
  `logmon.cfg`. *(NOTE: this direction was reversed on 2026-07-13b — see that
  entry. The original separate-state-file design was correct; embedding it in
  the config would have let the service clobber GUI edits.)*
- **Nested config schema (§3.2).** Nested schema is authoritative; a code change
  to consume it is flagged pending (not yet applied).

Open items raised or clarified (need operator decision — no code yet):
- **§10.1** bundle-selector model + the §3.4 auto-default rule (need more info).
- **§10.11** `engine_start_anchor` — keep as info-only or remove (recommend
  remove; per-bundle historical bootstrap already covers ramp-up).
- **§10.12** test-system reset utility (`logmon_reset.py` provided for review).
- **§14** P3–P5 (config surface, standalone service verification, GUI) — need
  more information to scope.

### 2026-07-09 — UTC standardization (operator-approved)

**logmon operates entirely in UTC, with no timezone configuration.** All
timeframe boundaries, rotation anchors, archive-span timestamps, and the
legal-retention horizon are computed in UTC. The `timezone` config key is
removed from the schema (it was inert and never read).

Reasoning: every artifact in logmon's pipeline is already UTC — Windows Event
Log `TimeCreated` and USN journal `TimeStamp` are both UTC FILETIME values, and
logmon's manifests already record UTC — so there is no reason to differ from
that standard, and doing so only offsets archive names from the evidence inside
them and reintroduces DST edge cases. Fixing the standard (rather than exposing
a setting) prevents any deployment from drifting off it. The prior constraint
that logmon match usnmon (§10.10) is dissolved because rotation and retention
are being removed from usnmon, leaving logmon the sole owner. Affected sections:
§3.2 (key removed), §4.1 (UTC locked + full reasoning), §6.1 (retention in UTC),
§10.10 (resolved). The one deliberate exception is the **diagnostic log**, which
stays in machine local time for its operational audience, with a `%z` offset on
each line to keep it unambiguous (§8.1).

This revision is a **documentation decision only**. The v0.0.1 code still uses
naive local `datetime.now()` (copied from usnmon); the UTC conversion is the
next code change and is not yet applied.

### 2026-07-06 — implementation review (operator-approved)

Five changes were approved after review of the v0.0.1 skeleton. Each is
reflected in the section noted:

1. **Single-call atomic capture (§2.2, §2.3).** The `wevtutil epl` export step
   is removed. Capture is now `wevtutil cl <channel> /bu:<archive>` ONLY, and
   the backup that the clear produces IS the archived `.evtx`. The old
   export-then-clear-then-discard-backup sequence opened a data-loss window —
   events written between the separate `epl` and `cl` calls were cleared but
   absent from the export, then lost with the discarded backup.

2. **One archive per primary channel; no `EVTX_LOGS` (§5.1, §5.2, §6.1).** Each
   tripped bundle produces its own `<PrimaryChannel>_<span>.zip` under its own
   subdirectory. The combined multi-bundle `EVTX_LOGS_<span>.zip` is removed.
   This makes per-primary-channel legal retention complete — the old
   `EVTX_LOGS` archives had no retention owner and would have accumulated
   indefinitely.

3. **First-run "historical" bootstrap (§2.1, §5.2, §6.1).** On a bundle's first
   run (no rotation anchor yet) logmon archives all pre-existing channel
   contents as a one-time `<PrimaryChannel>_historical_<timestamp>.zip`, clears
   the channels, and records the rotation anchor. This seeds the anchor that
   timeframe checks compute from — without it a timeframe-only bundle could
   never establish a first anchor and would never rotate.

4. **Boundary-snapped archive timestamp (§4.1).** A timeframe rotation names and
   anchors the archive on the boundary that was crossed, not the wall-clock
   poll time, so calendar-month naming (`<Bundle>_<MonthName>_<YYYY>`) fires
   and boundary math stays aligned.

5. **Repeated-clear-failure handling (§7.3, §10.7, §13.5).** A channel that
   exists but repeatedly fails to clear is backed off with a growing delay and,
   after a threshold of consecutive failures, disabled via a state
   (`clear_failed_channels`) DISTINCT from the missing-channel state — logged as
   **REPEATED CLEAR FAILURE**, not LOG MISSING — so the operator can resolve the
   two different root causes correctly.

Sections written pre-revision retain their original numbering; "revised
2026-07-06" markers flag the changed text. The §17 Lock Statement's "no code
written" wording reflects the 2026-06-28 lock only; post-lock changes are
governed by this Revision History.

---

## 1. Architectural Foundation

### 1.1 Purpose

logmon is a Windows Event Log archiving service. It captures event log data
from Windows Event Log channels on a rotation schedule (time-based or
size-based), archives the captured data with hashing and compression, and
manages legal-retention pruning of the archived copies.

logmon is a **companion** to the OS's Event Log system, not a replacement.

### 1.2 What logmon does

- Reads Windows Event Log channels via `wevtutil` on operator-configured
  triggers (timeframe elapsed OR per-log size threshold hit)
- Exports and clears the OS event log (extraction + reset, so the next
  archive period starts fresh)
- Applies naming, compression, hashing to the archived `.evtx` file (matching
  usnmon's existing archive workflow)
- Prunes archives by legal retention rules (matching usnmon's existing
  retention workflow)

### 1.3 What logmon does NOT do

- **Does NOT configure OS Event Log settings.** No `wevtutil sl` calls.
  Channel MaxSize and Retention policies remain whatever the operator has
  configured at the OS level.
- **Does NOT interfere with WEF/WEC subscriptions.** Windows Event Forwarding
  and Windows Event Collector continue to function normally.
- **Does NOT create new events.** logmon archives what the OS captured; it is
  not itself an event producer for the channels it archives.
- **Does NOT replace usnmon** — usnmon continues its role as the USN journal
  event recorder. logmon operates independently on Windows Event Log channels.

### 1.4 Long-term architectural intent

Once logmon is stable, the rotation, retention, naming, compression, and
hashing logic will be **removed from usnmon.py**. usnmon will simplify to a
pure event recorder (USN journal → event log entries). logmon will take over
all archive management going forward. **This extraction pass is a separate
future project, NOT in v0.0.1 scope.**

Until the extraction happens, usnmon retains its current archive management
duties for its own USN-journal-derived .evtx files. logmon operates on all
OTHER Windows Event Log channels.

**Archive directory naming:** logmon uses `EVENT_LOG_ARCHIVE` (vs usnmon's
current `FILESYSTEM_ARCHIVE`). When the extraction pass happens, usnmon's
archive directory reference will be removed entirely from usnmon.

---

## 2. Operational Model

### 2.1 Runtime flow

For each configured bundle (primary channel):

0. **First run (no rotation anchor yet)** — revised 2026-07-06:
   - Archive all pre-existing channel contents as a one-time "historical"
     dump (`<PrimaryChannel>_historical_<timestamp>.zip`), clearing the
     channels so the next period starts fresh.
   - Record the rotation anchor at the capture time. This seeds the "last
     archived at" that all later timeframe checks compute boundaries from.
1. **Check trigger conditions** at each polling cycle (once an anchor exists):
   - Has configured timeframe elapsed since last archive?
   - Has current channel `.evtx` file size crossed the configured threshold?
2. **If either trigger fires:**
   - Atomically back up + clear the channel in ONE operation
     (`wevtutil cl <channel> /bu:<archive>`); the backup is the archived copy
     (see 2.2 — no separate `epl` export).
   - Apply naming, compression, hashing to the archive.
   - Update state with the new "last archived at" timestamp (the boundary that
     fired, for timeframe triggers — see 4.1).
3. **Otherwise:** wait for next polling cycle.

Separately, on daily schedule:

4. **Check legal retention:** identify archives older than the configured
   retention period. Delete them (matching usnmon's existing retention logic).
5. **Log retention deletions** to logmon's diagnostic log.

### 2.2 Extraction + clear mechanism (revised 2026-07-06)

**Method:** `wevtutil cl <channel> /bu:<archive_path>` (clear-with-backup)
**only**. There is no separate `wevtutil epl` export step.

**Rationale:** the original design exported with `epl`, then cleared with
`cl /bu:`, then discarded the `/bu:` backup as redundant. Because `epl` and
`cl` are two separate process invocations, any events written between them were
cleared from the OS log but were NOT present in the `epl` export — and were
then lost when the backup was discarded. For a legal-evidence archiver that is
unacceptable data loss.

`wevtutil cl /bu:` already performs an atomic backup-and-clear at the OS level:
the backup contains EXACTLY the set of events that were cleared, with no gap.
logmon therefore uses that backup file directly as the archived `.evtx`. If the
backup write fails, `wevtutil` does not clear the channel, so a failure leaves
the OS log fully intact (fail-safe: no clear without a good backup).

**Post-clear handling:** the `/bu:` backup IS the archived `.evtx`. logmon
hashes it, writes its per-`.evtx` manifest, and adds it to the per-bundle zip.
No separate export file exists to reconcile or clean up. (A stale backup left
by a crashed prior run is removed before the clear, since `cl /bu:` refuses to
overwrite an existing file.)

### 2.3 wevtutil usage boundary

logmon uses `wevtutil` for (revised 2026-07-06 — `epl` removed):
- `wevtutil el` — enumerate available channels (once per config refresh cycle)
- `wevtutil cl <channel> /bu:<archive>` — clear channel with backup; the backup
  IS the archived copy (no separate `epl` export — see 2.2)
- `wevtutil gl <channel>` — get channel metadata (type, enabled state, etc.)
- `wevtutil gli <channel>` — get channel file size (size-trigger polling)
- `wevtutil qe <channel> /c:1` — probe for emptiness (empty channels are
  skipped, see 10.8)

logmon does NOT use `wevtutil sl` (set log configuration). OS-level Event Log
configuration remains the operator's responsibility, managed via normal OS
tools (Event Viewer, Group Policy, custom scripts, etc.).

---

## 3. Configuration

### 3.1 Config file

- **Location:** `C:\ProgramData\logmon\logmon.cfg`
- **Format:** JSON (matching current usnmon config format)
- **Content:** Service settings + per-bundle rotation and retention settings

### 3.2 Config + State schema — FULLY DEFINED (locked 2026-07-13)

**Two files. Each has exactly ONE writer.** The GUI and the service are separate
processes; if both read-modify-write one file, the service's stale copy silently
clobbers whatever the operator just saved. Single-writer ownership eliminates the
race by construction.

| File | Owner (writer) | Reader |
|---|---|---|
| `logmon.cfg` | **Operator / GUI** | Service (read-only after install) |
| `logmon.cfg.bak` | **Service** | Service (last-KNOWN-GOOD fallback) |
| `logmon_state.json` | **Service** | GUI (status display, read-only) |

#### 3.2.1 `logmon.cfg` — operator/GUI owned (schema v2)

```json
{
  "_README": "Edited by the logmon GUI. The service READS this and never writes it.",
  "schema_version": 2,
  "archive_root": "C:\\ProgramData\\logmon\\EVENT_LOG_ARCHIVE",
  "service": {
    "poll_interval_sec": 300,
    "config_reload_interval_sec": 300,
    "retention_check_interval_hours": 24,
    "include_all_analytic": false,
    "include_all_debug": false
  },
  "defaults": { "rotate": true, "timeframe": "1M", "legal_retention": "1y",
                "size_limit_bytes": null },
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
        "FileSystem": { "enabled": true, "timeframe": "10d",
                        "legal_retention": "7y" },
        "DellAudio":  { "enabled": true }
      }
    }
  }
}
```

**Two OS-real policy levels — PROVIDER and CHANNEL.** Rules attach only to a
provider (a channel's `owningPublisher`, or the synthetic buckets `Windows Logs`
for the classic five and `Applications and Services Logs` for a non-classic
channel with no publisher) and to a channel. The Event-Viewer display tree
(Microsoft → Windows → AAD → …) is DERIVED from channel names for the GUI and is
never stored.

**Effective value resolves per key:** channel override → provider `defaults` →
global `defaults` (1M / 1y). Overrides are sparse — only the keys present
override; everything else inherits. Policy keys: `rotate`, `timeframe`,
`legal_retention`, `size_limit_bytes` (`null` = track the channel's OS maxSize).

**Global Analytic/Debug** live in `service{}` as `include_all_analytic` /
`include_all_debug` — one yes/no each, service-wide, never per-channel.

**No `bundles`.** Schema v1 is not migrated (operator direction: the test
system resets); a v1 config is reported as a `schema_version` mismatch.

#### 3.2.2 `logmon_state.json` — service owned, GUI reads for status

```json
{
  "_README": "Written by the logmon SERVICE. Do not edit.",
  "schema_version": 1,
  "last_updated_utc": "2026-07-13 16:20:49.123Z",
  "engine": { "install_time_utc": "...", "service_start_type": "auto" },
  "config_errors": [],
  "bundle_state": {
    "Security": {
      "rotation_anchor": "2026-07-13 16:20:49",
      "missing_channels": [],
      "clear_failures": {},
      "clear_failed_channels": []
    }
  },
  "channel_state": {},
  "discovered_unconfigured": [],
  "disabled_channels": []
}
```

`channel_state`, `disabled_channels` are reserved for §10.18 / §10.13.

#### 3.2.3 GUI CONTRACT (binding on both sides)

1. The GUI writes `logmon.cfg` **atomically** (temp file + `os.replace`).
   A non-atomic write can be read mid-flight by the service.
2. The GUI **never** writes `logmon_state.json` or `logmon.cfg.bak`.
3. The service **never** writes `logmon.cfg` after the install bootstrap.
4. The service **validates everything** on load (§3.2.4) and publishes all errors
   to `state.config_errors` for the GUI to display.
5. All timestamps in both files are **UTC** (§4.1).

#### 3.2.4 Validation — nothing is accepted unvalidated (G2)

Every value the GUI can write is validated on load. **A bad value never results
in silent inertness.** Previously, `timeframe: "30 days"` parsed to `None` and the
bundle simply *never rotated* — no error, no log line; and `legal_retention:
"7 years"` meant archives were *never pruned*. For a legal-retention tool those
are the two worst possible failure modes, and both were one typo away.

Now:
- Invalid bundle → **forced `enabled: false`** + an `_error` string. Never
  half-run.
- Invalid service setting → falls back to the documented default, error published.
- **`logmon.cfg.bak` is only refreshed when a config validates CLEAN.** An
  unvalidated backup is worthless as a fallback — a known-good backup must be
  *known* good.
- Corrupt primary → fall back to the (validated) `.bak`, and say so loudly.

### 3.3 Config reload

- **On-demand reload:** service watches config file mtime. When it changes,
  reload before the next channel evaluation cycle.
- **Poll-interval reload:** service also re-checks config every N minutes
  (default 5) as a safety net.
- Combined approach: changes take effect within 5 minutes automatically, or
  immediately on the next cycle after mtime changes.

### 3.4 Channel discovery (implemented + partly deferred — 2026-07-09)

- Each poll cycle, logmon calls `wevtutil el` and compares the live channel set
  against every bundle's selector.
- **Implemented:** a channel matched by NO bundle selector is "unconfigured."
  Each newly-seen unconfigured channel is logged once to the diagnostic log as
  `unconfigured channel discovered: <name>` and persisted in config under
  `discovered_unconfigured` (so it is not re-logged, and so the GUI can
  highlight it). Channels that later become configured drop out of the set
  automatically. (`detect_unconfigured_channels` / `log_unconfigured_discoveries`.)
- Unconfigured channels are NOT archived until the operator assigns them (via
  the GUI, or by editing a bundle selector to match them).
- **GUI (deferred, §9):** on next run the GUI highlights the persisted
  `discovered_unconfigured` set for operator review.
- **Auto-configuration of matching channels (already works):** a channel that
  matches an existing bundle's selector is picked up dynamically by
  `resolve_bundle_channels` and archived with that bundle's settings — no
  operator action needed. It is therefore "configured," not surfaced as
  unconfigured.
- **RESOLVED 2026-07-13 — no auto-default.** A channel matching no bundle is
  logged once and surfaced for operator assignment; it is NEVER silently
  archived or cleared. (Superseded open question: the rule for
  auto-defaulting a channel that matches NO existing bundle — "default
  unconfigured channels to the same settings as the primary channel; only new
  unconfigured *primary* channels will not be logged." The intended behavior is
  ambiguous and depends on the still-open bundle-selector/bundle model (§10.1).
  logmon only logs + surfaces unmatched channels and takes no auto-default
  action. Resolved in §10.1.)

---

## 4. Trigger Mechanics

### 4.1 Timeframe trigger

- Match usnmon's existing calendar-boundary logic exactly. No rehash of the
  boundary *shape* (calendar-day/week/month/year snapping).
- **All boundaries are UTC (locked 2026-07-09).** Timeframe boundaries,
  rotation anchors, span timestamps, and the retention horizon are computed in
  UTC. logmon has no local-time mode and no timezone configuration — see the
  reasoning below and the resolution of §10.10.
- Trigger fires when the current UTC time crosses the next boundary since the
  last archive.
- **Boundary snapping (added 2026-07-06):** when the timeframe trigger fires,
  the archive's naming/anchor timestamp (`end_dt`) is the boundary that was
  crossed, NOT the wall-clock poll time. A 1M rotation whose boundary is 00:00
  on the 1st, detected by the 00:05 poll, is named for the month that just
  closed and anchors at exactly the boundary, keeping subsequent boundary math
  aligned. Without this, `end_dt = now` never coincided with a calendar
  boundary, so the full-calendar-month name (`<Bundle>_<MonthName>_<YYYY>`)
  could never appear. (Size triggers, which have no calendar boundary, still
  use the current UTC time.)

**Why UTC, and why fixed (not configurable):**
- **Every artifact logmon touches is already UTC.** Windows Event Log records
  store `TimeCreated` as a FILETIME (100-ns ticks since 1601-01-01 **UTC**);
  `wevtutil` export emits those with a trailing `Z`. The USN journal — the
  source behind usnmon's events, which logmon will also archive — stores its
  `TimeStamp` as the same UTC FILETIME. logmon's own manifest `generated` field
  is already UTC. There is no local-time data anywhere in the pipeline; local
  time only ever appears as a display convenience in Event Viewer.
- **So the archive names should mean the same thing as the evidence inside
  them.** Computing boundaries in local time offset the named span from the UTC
  timestamps of the events it contained (near a boundary an archive could be
  labelled for one day but hold events from the next), forcing an auditor to
  reconcile the machine's offset. UTC boundaries make `Security_June_2026`
  contain exactly the events whose UTC timestamps fall in June 2026.
- **No DST edge cases.** UTC has no daylight-saving transitions, so there are no
  23-/25-hour days, no ambiguous or non-existent local wall-clock instants at a
  boundary, and no dependence on the machine's configured timezone.
- **Fixed, not configurable, to prevent drift.** A single immutable standard is
  the point. Allowing `timezone` to be set would let a deployment diverge from
  the standard every other logmon artifact uses, reintroducing exactly the
  reconciliation problem UTC removes. The setting is therefore removed, not just
  defaulted (§3.2).
- **usnmon no longer constrains this.** §10.10 previously required usnmon and
  logmon to change together, because they shared rotation/retention conventions.
  Rotation and retention are being stripped out of usnmon (logmon owns them
  going forward), so that coupling is gone and logmon is free to standardize on
  UTC.

**Implementation status: DONE 2026-07-13.** All boundary/retention/trigger math
now flows through `_utcnow()` (naive-UTC); verified on a simulated UTC-4 host.

### 4.2 Size trigger (CORRECTED 2026-07-13 — per-channel ceiling)

- **The ceiling is PER CHANNEL, read from the OS.** `wevtutil gl` reports each
  channel's `maxSize`. logmon reads it and NEVER sets it (HARD RULE 3).
- **`size_limit_bytes: null` (recommended default) = track the channel's own OS
  `maxSize`.** This can never produce an unreachable threshold.
- **An explicit limit is CLAMPED to the channel's `maxSize`,** with a warning:
  `effective_limit = min(configured, os_maxSize)`.
- **Trigger fires when** `channel fileSize >= effective_limit x 0.95`.
- The manifest records the ceiling that actually governed each capture
  (`caveats.size_trigger`: `os_max_size`, `configured_limit`, `effective_limit`,
  `clamped_to_os_max`).

**What was wrong (both real defects, now fixed):**

1. **The 4 GiB ceiling was fiction.** `_v_size_bytes()` validated against a
   global `EVENT_LOG_OS_MAX_BYTES` of 4 GiB. Measured on a live host:
   Security/System/Application = **20 MiB**, Windows PowerShell = 15 MiB,
   Kernel-Boot/Analytic = 1 MiB. An operator setting the doc's own example value
   of 3.5 GiB on a 20 MiB channel created a threshold that **could never be
   reached** — the size trigger would never fire and the channel would churn
   forever under OS circular overwrite. `_v_size_bytes` is now only an
   upper *sanity* bound; the operative ceiling is per-channel.

2. **`null` disabled the size trigger entirely.** The old code read
   `if not limit: return False`. Since `null` is the GUI's default, a 20 MiB
   CIRCULAR Security log with a 30d timeframe would fill in **~13 days**, wrap,
   and silently shed events for the remaining ~17 days of every cycle. `null`
   now means "use this channel's OS maxSize".

**Poll interval:** 5 minutes.

**Acknowledged residual risk:** if a channel goes from below the threshold to
past its OS maxSize inside one 5-minute poll, the OS overwrites events before
logmon triggers. At the measured rate on the audited host (1.17 rec/min) the
1 MiB of headroom on a 20 MiB channel is ~15.6 hours of runway — ample. During a
BURST it can be seconds. logmon cannot prevent this (it never alters OS log
config); it **detects and quantifies** it exactly (§10.16) and discloses it in
the manifest (§10.17). Growth-rate-aware triggering (§10.15) remains open as
insurance against the burst case.

### 4.3 Trigger interaction

- **Timeframe trip:** produces one archive per timeframe period.
- **Size trip during timeframe:** produces additional archive(s) within the
  same timeframe period. Multiple archives can exist per configured
  timeframe.
- Both triggers are checked at each polling cycle. First trigger to fire
  wins; the trigger that fired resets its clock/state.

### 4.4 Trigger evaluation ordering

- Sequential processing across channels (per operator direction Q11).
- No hard priority order defined in v0.0.1. Bundles are processed in the
  order they appear in the config file.
- If multiple channels trip triggers in the same polling cycle, they are
  processed one at a time. This is safer for `wevtutil` service load and
  disk I/O, at the cost of some latency when many channels trip
  simultaneously.

---

## 5. Archive Management

### 5.1 Archive grouping (revised 2026-07-15, schema v2)

Each poll cycle, active channels are grouped by their effective
**(rotation, retention)** pair. One archive — one zip + per-channel manifests —
is produced per distinct pair that is DUE. Normally 2–3 archives per cycle;
unbounded combinations are supported.

**The one unbreakable invariant:** an archive holds exactly ONE retention term,
so it is always cleanly prunable. Grouping by (rotation, retention) generalizes
the earlier per-primary-channel model; it never mixes retention terms in one zip
(the reason the old combined `EVTX_LOGS` archive was removed).

### 5.2 Archive naming & layout

- **Flat in the archive root** (fewer subdirectories, per operator direction).
- **Filename states the coverage window, the group POLICY, and the safe-delete
  date -- not the contents:**
  `<start>_to_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date>.zip`, or
  `historical_<end>_rot-<ROT>_ret-<RET>_DELETE-AFTER_<date>.zip` for the
  first-run dump. All UTC. The `rot-/ret-` tag is the group identity, so two
  distinct groups written in the same cycle can never collide, and the retention
  term is readable straight off the name. ROT/RET are the terms or `NONE`
  (rotate-off / keep-forever; `ret-NONE` always pairs with `DELETE-AFTER_NEVER`).
- **`DELETE-AFTER_<date>`** is `end + effective_retention`, day-floored;
  keep-forever → `DELETE-AFTER_NEVER`. The pruner deletes only once the current
  UTC date is strictly past the printed date, so the filename is a truthful
  "safe to delete on/after the next day" statement to anyone holding the file
  off-system. The **manifest** carries the authoritative `retention` block
  (`term`, `delete_after_date`, `retention_expires_utc`).
- **Inside the zip**, each channel keeps its own folder
  (`Security/Security_<ts>.evtx` + `.manifest`) — browsable, and mirroring Event
  Viewer's doubled naming.

### 5.3 Coexistence

logmon's `EVENT_LOG_ARCHIVE` is independent of usnmon's archive tree. During the
usnmon phase-out both may archive the `FileSystem` channel (usnmon's own output
channel); logmon detects usnmon's clears as EXTERNAL_CLEAR events, which is a
correct-but-benign true positive during the transition.

## 6. Legal Retention

### 6.1 Retention model (schema v2)

- Retention is a per-channel **effective** term (channel override → provider
  default → global 1y). Every channel in a (rotation, retention) group shares
  the group's term, so each archive is prunable on that single term.
- **Zip-aware:** the delete-after date is `end_of_coverage + term`, so a zip is
  kept until its whole coverage window has aged out — an archive is never
  deleted while it could still hold in-term events.

### 6.2 Retention sweep

- Runs on the retention-check cadence (default daily).
- **`prune_by_delete_after`**: walks the flat archive root, reads the
  `DELETE-AFTER` date from each filename, and deletes only when the current UTC
  date is strictly greater. `DELETE-AFTER_NEVER` is never pruned. This is exact
  and self-consistent with the promise printed on every file; it replaced the
  v1 per-subdirectory span-regex pruner.

### 6.3 Retention logging

Each deletion is logged (filename + the delete-after date that authorized it).

## 7. Service Architecture

### 7.1 Framework

- Same `pywin32` service pattern as usnmon (per operator direction Q6).
- Same install/start/stop/status/uninstall subcommands.
- Same `service_start_type` configuration options (delayed / automatic /
  manual).
- Service name (registered / `_svc_name_`): `LogMonitorService`.
  Display name: `Event Log Archiver (logmon)`. ("logmon" is the product/
  colloquial name; the Windows service is registered as `LogMonitorService`.)

### 7.2 State file — SEPARATE, service-owned (CORRECTED 2026-07-13)

**Runtime state lives in `C:\ProgramData\logmon\logmon_state.json`, NOT in the
config file.**

*Correction of record:* on 2026-07-09 this section was rewritten to say "state is
embedded in `logmon.cfg`, no separate state file," in order to match what the
skeleton code did. **That was the wrong direction.** The original 2026-06-28
design specified a separate state file and was correct; the code should have been
moved to the doc, not the reverse. The GUI requirement proved it: with state in
the config, the service's read-modify-write (8 call sites — anchors, missing
channels, clear failures, discoveries) would **silently clobber operator edits**
saved from the GUI. Restored to a separate file, single-writer.

Contents (see §3.2.2): `engine`, `config_errors`, `bundle_state` (rotation
anchors, missing channels, clear-failure backoff/disable), `channel_state`
(§10.18), `discovered_unconfigured`, `disabled_channels`.

State is **reconstructible** — if the file is lost, logmon re-bootstraps with a
fresh historical capture, which is safe. It therefore needs no backup chain
(unlike the config, which has a validated last-known-good).

### 7.3 Failure handling

**Standing question (Q2b):** Match usnmon's current behavior. Exact behavior
to be documented during code copy audit.

**Provisional handling (revised 2026-07-06 for single-call capture):**
- Clear-with-backup failure (`cl /bu:` returns non-zero, or produces no backup
  file): log to diagnostic log, do NOT clear (wevtutil leaves the OS log intact
  on backup failure), and escalate into the repeated-clear-failure state machine
  below. There is no separate export step to fail (see 2.2).
- Hash/manifest/zip failure after a successful clear: log to diagnostic log;
  the channel has already been cleared, so the extracted backup in the work
  directory is the authoritative copy — retry finalization next cycle.
- Config parse failure (revised 2026-07-09): logmon keeps a last-good backup of
  the config. `_write_config_atomic` copies the current good config to
  `logmon.cfg.bak` immediately before each overwrite. On read, if the primary
  config is ABSENT it is treated as a legitimate first-run empty state; if the
  primary EXISTS but is unreadable/corrupt (bad hand-edit, partial file, disk
  fault), `read_config` falls back to `logmon.cfg.bak` and logs the fallback.
  This prevents a single corrupt config from collapsing to an empty dict — which
  would otherwise silently drop every bundle and halt all archiving. The service
  continues on the last-good config until the primary is corrected.

### 7.3.1 Repeated-clear-failure handling (added 2026-07-06)

A channel that **exists** but whose `wevtutil cl /bu:` keeps failing is handled
DISTINCTLY from a missing channel (10.7). This is deliberately a separate,
uniquely named state so the operator can resolve the correct root cause:

- **Growing backoff.** Each consecutive clear failure increments a per-channel
  counter and sets a growing `next_retry` (base ≈ one poll interval, doubling,
  capped at 6 h). A persistently failing channel is therefore not re-attempted
  every 5-minute poll and does not spam the diagnostic log.
- **Disable after threshold.** After `CLEAR_FAILURE_DISABLE_THRESHOLD` (6)
  consecutive failures, the channel is disabled via a dedicated
  `clear_failed_channels` state (separate from `missing_channels`) and logged as
  **REPEATED CLEAR FAILURE** — explicitly *not* LOG MISSING. It is not attempted
  again until the operator corrects the cause.
- **Auto-recovery.** A successful clear resets the counter and lifts the
  disable.
- **Why a distinct state.** The two states point the operator at different
  fixes:
  - **LOG MISSING** — the channel is gone. Recreate it, or fix the bundle
    selector.
  - **REPEATED CLEAR FAILURE** — the channel is present but un-clearable. Under
    LocalSystem, straightforward permission denials are rare on standard
    channels, so the realistic causes are: a restrictive per-channel
    `channelAccess`/SDDL; an opted-in Analytic/Debug channel (these generally
    cannot be cleared while enabled); an unreachable archive backup path (a UNC
    root is written by the machine account under LocalSystem, which typically
    has no share rights); or another handle holding the log.

**Size-trigger interaction:** a channel that is disabled or in backoff is
excluded from the size-trigger check, so a stuck, ever-growing channel does not
drive a pointless rotation event every cycle.

### 7.4 Reload without restart

Per operator direction Q6b, combined approach:
- Config file mtime watched (on-demand reload)
- Config re-read every 5 minutes as safety net (poll-interval reload)

The service stays running through config changes. Only genuine service
lifecycle events (install/uninstall/upgrade) require a full stop-restart.

---

## 8. Diagnostic Logging

### 8.1 Location

- **File:** `C:\ProgramData\logmon\logmon.log` (per operator direction Q9a)
- **Format:** structured plaintext (matching usnmon's diagnostic log style if
  applicable).
- **Rotation:** size-based only — `RotatingFileHandler`, **5 MB x 3 backups**
  (`logmon.log`, `.1`, `.2`, `.3`). This is logmon's OWN diagnostic log, not an
  OS log, so the policy is ours to set. (Corrected 2026-07-13: an earlier draft
  said "size and age"; there is no age-based rotation and none is needed.)

**Timezone — LOCAL by design (locked 2026-07-09).** The diagnostic log is
deliberately kept in machine **local** time, unlike archives, rotation
boundaries, and legal retention, which are UTC (§4.1 / §10.10). This is an
intentional split, not an inconsistency:

- **Audience.** The diagnostic log is operational telemetry — service start/stop,
  export/clear results, skips, missing/clear-failed channels — read by system
  admins and help desk reasoning about *this machine now*. Local time matches
  their wall clock, their ticketing system, and Event Viewer's default local
  view. Requiring UTC→local conversion on operational logs adds friction exactly
  when it's least wanted (during an incident). The archives, by contrast, are
  legal-retention evidence whose timestamps must align with the UTC times baked
  into the events, for a forensic/legal audience that works in UTC.
- **Ambiguity removed.** Each log line's timestamp carries the local UTC offset
  via a trailing `%z` (e.g. `2026-07-09 15:04:22 -0400`). This resolves the
  twice-yearly DST fall-back ambiguity and lets any line be cross-referenced to
  a UTC archive without guesswork.
- **Millisecond precision dropped (2026-07-09).** Adding the `%z` offset via a
  custom `datefmt` drops the sub-second (`,%03d`) field the default formatter
  included. This is accepted: millisecond precision is not needed for
  operational diagnostic logs, and the unambiguous offset is the better
  trade. (If sub-second ordering is ever needed, a custom formatter can restore
  it alongside the offset.)
- **Do not "fix" to UTC.** The local-time choice is deliberate; the offset makes
  it unambiguous. Changing the diagnostic log to UTC would degrade its
  operational audience's experience for no forensic gain (the archives already
  carry the authoritative UTC record).

### 8.2 What logmon logs

- Service lifecycle events (start, stop, install, uninstall)
- Config load/reload events
- Channel discovery events (new channels found, missing channels detected)
- Archive events per bundle (trigger fired, export result, clear result,
  compression result, hash result)
- Retention deletion events (per archive deleted)
- Errors and warnings
- Config validation errors

### 8.3 Event ID scheme

- logmon does NOT write to a Windows Event Log channel of its own in
  v0.0.1 (per operator direction Q9a — logmon is an archive manager, not an
  event producer).
- Diagnostic log entries use logmon's own internal numbering, not tied to
  usnmon's 900-series operational scheme.
- **Future consideration:** if logmon later gains a Windows Event Log channel
  for diagnostic events, the 900-series scheme from usnmon should be adopted
  for consistency (per Q9b).

---

## 9. GUI Companion (Required for v0.0.1)

**GUI is REQUIRED for v0.0.1.** Without it, there is no way for the operator
to set configuration options. logmon cannot be usable without the GUI.

The GUI does NOT exist yet — it will be developed over the next several days
as part of the v0.0.1 work stream. Nothing to copy from usnmon since usnmon
does not have a GUI.

### 9.1 Design intent

- **Bundle selection UI:** operator sets rotation and retention per bundle,
  not per individual channel.
- **Channel visibility:** GUI displays all channels currently on the system,
  grouped by bundle. Unconfigured channels are highlighted or filterable.
- **New channel discovery:** GUI surfaces channels added since last config
  save.
- **Save action:** signals the service to reload config on-demand, OR
  (fallback) performs the stop → uninstall → reinstall → start cycle.
- **Analytical/Debug channel handling:** operator can override per-channel
  to enable normally-skipped Analytical or Debug channels.
- **Framework:** PySide6 (matches Envelope Printer stack and planned
  usn_stats GUI).

### 9.2 GUI-Service integration

The GUI and the service are separate processes. The GUI writes/reads the
config file and signals the service to reload. The service runs continuously
regardless of whether the GUI is open.

### 9.3 GUI implementation timeline

GUI development happens after Priorities 1-4 (code copy, refactor, config
surface, standalone service verification) but is required for v0.0.1 to ship.
No config-editing surface exists without the GUI.

---

## 10. ITEMS REGISTER (all v0.0.1 items resolved; see status per item)

### 10.0 Open Items Register (as of 2026-07-13)

Consolidated working list. **No code has been written for any item below.**
Items marked NEW arose from the 2026-07-13 `wevtutil` field audit on a live
Windows host (see Appendix A for the empirical evidence).

| # | Item | Section | Status |
|---|------|---------|--------|
| 1 | Bundle selector — curated explicit lists w/ user override | §10.1 | **IMPLEMENTED** — 13 curated bundles ship in the GUI |
| 2 | Analytic skip bug (`"analytical"` vs `Analytic`) | §10.2 | **FIXED 2026-07-13** |
| 3 | `enabled: false` channels — surface as separate list | §10.13 | **CLOSED 2026-07-13** — service skips + surfaces + auto-recovers |
| 4 | Per-channel `maxSize` clamp (not global 4 GB) | §10.14 | **CLOSED 2026-07-13** — clamp + null-tracks-OS-max |
| 5 | `autoBackup` / `wevtutil sl` | §11 | **CLOSED — logmon NEVER calls `sl`.** Locked 2026-07-13 |
| 6 | Size-trigger margin — growth-rate/time-to-full triggering | §10.15 | **DEFERRED to v0.0.2** — original design (Q3c) explicitly ACCEPTED this risk; not in v0.0.1 scope |
| 7 | Overwrite-loss detection & quantification | §10.16 | **IMPLEMENTED 2026-07-13** |
| 8 | Caveats/provenance block in manifest | §10.17 | **IMPLEMENTED 2026-07-13** |
| 9 | Per-channel `channel_state` watermarks | §10.18 | **IMPLEMENTED 2026-07-13** (baseline is one-shot — must precede first clear) |
| 10 | Tamper/loss alert — durable store + visible surface | §10.19 | **IMPLEMENTED** — service store + `logmon_tray.py` watcher |

**Hard rule adopted 2026-07-13:** see §12.1 — never parse `wevtutil` **text**
output for timestamps; XML only.

### 10.1 Channel selection — RESOLVED (schema v2, 2026-07-15)

Superseded by the schema-v2 provider/channel model. There are no bundles and no
selectors: the operator configures channels explicitly (individually or
multi-selected) on the GUI Channels tab, and policy attaches to providers and
channels. Curated recommendations ship as explicit channel→(rotation, retention)
entries applied only to channels that exist on the box. logmon CLEARS what it
archives, so membership is always explicit — never a pattern.

### 10.2 Analytical/Debug channel default handling

Per operator direction Q1c, default = skip Analytical and Debug channels;
allow per-channel override. Implementation detail: how is "Analytical" vs
"Debug" vs "Operational" detected? Via `wevtutil gl <channel>` output
inspection? Locked during implementation.

**RESOLVED (detection) + BUG CONFIRMED (2026-07-13).** Detection IS via
`wevtutil gl <channel>`, which reports a `type:` field. Empirically confirmed
on a live host, the four possible values are:

    Admin | Operational | Analytic | Debug

`should_skip_channel()` compares the type to the literal string
**`"analytical"`**, which is **not one of the four values**. Therefore the
Analytic default-skip **never fires** — Analytic channels are currently
archived AND CLEARED despite this section mandating they be skipped. Debug is
matched correctly. Verified: `type=Analytic -> skip=False`,
`type=Debug -> skip=True`.

Blast radius: on the audited host, 627 of 1,270 channels carry
Analytic/Debug/Diagnostic/Trace/Perf-style names. Many are *direct* channels
that cannot be cleared while enabled, so the bug also drives spurious REPEATED
CLEAR FAILURE churn (§7.3.1).

**Fix:** compare against `"analytic"`. Awaiting operator go-ahead (open item 2).

### 10.3 Failure handling exact behavior — RESOLVED

Documented in full at §7.3 / §7.3.1: clear-with-backup failure (no clear occurs;
OS log intact), the REPEATED CLEAR FAILURE backoff/disable state machine, and
config-parse failure (last-known-good fallback). Deviations from usnmon are
listed in the Revision History rather than silently applied.

### 10.4 Config file schema versioning — RESOLVED

Both files carry `schema_version` (currently `1`). `validate_config()` reports a
mismatch as an error surfaced to the GUI. Migration logic is still deferred to
the first schema-BREAKING change — but the version field now exists, so a future
migration has something to key on. (It did not exist when this item was raised.)

### 10.5 State file format — RESOLVED

logmon DIVERGES from usnmon deliberately. Format is defined at §3.2.2:
service-owned `logmon_state.json` (`engine`, `config_errors`, `bundle_state`,
`channel_state`, `discovered_unconfigured`, `disabled_channels`, `alert_seq`).
The divergence is required by the GUI single-writer contract (§7.2) and by
logmon-specific state (channel watermarks, clear-failure backoff) that usnmon
has no equivalent for.

### 10.6 Legal retention window resolution

Per Q8c operator confirmed: "checked daily, 90-day limit hits, prune, unless
the zip file contains stuff younger than 90-days, then when the youngest
hits 90-days also, prune at that time the entire zip. Just like currently
programmed in usnmon legal retention code, again no need to recreate logic,
we already have it." No open item — matches usnmon exactly.

### 10.7 Channel-missing behavior

Per Q10 operator direction: log LOG MISSING error and disable further
attempts until operator corrects. Precise state file representation of
"disabled due to missing channel" needs schema definition. Locked during
implementation.

**Resolved / implemented (2026-07-06):** stored as `missing_channels` (a sorted
list) per bundle in `bundle_state`. This is SEPARATE from `clear_failed_channels`
(see 7.3.1): a *missing* channel is gone; a *clear-failed* channel is present but
un-clearable. The two states are kept distinct and named distinctly (LOG MISSING
vs REPEATED CLEAR FAILURE) so the operator resolves each with the correct fix.

### 10.8 Empty channel handling

Per Q10b: if channel is empty at archive time, skip archive creation. No
archive file produced for empty channels. Diagnostic log entry noting the
skip.

### 10.9 Bundle-to-channel resolution refresh frequency — RESOLVED

**Every poll (5 min).** `evaluate_all_bundles()` calls `enumerate_channels()`
each cycle and re-resolves every bundle's membership, so a channel that appears
mid-day is picked up on the next cycle. There is no separate slower discovery
pass. Unconfigured channels are likewise detected per poll and logged once
(§3.4).

### 10.10 Timezone handling for legal retention — RESOLVED 2026-07-09

**Resolved: logmon is UTC, always, with no configuration.** All boundary,
anchor, span, and retention math is computed in UTC; there is no local-time
mode and no `timezone` setting (removed from the schema — see §3.2). The
original inheritance question ("logmon uses whatever usnmon uses") is moot:
rotation and retention are being removed from usnmon, so logmon no longer needs
to align with it, and every data source in logmon's pipeline (Event Log
`TimeCreated`, USN journal `TimeStamp`, logmon's own manifests) is already UTC.
Full reasoning in §4.1. This item is closed; the code conversion to UTC was
applied 2026-07-09 (a single `_utcnow()` helper feeds all boundary/retention/
trigger math; the `timezone` key is removed from the schema ordering).

*Superseded text (2026-06-28):* "usnmon's retention logic uses whatever timezone
usnmon currently uses. logmon inherits. If usnmon uses UTC, logmon uses UTC. If
usnmon uses local, logmon uses local. Verified during code copy."

### 10.11 Engine start anchor — RESOLVED: REMOVED

`engine_start_anchor` has been **removed** from the schema. It was never read or
written by any logic, and it is not needed: each bundle establishes its own base
via the first-run **historical bootstrap** (§2.1), which archives the channel's
existing contents, clears them, and records `rotation_anchor`. That per-bundle
anchor is what every subsequent timeframe boundary and retention span is measured
from. A bundle added later ramps from its own first-seen time, which is correct.
A separate global anchor would have been redundant.

Also removed for the same reason: **`rotate_interval_default`** and the
**`--log-interval`** CLI pre-flag (inherited from usnmon). They set a GLOBAL
default rotation cadence, but logmon requires a `timeframe` on every rotating
bundle (`validate_config` disables a bundle that lacks one), so the global
fallback could never fire. Keeping them would have meant an unvalidated raw
config key that silently did nothing. Rotation cadence is per bundle, set in the
GUI.

### 10.12 Test-system reset utility — RESOLVED (logmon_reset.py shipped)

To avoid stale/misconfigured state accumulating on test machines across runs, a
small reset utility can delete logmon's config, backup, and runtime state before
a clean test. Scope and safety to confirm (operator, 2026-07-09):

- **Default safe:** removes `logmon.cfg`, `logmon.cfg.bak`, `logmon.cfg.tmp`
  (config + embedded state), NOT archives.
- **Explicit opt-in flag** required to also delete the archive root (archives
  are legal evidence — never deleted by default).
- Test-only; not shipped as part of the service.

**CLOSED:** `logmon_reset.py` is shipped. Default removes config/state/logs only;
deleting archives requires BOTH `--with-archives` AND `--yes-delete-archives`.
Archives (legal evidence) are never removed casually.

### 10.13 Disabled channels (`enabled: false`) — CLOSED 2026-07-13

**CLOSED 2026-07-13.** Implemented exactly as proposed below: the service reads
`enabled` from `wevtutil gl`, skips disabled channels, records them in
`state.disabled_channels` (separate from missing/unconfigured), auto-recovers
when re-enabled, and never enables them itself (that needs `sl`).

Original analysis:

`wevtutil gl` reports an `enabled:` field. logmon did not originally read it.
A disabled channel produces no events, yet `channel_exists()` returns true for
it (metadata parses fine), so it flows into the archive path and receives a
pointless `cl /bu:` attempt — feeding REPEATED CLEAR FAILURE churn (§7.3.1).

**Proposal:** read `enabled` at resolution time; skip disabled channels, and
surface them in a **separate list** from `discovered_unconfigured` — they are a
distinct condition with a distinct remedy.

**Constraint:** enabling a channel requires `wevtutil sl /e:true`, which logmon
is **forbidden** to call (§11). So logmon can *surface* a disabled channel
("disabled — produces no events; must be enabled by the operator before logmon
can archive it") but can never enable it. The operator enables it out-of-band
(Event Viewer / GPO / their own script), after which logmon picks it up.

### 10.14 Per-channel `maxSize` — CLOSED 2026-07-13

**CLOSED:** `effective_size_limit()` clamps to each channel's OS `maxSize`;
`null` tracks the OS maxSize; the manifest records the governing ceiling. See
§4.2 (revised) and the 2026-07-13f revision entry.

Original problem statement:

§4.2 and `_v_size_bytes()` originally validated
`size_limit_bytes` against a global `EVENT_LOG_OS_MAX_BYTES` of 4 GB. The real
ceiling is **per channel**, reported by `wevtutil gl` as `maxSize`. Measured on
the audited host:

| Channel | maxSize |
|---|---|
| Security | 20,971,520 (20 MiB) |
| System | 20,971,520 (20 MiB) |
| Application | 20,971,520 (20 MiB) |
| Windows PowerShell | 15,728,640 (15 MiB) |
| Kernel-Boot/Analytic | 1,052,672 (~1 MiB) |

The 4 GB ceiling is fiction — the real limit is ~1/200th of it. An operator who
configures `size_limit_bytes: 3.5 GB` on a 20 MiB channel creates a threshold
that **can never be reached**, so the size trigger never fires and the channel
silently churns forever under OS circular overwrite.

**Fix:** read `maxSize` per channel from `gl`; clamp/validate `size_limit_bytes`
against **that channel's** ceiling, not a global constant. Requires revising
§4.2 and `_v_size_bytes()`. Needs **no** `sl` call.

### 10.15 Size-trigger margin — growth-rate triggering — DEFERRED to v0.0.2

The fixed `× 0.95` margin (§4.2) is **scale-dependent**. On a 20 MiB channel it
leaves only **1 MiB** of headroom (~1,100 records at the measured 953-byte
average). Against a 5-minute poll:

- at the **measured** rate on the audited host (1.17 rec/min) → ~15.6 h of
  runway. **Adequate.**
- during a **burst** (brute-force, scan, malware chain, or a busy DC) →
  headroom can be consumed in **seconds**. Inadequate.

Bursts are precisely when the events matter most. Also note: once a circular log
wraps, `fileSize` **pins at maxSize and stops growing**, so file size degrades
from a "about to lose data" signal into an "already lost data" signal.

**Proposal:** replace the fixed fraction with **growth-aware triggering** —
sample per-channel bytes/sec across polls, derive time-to-full, and trigger when
`time_to_full < poll_interval × safety`. Adapts to a 20 MiB and a 4 GB channel
under one rule. **Priority: below §10.14 and §10.16** (it is insurance against
the tail, not an emergency).

### 10.16 Overwrite-loss detection — IMPLEMENTED 2026-07-13

**IMPLEMENTED** in `observe_channel()`; loss count written to the manifest.

`wevtutil gli` reports `oldestRecordNumber` and `numberOfLogRecords`. Empirically
confirmed (Appendix A):

- After a `cl`, the live log **resets** to `oldestRecordNumber = 1`
  (numbering is NOT monotonic across a clear).
- `oldestRecordNumber` thereafter advances **only** when the OS purges records.

Therefore, at any capture:

> **records destroyed during this period = `oldestRecordNumber` − 1**

This is an **exact count**, not an estimate, obtained from a single `gli` read at
capture time. No cross-poll watermark is required for the per-archive figure.

**Critical caveat — purging is BYTE-driven, not record-driven.** Measured: 85
records were destroyed to make room for 22 new (larger) ones. Loss can therefore
**never** be inferred from event counts or file-size deltas. The
`oldestRecordNumber` delta is the *only* authoritative loss counter.

### 10.17 Caveats / provenance block in the manifest — IMPLEMENTED 2026-07-13

logmon cannot guarantee completeness on a circular channel it does not control.
It **can** guarantee that every gap is detected, counted, and disclosed — a far
stronger legal position than an unbackable completeness claim.

**Proposal:** at capture time, record each channel's OS-governed reality into the
per-`.evtx` manifest (which is hashed and optionally signed):

- `enabled`, `type`, `maxSize`, `retention`, `autoBackup`
- `oldestRecordNumber`, `numberOfLogRecords` at capture
- `records_destroyed_this_period` (§10.16)
- any external-clear/tamper finding (§10.18)

Reports/analysis render this as a **caveats section** ("these archives are
governed by Windows with the following settings in effect"), but the
**manifest is the authoritative record** — it travels with the archive and is
tamper-evident.

### 10.18 Per-channel `channel_state` watermarks — IMPLEMENTED 2026-07-13

Persist per-channel counters so loss/tamper survives a service restart (a restart
gap is exactly when loss goes unobserved).

**Placement:** a **top-level `channel_state`, keyed by CHANNEL name** — not
nested under `bundle_state`. The watermark is a property of the OS channel, not
of the bundle that happens to group it; reassigning or renaming a bundle must not
reset a channel's loss history.

```json
"channel_state": {
  "Security": {
    "baseline_oldest_record": 868288,
    "baseline_seen": "2026-07-13 16:16:51",
    "last_oldest_record": 1,
    "last_record_count": 6,
    "last_cleared_by_logmon": "2026-07-13 16:20:49",
    "overwritten_since_baseline": 0
  }
}
```

- **`baseline_oldest_record`** is capturable **only once, at first contact**. On
  the audited host it records that **868,287 Security records were already
  destroyed before logmon ever ran** — drawing a clean, permanent line between
  pre-existing loss (not logmon's) and anything afterward. If not captured at
  install, it is gone forever.
- **Write-on-change only.** `oldestRecordNumber` does not move on a healthy
  (unwrapped) log, so this does not cause per-poll config churn. Keep in memory
  during a run; persist only on delta. This preserves the single-atomic-write
  consistency of the config (§7.2) without 288 rewrites/day.
- **External-clear (tamper) detection:** if `oldestRecordNumber` **drops to 1**
  (or the record count collapses) and **logmon did not clear**, someone else
  cleared the log. Clearing the Security log is a textbook anti-forensic action.
  Note that a clear — by logmon *or* by an attacker — writes an identical **Event
  1102**, so 1102 alone **cannot** distinguish them; the watermark plus logmon's
  own `last_cleared_by_logmon` is what disambiguates. False positives are
  negligible: after a clear, `oldestRecordNumber` stays pinned at 1 until the log
  completely refills (~22,000 records) and begins wrapping.

### 10.19 Tamper/loss alert — durable store + visible surface — IMPLEMENTED 2026-07-13

A tamper indicator that sits unread in a log file is worthless. The alert must be
**broadcast**, but this is **NOT** an event-producer change — §8.3 stands, logmon
does not write to a Windows Event Log channel.

**Constraint — Session 0 isolation.** The service runs as LocalSystem in session
0 and **cannot draw UI** to a user's desktop (no MessageBox, no toast). So a
"visible alert" necessarily means: the **service persists** an alert, and a
**user-mode surface** displays it.

**Design (operator-approved 2026-07-13):**
- Service writes an alert record (e.g. `URGENT.TXT` / `alerts.jsonl`) to a known
  location.
- A **system-tray helper app** watches for it and raises a popup, and/or forwards
  to a SIEM. Handling/forwarding options are configurable.
- **Durability — the alert must be at least as durable as the tamper it reports:**
  - ACL the alert store **SYSTEM-write / Users-read-only**, so an attacker cannot
    trivially erase the evidence that they cleared the log.
  - **Append-only structured records** (JSON lines), not an overwritten blob.
  - **Also write the tamper finding into the archive manifest** (§10.17), which
    is hashed and optionally signed. If the alert file is destroyed, the
    tamper-evident record still survives inside the archive.
- **The tray helper is the CONVENIENCE surface; the MANIFEST is the AUTHORITATIVE
  one.**

---

## 11. Items Explicitly NOT in Scope

- **Modifying OS Event Log configuration. No `wevtutil sl` calls ever.**
  **RE-AFFIRMED AND LOCKED 2026-07-13**, after explicitly reconsidering whether
  logmon should raise `maxSize` or set `autoBackup: true` to prevent event loss.
  Operator position: *"we are a logging archive management system, not a logging
  system. What we need to do is adapt to any potential log setup environment and
  handle the limitations of such settings in a manner that is consistent and
  provides as complete a log as possible."* Supporting reasons:
  - **Group Policy would fight it.** Where channel size/retention is set by GPO
    (standard in managed environments; mandatory under most STIG/CIS baselines),
    an `sl` change is reverted at the next policy refresh (~90 min), producing a
    perpetual flapping war on exactly the machines that matter most.
  - **`autoBackup: true` creates an unmanaged shadow archive.** The OS would
    write `Archive-<Channel>-<timestamp>.evtx` into
    `%SystemRoot%\System32\Winevt\Logs\` — files outside logmon's archive tree,
    unhashed, unmanifested, unpruned, and outside the chain of custody, growing
    on the system drive forever. It would make the problem worse, not better.
  - **It would alter the evidence environment.** On a machine subject to legal
    hold, "did your tool modify the audit configuration?" must be answerable
    **no**.
  - **Empirically verified (2026-07-13):** `wevtutil cl /bu:` leaves `maxSize`,
    `retention`, and `autoBackup` untouched. Capture does not mutate channel
    configuration. The boundary holds.

  logmon instead **inspects, clamps, detects, and discloses** (§10.14, §10.16,
  §10.17). Remediation of a hostile channel configuration is the operator's act,
  performed out-of-band via GPO or Event Viewer.
- **Replacing WEF/WEC.** OS subscriptions continue unchanged.
- **Real-time event streaming.** logmon operates on scheduled cycles, not
  real-time streams.
- **Multi-machine coordination.** logmon runs independently on each machine.
  Cross-machine coordination is out of scope.
- **Cross-platform support.** Windows only. No Linux/macOS considerations.
- **Replacing usnmon.** usnmon continues its role as USN journal event
  recorder. logmon is a separate service for OTHER Windows Event Log
  channels.
- **Test/diagnostic scaffold at start.** Per operator direction Q12, no test
  suite ships in v0.0.1. Manual verification is the initial validation
  approach.
- **Sharing code with add-on modules.** Per operator direction, single
  `logmon.py` file with no `logmon_common.py`. No external modules will
  import logmon internals.

**Explicitly OUTSIDE this project's concern** (not deferred within logmon,
but handled outside logmon entirely):

- **Extraction of archive management from usnmon.** Any future
  simplification of usnmon is a separate concern, handled outside this
  project.
- **Fleet management** and multi-machine bundle policy propagation.
- **SIEM integration.**

---

## 12. What Will NOT Change from usnmon Reference

Code copied from usnmon into logmon must preserve identical behavior for:
- File naming convention
- Compression approach
- Hashing approach
- Legal retention logic (including zip-aware retention)
- Timeframe boundary calculation
- Service registration approach (`pywin32`)
- Service install/start/stop/status/uninstall subcommands
- Config file format (JSON)
- State file approach

Any deviation from usnmon behavior discovered during code copy must be
flagged as an open item, not silently changed.

### 12.1 HARD RULES (violations are defects, not preferences)

**HARD RULE 1 — Never parse `wevtutil` TEXT output for timestamps. XML only.**
*(Adopted 2026-07-13.)*

`wevtutil qe ... /f:text` renders event times in **machine LOCAL time but stamps
them with a `Z` (UTC) suffix**. The suffix is false. Observed on the audited host
(Little Rock, CDT = UTC−5):

    /f:text reports : Date: 2026-07-13T11:11:35.2010000Z
    actual UTC      :       2026-07-13T16:11:35.201
    discrepancy     : 5 hours, silently mislabelled as UTC

Proof it is local: the clear was performed at 16:20:49 UTC, and 22 records were
measured as written in the preceding 20 minutes — so the newest event in the
backup **cannot** predate the clear by five hours. 11:11:35 local = 16:11:35 UTC,
nine minutes before the clear. Consistent.

The underlying storage **is** genuinely UTC — the XML `TimeCreated SystemTime`
attribute is the authoritative value, and the UTC decision (§4.1) stands
unaffected. But any component that shells out to `/f:text` and trusts the `Z`
will silently emit timestamps five hours wrong **inside a legal artifact**.

Applies to: logmon itself, the GUI, report/analysis generators, verification
tooling, and anything downstream. Use `/f:xml` and read `SystemTime`.

*(logmon does not parse event timestamps today — it archives the file whole —
so this is a landmine to be avoided, not a present defect.)*

**HARD RULE 2 — Loss is measured ONLY from the `oldestRecordNumber` delta.**
Never infer event loss from record counts or file-size deltas. Purging is
BYTE-driven, not record-driven: 85 records were observed destroyed to make room
for 22 larger ones. See §10.16.

**HARD RULE 3 — `wevtutil sl` is never invoked.** See §11.

---

## 13. Code Copy Strategy

### 13.1 Single-file target

Per operator direction, logmon consolidates into a single file:
- **Target:** `logmon.py`
- **NO `logmon_common.py`** — usnmon's `usn_common.py` contents that logmon
  needs are inlined into `logmon.py`
- **Rationale:** logmon is a self-contained service. Nothing else imports
  from it. Splitting into modules creates maintenance overhead without
  benefit.

### 13.2 usnmon code retention

usnmon.py and usn_common.py remain UNCHANGED during the code copy. The copy
is duplication, not extraction:
- After copy: usnmon has all its current code + logmon has copies of
  relevant portions.
- Future extraction pass (separate project) removes duplicated code from
  usnmon, leaving logmon as the sole owner.

### 13.3 What gets copied from usnmon

Functions and logic to duplicate into logmon.py:
- Rotation timeframe boundary calculation
- Rotation size threshold checking
- File naming (span-based ISO-8601 format)
- Compression (zip/whatever usnmon uses)
- Hashing (whatever hash function usnmon uses)
- Legal retention logic (including zip-aware retention)
- Service framework code (pywin32 service class, install/uninstall/start/stop
  subcommands, status subcommand if it exists in usnmon)
- Config file read/write
- State file read/write
- Diagnostic logging setup (matching usnmon's style)

### 13.4 What does NOT get copied

- USN journal reading logic
- USN-specific event ID handling (100-106 file events, 500s device events,
  914+ operational events, 925/926 restart detection, etc.)
- `usntag_stats` and related analytical helpers
- `usn_stats.py`, `usn_drill.py`, or any other diag/analysis tools
- Test files, .map files, GitHub_Docs
- USN-specific rotation anchor state machine (if it's USN-specific vs
  general rotation-boundary logic — audit during copy)

### 13.5 What gets added (net-new logmon logic)

- Channel enumeration via `wevtutil el`
- Bundle-to-channel resolution logic
- Single-call atomic capture via `wevtutil cl /bu:` — the backup IS the archive
  (revised 2026-07-06; no `epl` export)
- First-run "historical" bootstrap: archive + clear pre-existing contents and
  seed the rotation anchor (added 2026-07-06)
- Boundary-snapped `end_dt` for calendar rotations (added 2026-07-06)
- One zip per primary channel; no `EVTX_LOGS` combined archive
  (revised 2026-07-06)
- New-channel discovery and diagnostic logging
- Missing-channel detection and per-channel disable state (LOG MISSING)
- Repeated-clear-failure detection: growing backoff + distinct
  `clear_failed_channels` disable state (REPEATED CLEAR FAILURE), separate from
  missing-channel handling (added 2026-07-06)
- Empty-channel skip logic
- Per-bundle configuration parsing and validation
- 5-minute size polling
- On-demand config reload via mtime watch
- Unconfigured-channel discovery: log-once + persist `discovered_unconfigured`
  for GUI highlighting (added 2026-07-09)
- Config resilience: last-good `logmon.cfg.bak` on write + corrupt-primary
  fallback on read (added 2026-07-09)

---

## 14. Implementation Priority Order

### 14.1 v0.0.1 Scope (all priorities required to ship v0.0.1)

**Priority 1: Code copy** — literal duplication of usnmon rotation,
retention, naming, compression, hashing, and service framework code into a
new `logmon.py`. Preserve all existing behavior. usnmon.py remains untouched.

**Priority 2: Refactor for event log operation** — adapt copied code to
operate on Windows Event Log channels via `wevtutil` instead of USN journal.
Add channel enumeration, bundle resolution, extract-and-clear mechanism.

**Priority 3: Configuration surface** — define config file schema, add
config parse/validate/reload logic, define diagnostic log format.

**Priority 4: Standalone service verification** — confirm service
registers, starts, stops, uninstalls independently of usnmon. Confirm both
services can coexist on the same machine.

**Priority 5: GUI** — PySide6 GUI for bundle configuration. Required for
v0.0.1 to be usable. Developed over the several days following P1-P4
completion.

All five priorities must be complete before v0.0.1 ships.

---

## 15. Documentation Deliverables

When v0.0.1 ships, the following documentation should be produced:

- `README.md` — installation, service registration, basic config guide
- `CONFIG.md` — full config file schema, bundle definitions, defaults
- `CHANGELOG.md` — starting with v0.0.1
- `LICENSE` — PolyForm Noncommercial 1.0.0
- `DEPLOYMENT.md` — service install/uninstall guide, coexistence with usnmon
- Diagnostic log format specification (in README or CONFIG)

---

## 16. Repository Setup

- **Repo name:** `logmon` (github.com/OnyxOmega/logmon)
- **License:** PolyForm Noncommercial 1.0.0
- **README:** as above
- **Governance files:** copy governance docs from usnmon
  (CODE_OF_CONDUCT.md, CONTRIBUTING.md, CLA.md, COMMERCIAL.md, SECURITY.md,
  ISSUE_TEMPLATE, PULL_REQUEST_TEMPLATE) — adapted for logmon references

---

## 17. Lock Statement

This document captures the design as discussed and agreed on 2026-06-28, and its
subsequent evolution through implementation. Locked items are in sections 1-16.

**STATUS as of 2026-07-13: the v0.0.1 design is COMPLETE. The §10.0 register
shows ZERO items outstanding for v0.0.1** — every item is IMPLEMENTED, CLOSED, or
(item 6 only) explicitly DEFERRED to v0.0.2 per the original design's accepted
risk. The remaining work is VALIDATION, not design: Priority 4 (standalone
service verification on real Windows/`wevtutil`) has not yet been performed —
everything to date was tested against stubs on Linux.

The "no code written" statement below reflects the original 2026-06-28 lock. As
of 2026-07-06 the v0.0.1 skeleton has been implemented and five operator-approved
revisions have been applied; those changes are recorded in the **Revision
History** at the top of this document and marked inline with "revised 2026-07-06"
in their sections. This document remains the authoritative design reference.

> *Original lock statement (2026-06-28):* No code has been written. No files have
> been copied. No repository has been created.

This document is the authoritative design reference for logmon v0.0.1 work.

**Operator approval required before:**
- Any code is copied from usnmon into logmon
- Any locked decisions are revised
- Any open items are pre-decided
- Any repository is created
- usnmon.py is modified in any way

---

---

## Appendix A — Empirical `wevtutil` Field Audit (2026-07-13)

Measured on a live Windows host (`DESKTOP-8JVDVEQ`, WORKGROUP, CDT/UTC−5).
These are **observations, not assumptions**, and several design decisions rest
directly on them.

### A.1 Channel inventory
- **1,270** channels reported by `wevtutil el`; **118** classic (no `/`).
- ~**627** carry Analytic/Debug/Diagnostic/Trace/Perf-style names.
- Only ~**43** are forensically relevant → curated explicit lists are tractable
  (§10.1).
- Microsoft's own naming is inconsistent: both `Microsoft-Windows-SMBServer/...`
  and `Microsoft-Windows-SmbClient/...` exist. Explicit lists absorb this;
  pattern matching does not.

### A.2 Selector false positives (why §10.1 matters)
The design's own example selector `prefix:Security or contains:-Security-`
matched **19 channels** — only **one** is the Security audit log. The other 18
include `Security-SPP-UX/Analytic` (software-licensing UI) and
`Security-Vault/Performance`. **Because logmon CLEARS what it archives, a false
positive is destructive** — it would wipe 18 unrelated channels and place them
under a 7-year retention hold. Similarly, `prefix:System` matched
`SystemEventsBroker`, which is not the System log.

### A.3 `wevtutil gl` — channel configuration

    type: Admin | Operational | Analytic | Debug     <-- NOT "Analytical" (§10.2 bug)

| Channel | enabled | type | retention | autoBackup | maxSize |
|---|---|---|---|---|---|
| Security | true | Admin | **false** | false | 20,971,520 |
| System | true | Admin | **false** | false | 20,971,520 |
| Application | true | Admin | **false** | false | 20,971,520 |
| Windows PowerShell | true | Admin | **false** | false | 15,728,640 |
| Kernel-Boot/Analytic | **false** | Analytic | true | false | 1,052,672 |

`retention: false` = **circular overwrite, oldest-first**. The log never refuses
new events; it silently discards the oldest — which are exactly the ones logmon
has not yet archived.

### A.4 `wevtutil gli` — the loss evidence

Two reads, 20 minutes apart, **with logmon not running**:

| time (UTC) | oldestRecordNumber | numberOfLogRecords | newest (derived) |
|---|---|---|---|
| 15:56:51 | 868,203 | 22,006 | 890,208 |
| 16:16:51 | 868,288 | 21,943 | 890,230 |

- records **written** in 20 min: **22** (1.10/min — matches the 17-month average)
- records **DESTROYED** in 20 min: **85**
- → **85 old records purged to make room for 22 new ones.** Purging is
  **byte-driven**, not record-driven (HARD RULE 2).

**Cumulative:** ~890,208 records written since 2025-02-01; **22,006 retained**.
**868,202 records — 97.5% of every Security event this machine ever generated —
had already been destroyed** before logmon existed. This is the status quo logmon
exists to end, quantified.

### A.5 `wevtutil cl /bu:` — capture is lossless and non-mutating

| | before clear | after clear |
|---|---|---|
| fileSize | 20,975,616 | 69,632 |
| numberOfLogRecords | 21,943 | **1** |
| oldestRecordNumber | 868,288 | **1** |

- **Record numbering RESETS to 1 on clear** — it is NOT monotonic. This is the
  basis of the §10.16 arithmetic and the §10.18 tamper signal.
- The single record remaining is the **Event 1102** ("audit log was cleared")
  that the clear itself generates. **logmon's own clears emit 1102s identical to
  an attacker's** — 1102 alone cannot distinguish them.
- **Backup integrity — verified three ways:**
  - backup file size = **20,975,616** bytes = **exactly** the channel's
    pre-clear `fileSize`
  - backup `gli` reports **21,943** records = exactly the pre-clear count
  - `Get-WinEvent -Path ... .Count` = **21,943**
  - → **`cl /bu:` is empirically PROVEN lossless.** This retroactively validates
    removing the `epl` export (Revision 2026-07-06 #1): the discarded `/bu:`
    backup was the complete capture all along.
- `gl` after the clear is **identical** to `gl` before: capture does **not**
  mutate channel configuration (§11).
- The archived `.evtx` **preserves the original EventRecordIDs**
  (`oldestRecordNumber: 868,288` inside the backup) even though the live channel
  restarted at 1 — so each archive carries its own provenance.
- An emptied Security log baselines at **69,632 bytes**; `fileSize` moves in
  **64 KiB chunks**, so file size is a coarse signal at small magnitudes.

### A.6 Tooling notes
- `Get-WinEvent -Path <file>` on ~22k events took **several minutes** — it
  materializes every event as a .NET object. **Not viable** for whole-file
  operations. Use `wevtutil gli <file> /lf:true` instead.
- `wevtutil qe /f:text` **mislabels local time as `Z`** — see HARD RULE 1.

---

*End of design lock document.*
