"""Where mutable runtime data lives.

The application folder must stay replaceable: unzipping a new build over it
must never destroy the ledger, raw payloads, or memory. Packaged Alice
therefore keeps data under the per-user location, while source runs keep the
familiar `<repo>/state` folder.

Overrides, highest priority first:
  --data-dir PATH        CLI flag (handled by the caller)
  data_dir: PATH         YAML, resolved against the app root
  ALICE_DATA_DIR         environment variable
  ALICE_PROFILE          named profile under the per-user base
  default                %LOCALAPPDATA%\\Alice when frozen, else <root>/state
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from pathlib import Path

APP_ID = "Alice"
STATE_DIRNAME = "state"
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

logger = logging.getLogger(__name__)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_mode() -> str:
    """`source` for a checkout, `portable` for the built exe."""
    return "portable" if is_frozen() else "source"


def local_data_root() -> Path:
    """Per-user data directory for this platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base).expanduser()
        return Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base).expanduser() if base else Path.home() / ".local" / "share"


def profile_root() -> Path:
    """Per-user Alice directory for the active profile."""
    base = local_data_root() / APP_ID
    profile = os.environ.get("ALICE_PROFILE", "").strip()
    if not profile or profile.casefold() == "shared":
        return base
    if not _PROFILE_RE.fullmatch(profile):
        raise ValueError(
            "ALICE_PROFILE must be a simple name (letters, digits, dot, dash, "
            "underscore; at most 64 characters)"
        )
    return base / "profiles" / profile


def resolve_data_dir(root: Path, override: Path | str | None = None) -> Path:
    """Resolve the data directory when the config does not set one."""
    if override is not None:
        return _relative_to(Path(override).expanduser(), root)

    env_dir = os.environ.get("ALICE_DATA_DIR", "").strip()
    if env_dir:
        return _relative_to(Path(env_dir).expanduser(), root)

    if is_frozen() or os.environ.get("ALICE_PROFILE", "").strip():
        return profile_root()

    return root / STATE_DIRNAME


def migrate_legacy_state(
    legacy_root: Path, data_dir: Path, *, frozen: bool | None = None
) -> Path | None:
    """Move an old `<app>/state` folder into the per-user data directory.

    Runs once, before the database is opened. If the target already has data,
    the legacy folder is left untouched and a warning is logged rather than
    silently merging two stores.
    """
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return None

    legacy = Path(legacy_root) / STATE_DIRNAME
    target = Path(data_dir)
    try:
        if not legacy.is_dir() or legacy.resolve() == target.resolve():
            return None
    except OSError:
        return None

    if target.exists():
        try:
            if any(target.iterdir()):
                logger.warning(
                    "legacy state left at %s because %s already contains data",
                    legacy,
                    target,
                )
                return None
            target.rmdir()
        except OSError as exc:
            logger.warning("could not inspect %s for migration: %s", target, exc)
            return None

    staged = target.with_name(f"{target.name}.migrating")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            shutil.rmtree(staged)
        shutil.move(str(legacy), str(staged))
        staged.rename(target)
    except OSError as exc:
        logger.warning("could not move legacy state %s to %s: %s", legacy, target, exc)
        return None

    logger.info("moved legacy state from %s to %s", legacy, target)
    return target


def _relative_to(path: Path, root: Path) -> Path:
    if path.is_absolute():
        return path
    return root / path
