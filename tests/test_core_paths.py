from __future__ import annotations

import sys
from pathlib import Path

import pytest

from alice.core import paths


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALICE_DATA_DIR", "ALICE_PROFILE", "LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_source_default_is_the_repo_state(tmp_path: Path, monkeypatch, clean_env) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert paths.resolve_data_dir(tmp_path) == tmp_path / "state"


def test_explicit_override_is_relative_to_the_root(tmp_path: Path, clean_env) -> None:
    assert paths.resolve_data_dir(tmp_path, "Var/Alice") == tmp_path / "Var" / "Alice"


def test_absolute_override_wins(tmp_path: Path, clean_env) -> None:
    other = tmp_path / "elsewhere"

    assert paths.resolve_data_dir(tmp_path, other) == other


def test_env_data_dir(tmp_path: Path, monkeypatch, clean_env) -> None:
    monkeypatch.setenv("ALICE_DATA_DIR", str(tmp_path / "envdata"))

    assert paths.resolve_data_dir(tmp_path) == tmp_path / "envdata"


def test_frozen_default_is_local_app_data(
    tmp_path: Path, monkeypatch, clean_env
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    assert paths.resolve_data_dir(tmp_path) == tmp_path / "Local" / "Alice"


def test_named_profile(tmp_path: Path, monkeypatch, clean_env) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("ALICE_PROFILE", "work")

    assert (
        paths.resolve_data_dir(tmp_path)
        == tmp_path / "Local" / "Alice" / "profiles" / "work"
    )


def test_shared_profile_is_the_base(tmp_path: Path, monkeypatch, clean_env) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("ALICE_PROFILE", "shared")

    assert paths.resolve_data_dir(tmp_path) == tmp_path / "Local" / "Alice"


def test_invalid_profile_name_is_rejected(monkeypatch, clean_env) -> None:
    monkeypatch.setenv("ALICE_PROFILE", "../escape")

    with pytest.raises(ValueError, match="ALICE_PROFILE"):
        paths.profile_root()


def test_mode_reports_source_or_portable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.runtime_mode() == "source"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.runtime_mode() == "portable"


def test_migration_moves_legacy_state(
    tmp_path: Path, monkeypatch, clean_env
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    app = tmp_path / "app"
    legacy = app / "state"
    legacy.mkdir(parents=True)
    (legacy / "alice.db").write_text("db", encoding="utf-8")
    target = tmp_path / "profile"

    moved = paths.migrate_legacy_state(app, target)

    assert moved == target
    assert (target / "alice.db").read_text(encoding="utf-8") == "db"
    assert not legacy.exists()


def test_migration_skips_source_runs(tmp_path: Path, monkeypatch, clean_env) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    legacy = tmp_path / "app" / "state"
    legacy.mkdir(parents=True)
    (legacy / "alice.db").write_text("db", encoding="utf-8")

    assert paths.migrate_legacy_state(tmp_path / "app", tmp_path / "profile") is None
    assert (legacy / "alice.db").is_file()


def test_migration_uses_an_empty_target(
    tmp_path: Path, monkeypatch, clean_env
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    legacy = tmp_path / "app" / "state"
    legacy.mkdir(parents=True)
    (legacy / "marker").write_text("x", encoding="utf-8")
    target = tmp_path / "profile"
    target.mkdir()

    assert paths.migrate_legacy_state(tmp_path / "app", target) == target
    assert (target / "marker").is_file()


def test_migration_refuses_to_merge_two_stores(
    tmp_path: Path, monkeypatch, clean_env, caplog
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    legacy = tmp_path / "app" / "state"
    legacy.mkdir(parents=True)
    (legacy / "old.db").write_text("old", encoding="utf-8")
    target = tmp_path / "profile"
    target.mkdir(parents=True)
    (target / "new.db").write_text("new", encoding="utf-8")

    with caplog.at_level("WARNING", logger="alice.core.paths"):
        moved = paths.migrate_legacy_state(tmp_path / "app", target)

    assert moved is None
    assert (legacy / "old.db").is_file()
    assert (target / "new.db").is_file()
    assert any("already contains data" in record.message for record in caplog.records)

