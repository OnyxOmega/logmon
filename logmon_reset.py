"""logmon_reset.py -- TEST-SYSTEM reset utility for logmon.  YASDC

Removes logmon's config + embedded runtime state so a test machine starts from
a clean slate, without leaving stale/misconfigured JSON behind between runs.

SAFETY MODEL (deliberately conservative -- archives are legal evidence):
  * By DEFAULT this removes ONLY the config + state files:
        logmon.cfg, logmon.cfg.bak, logmon.cfg.tmp
    and the diagnostic logs:
        logmon.log, logmon.log.1, logmon.log.2, logmon.log.3
    It does NOT touch the archive directory.
  * To ALSO delete the archive root you must pass BOTH --with-archives AND
    --yes-delete-archives. Without both, archives are never removed.
  * --dry-run prints what would be removed and deletes nothing.

This is a TEST-ONLY tool. It is not part of the logmon service and should not be
shipped to production machines. See LOGMON design lock 10.12.

Usage:
    python logmon_reset.py                       # remove config/state/logs
    python logmon_reset.py --dry-run             # show what would be removed
    python logmon_reset.py --with-archives --yes-delete-archives
"""

import argparse
import os
import shutil
import sys


def config_dir():
    """logmon's config/state directory: %ProgramData%\\logmon."""
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, "logmon")


def config_path():
    return os.path.join(config_dir(), "logmon.cfg")


def _read_archive_root():
    """Best-effort read of archive_root from the config BEFORE it is deleted, so
    --with-archives knows what to remove. Falls back to the documented default.
    Returns None if it cannot be determined safely."""
    default = r"C:\ProgramData\logmon\EVENT_LOG_ARCHIVE"
    try:
        import json
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        root = cfg.get("archive_root") or default
        return root
    except Exception:
        return default


# Config/state/log files to remove by default (relative to config_dir()).
_STATE_FILES = (
    "logmon.cfg",
    "logmon.cfg.bak",
    "logmon.cfg.tmp",
    "logmon.log",
    "logmon.log.1",
    "logmon.log.2",
    "logmon.log.3",
)


def _remove_file(path, dry_run):
    if not os.path.exists(path):
        return False
    if dry_run:
        print("  [dry-run] would remove file:", path)
        return True
    try:
        os.remove(path)
        print("  removed file:", path)
        return True
    except Exception as exc:
        print("  FAILED to remove %s: %r" % (path, exc), file=sys.stderr)
        return False


def _remove_tree(path, dry_run):
    if not os.path.isdir(path):
        print("  archive root not present (nothing to remove):", path)
        return False
    if dry_run:
        print("  [dry-run] would DELETE archive tree:", path)
        return True
    try:
        shutil.rmtree(path)
        print("  DELETED archive tree:", path)
        return True
    except Exception as exc:
        print("  FAILED to delete %s: %r" % (path, exc), file=sys.stderr)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reset logmon config/state on a TEST system.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be removed; delete nothing")
    ap.add_argument("--with-archives", action="store_true",
                    help="also delete the archive root (requires "
                         "--yes-delete-archives)")
    ap.add_argument("--yes-delete-archives", action="store_true",
                    help="explicit confirmation required to delete archives")
    args = ap.parse_args(argv)

    cdir = config_dir()
    print("logmon reset -- config dir: %s%s"
          % (cdir, "  (DRY RUN)" if args.dry_run else ""))

    print("Config / state / logs:")
    removed_any = False
    for name in _STATE_FILES:
        if _remove_file(os.path.join(cdir, name), args.dry_run):
            removed_any = True
    if not removed_any:
        print("  (nothing to remove)")

    if args.with_archives:
        root = _read_archive_root()
        print("Archives:")
        if not args.yes_delete_archives:
            print("  REFUSED: --with-archives requires --yes-delete-archives.")
            print("  Archives are legal evidence and are never deleted without "
                  "explicit confirmation.")
            return 2
        if not root:
            print("  could not determine archive root; skipping.",
                  file=sys.stderr)
            return 2
        _remove_tree(root, args.dry_run)
    else:
        print("Archives: left untouched (pass --with-archives "
              "--yes-delete-archives to remove).")

    print("Done." if not args.dry_run else "Dry run complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
