from __future__ import annotations

import sys
from pathlib import Path

import pytest

from alice.app.settings import load_settings
from alice.core.config import ConfigError, default_config_path

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "alice.yaml"


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_config_loads() -> None:
    config = load_settings(REPO_CONFIG)

    assert set(config.models) == {"triage", "reason", "heavy", "consolidate"}
    assert config.models["consolidate"].batch is True
    assert config.budget.hard_daily_usd == 2.50
    assert config.triage.interval_minutes == 1
    assert config.triage.max_batch_tokens == 6000
    assert config.notify.quiet_hours == "22:00-08:00"
    assert config.notify.min_toast_priority == 2
    assert [source.type for source in config.sources] == ["logscan", "message"]
    logscan = config.sources[0].options
    assert logscan["path"] == "C:/"
    assert logscan["globs"] == ["*.log", "*.txt", "*.jsonl"]
    assert logscan["max_files"] == 5000
    assert logscan["refresh_minutes"] == 30
    assert logscan["recent_days"] == 14
    assert logscan["text_only"] is True
    assert logscan["from_start"] is False
    assert config.retention.raw_payload_days == 14
    assert config.retention.max_raw_mb == 50
    assert config.gate.min_salience == 0.5
    assert config.gate.novelty_window_hours == 24
    assert [pattern.name for pattern in config.redaction.patterns] == ["AWS_KEY"]
    assert config.models["triage"].provider == "opencode"
    assert config.models["triage"].model == "opencode-go/deepseek-v4.1-flash"
    assert config.models["triage"].pricing is None
    assert config.memory.core_memory_file == config.data_dir / "memory" / "core.json"
    assert config.memory.hard_max_tokens == 2000
    assert config.memory.embedding.model == "minishlab/potion-multilingual-128M"
    assert config.memory.embedding.dimension == 256
    assert config.plugins.enabled == ()
    assert config.brain.consolidation.hour == 3
    assert config.brain.consolidation.max_events == 500
    assert config.core.backup.enabled is True
    assert config.core.backup.dir == config.data_dir / "backups"
    assert config.core.backup.hour == 4
    assert config.core.backup.keep == 7
    assert config.inputs.files.roots == (Path.home().resolve(),)
    assert config.inputs.files.max_bytes == 262_144
    assert config.inputs.files.max_files == 5
    assert config.core.actions.enabled is True
    assert config.tools.web_domains == ("wikipedia.org",)
    assert config.tools.web_allow_public is True
    assert config.tools.calendar_file == config.data_dir / "calendar.ics"
    assert config.tools.email_enabled is False
    assert config.tools.email_host == "imap.naver.com"
    assert "winget" in config.core.actions.allowed_programs
    assert config.core.actions.timeout_seconds == 120


def test_defaults_and_paths(config_path: Path) -> None:
    config = load_settings(config_path)

    root = config_path.parent.parent
    assert config.root == root
    assert config.data_dir == root / "state"
    assert config.db_path == root / "state" / "alice.db"
    assert config.log_dir == root / "state" / "logs"
    assert config.seed_dir == root / "memory" / "seed"

    # Sections absent from the minimal config fall back to defaults.
    assert config.triage.max_batch_tokens == 3000
    assert config.notify.mode == "digest+toast"
    assert config.retention.raw_payload_days == 14
    assert config.core.backup.enabled is True
    assert config.core.backup.dir == root / "state" / "backups"
    assert config.core.actions.enabled is False
    assert config.models["triage"].batch is False
    assert config.sources[0].options == {"path": "./logs", "glob": "*.log"}
    assert config.memory.hard_max_tokens == 2000
    assert config.memory.core_memory_file == root / "state" / "memory" / "core.json"
    assert config.memory.embedding is None
    assert config.plugins.enabled == ()


def test_memory_section_override(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + (
            "\nmemory:\n"
            "  core_memory_file: 'custom/core.json'\n"
            "  hard_max_tokens: 500\n"
            "  task_ttl_hours: 12\n"
            "  recall_budget_tokens: 300\n"
            "  recall_k: 3\n"
            "  embedding: {model: test/model, dimension: 8}\n"
        ),
    )

    config = load_settings(config_path)

    assert config.memory.core_memory_file == config.data_dir / "custom" / "core.json"
    assert config.memory.hard_max_tokens == 500
    assert config.memory.task_ttl_hours == 12
    assert config.memory.recall_budget_tokens == 300
    assert config.memory.recall_k == 3
    assert config.memory.embedding.dimension == 8


def test_bad_memory_dimension(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + "\nmemory:\n  embedding: {model: test/model, dimension: 0}\n",
    )

    with pytest.raises(ConfigError, match="dimension"):
        load_settings(config_path)


def test_plugins_enabled(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + "\nplugins:\n  enabled: [logfile-extra, toast]\n",
    )

    assert load_settings(config_path).plugins.enabled == ("logfile-extra", "toast")


def test_duplicate_plugin_is_rejected(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + "\nplugins:\n  enabled: [same, same]\n",
    )

    with pytest.raises(ConfigError, match="duplicate"):
        load_settings(config_path)


def test_consolidation_section(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + "\nconsolidation: {hour: 5, max_events: 10, max_tokens: 100}\n",
    )

    config = load_settings(config_path)

    assert config.brain.consolidation.hour == 5
    assert config.brain.consolidation.max_events == 10
    assert config.brain.consolidation.max_tokens == 100


def test_bad_consolidation_hour(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8") + "\nconsolidation: {hour: 24}\n",
    )

    with pytest.raises(ConfigError, match="between 0 and 23"):
        load_settings(config_path)


def test_backup_section(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + "\nbackup: {enabled: false, dir: 'copies', hour: 2, keep: 3}\n",
    )

    config = load_settings(config_path)

    assert config.core.backup.enabled is False
    assert config.core.backup.dir == config.data_dir / "copies"
    assert config.core.backup.hour == 2
    assert config.core.backup.keep == 3


def test_files_section(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + "\nfiles: {roots: ['./docs'], max_bytes: 1000, max_files: 2}\n",
    )

    config = load_settings(config_path)

    assert config.inputs.files.roots == (config.root / "docs",)
    assert config.inputs.files.max_bytes == 1000
    assert config.inputs.files.max_files == 2


def test_actions_section(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8")
        + "\nactions: {enabled: false, allowed_programs: [winget], timeout_seconds: 30}\n",
    )

    config = load_settings(config_path)

    assert config.core.actions.enabled is False
    assert config.core.actions.allowed_programs == ("winget",)
    assert config.core.actions.timeout_seconds == 30


def test_bad_actions_programs(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8") + "\nactions: {allowed_programs: []}\n",
    )

    with pytest.raises(ConfigError, match="allowed_programs"):
        load_settings(config_path)


def test_bad_files_section(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8") + "\nfiles: {roots: []}\n",
    )

    with pytest.raises(ConfigError, match="files.roots"):
        load_settings(config_path)


def test_bad_backup_hour(config_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8") + "\nbackup: {hour: 24}\n",
    )

    with pytest.raises(ConfigError, match="backup.hour"):
        load_settings(config_path)


def test_logscan_source_options(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8").replace(
        "  - {type: logfile, path: './logs', glob: '*.log'}",
        (
            "  - type: logscan\n"
            "    path: 'C:/'\n"
            "    globs: ['*.log', '*.txt', '*.jsonl']\n"
            "    max_files: 100\n"
            "    refresh_minutes: 15\n"
            "    max_depth: 8\n"
            "    recent_days: 0\n"
            "    from_start: false\n"
            "    exclude_dirs: [Windows, 'Program Files']\n"
            "    exclude_paths: ['C:/Temp']\n"
        ),
    )
    write_config(config_path, text)

    config = load_settings(config_path)
    source = config.sources[0]

    assert source.type == "logscan"
    assert source.options["path"] == "C:/"
    assert source.options["globs"] == ["*.log", "*.txt", "*.jsonl"]
    assert source.options["max_files"] == 100
    assert source.options["refresh_minutes"] == 15
    assert source.options["max_depth"] == 8
    assert source.options["recent_days"] == 0
    assert source.options["exclude_dirs"] == ["Windows", "Program Files"]


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (
            "  - {type: logscan, path: './logs', glob: '*.log', globs: ['*.log']}",
            "either glob or globs",
        ),
        ("  - {type: logscan, path: './logs', max_files: 0}", "max_files"),
        ("  - {type: logscan, path: './logs', recent_days: -1}", "recent_days"),
        ("  - {type: logscan, path: './logs', exclude_dirs: 'Windows'}", "exclude_dirs"),
    ],
)
def test_bad_logscan_options(
    config_path: Path, replacement: str, message: str
) -> None:
    text = config_path.read_text(encoding="utf-8").replace(
        "  - {type: logfile, path: './logs', glob: '*.log'}", replacement
    )
    write_config(config_path, text)

    with pytest.raises(ConfigError, match=message):
        load_settings(config_path)


def test_relative_data_dir(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "\ndata_dir: var/alice\n"
    write_config(config_path, text)

    config = load_settings(config_path)
    assert config.data_dir == config_path.parent.parent / "var" / "alice"


def test_absolute_data_dir(config_path: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    write_config(config_path, config_path.read_text(encoding="utf-8") + f"\ndata_dir: '{other.as_posix()}'\n")

    assert load_settings(config_path).data_dir == other


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings(tmp_path / "nope.yaml")


def test_empty_file(config_path: Path) -> None:
    write_config(config_path, "")
    with pytest.raises(ConfigError, match="is empty"):
        load_settings(config_path)


def test_missing_models(config_path: Path) -> None:
    write_config(config_path, "budget:\n  soft_daily_usd: 1.0\n")
    with pytest.raises(ConfigError, match="missing required section: models"):
        load_settings(config_path)


def test_missing_tier(config_path: Path) -> None:
    write_config(
        config_path,
        "models:\n"
        "  triage: {provider: fake, model: fake-triage}\n"
        "  reason: {provider: fake, model: fake-reason}\n",
    )
    with pytest.raises(ConfigError, match="missing required tier"):
        load_settings(config_path)


def test_model_tier_needs_provider_and_model(config_path: Path) -> None:
    write_config(
        config_path,
        "models:\n"
        "  triage: {provider: fake, model: fake-triage}\n"
        "  reason: {provider: fake, model: fake-reason}\n"
        "  consolidate: {provider: fake, model: fake-consolidate}\n"
        "  heavy: {model: fake-heavy}\n",
    )
    with pytest.raises(ConfigError, match=r"models\.heavy\.provider"):
        load_settings(config_path)


def test_model_tier_rejects_unknown_option(config_path: Path) -> None:
    write_config(
        config_path,
        "models:\n"
        "  triage: {provider: fake, model: fake-triage, temperature: 1}\n"
        "  reason: {provider: fake, model: fake-reason}\n"
        "  consolidate: {provider: fake, model: fake-consolidate}\n",
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_settings(config_path)


def test_soft_cap_above_hard_cap(config_path: Path) -> None:
    write_config(
        config_path,
        "models:\n"
        "  triage: {provider: fake, model: fake-triage}\n"
        "  reason: {provider: fake, model: fake-reason}\n"
        "  consolidate: {provider: fake, model: fake-consolidate}\n"
        "budget: {soft_daily_usd: 5, hard_daily_usd: 1}\n",
    )
    with pytest.raises(ConfigError, match="soft_daily_usd"):
        load_settings(config_path)


def test_negative_budget(config_path: Path) -> None:
    write_config(
        config_path,
        "models:\n"
        "  triage: {provider: fake, model: fake-triage}\n"
        "  reason: {provider: fake, model: fake-reason}\n"
        "  consolidate: {provider: fake, model: fake-consolidate}\n"
        "budget: {hard_daily_usd: -1}\n",
    )
    with pytest.raises(ConfigError, match="greater than zero"):
        load_settings(config_path)


def test_bad_quiet_hours(config_path: Path) -> None:
    write_config(
        config_path,
        "models:\n"
        "  triage: {provider: fake, model: fake-triage}\n"
        "  reason: {provider: fake, model: fake-reason}\n"
        "  consolidate: {provider: fake, model: fake-consolidate}\n"
        "notify: {quiet_hours: '25:00-08:00'}\n",
    )
    with pytest.raises(ConfigError, match="quiet_hours"):
        load_settings(config_path)


def test_unknown_top_level_key(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "\nmodles: {}\n"
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="unknown key"):
        load_settings(config_path)


def test_source_needs_type(config_path: Path) -> None:
    write_config(config_path, config_path.read_text(encoding="utf-8") + "  - {path: './x'}\n")
    with pytest.raises(ConfigError, match="sources"):
        load_settings(config_path)


def test_secret_in_config_rejected(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "\nanthropic_api_key: sk-ant-123\n"
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="secrets must not be stored"):
        load_settings(config_path)


def test_secret_inside_sources_rejected(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "  - {type: message, token: abc123}\n"
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="secrets must not be stored"):
        load_settings(config_path)


def test_gate_defaults_when_absent(config_path: Path) -> None:
    config = load_settings(config_path)

    assert config.gate.min_salience == 0.5
    assert config.gate.novelty_window_hours == 24.0
    assert len(config.gate.rules) == 6
    assert config.gate.source_weights["message"] == 1.2
    assert config.redaction.patterns == ()


def test_gate_and_redaction_sections_load(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + (
        "\ngate:\n"
        "  min_salience: 0.8\n"
        "  novelty_window_hours: 6\n"
        "  source_weights: {logfile: 2.0}\n"
        "  rules:\n"
        "    - {name: deploy, pattern: 'deploy', weight: 0.9}\n"
        "redaction:\n"
        "  patterns:\n"
        "    - {name: project_token, pattern: '\\bTOK-[0-9]{4}\\b'}\n"
    )
    write_config(config_path, text)

    config = load_settings(config_path)

    assert config.gate.min_salience == 0.8
    assert config.gate.novelty_window_hours == 6.0
    assert dict(config.gate.source_weights) == {"logfile": 2.0}
    assert [rule.name for rule in config.gate.rules] == ["deploy"]
    assert config.redaction.patterns[0].name == "PROJECT_TOKEN"
    assert config.redaction.patterns[0].pattern.search("prefix TOK-1234 suffix")


def test_min_salience_out_of_range(config_path: Path) -> None:
    write_config(config_path, config_path.read_text(encoding="utf-8") + "\ngate: {min_salience: 1.5}\n")
    with pytest.raises(ConfigError, match="min_salience"):
        load_settings(config_path)


def test_invalid_gate_regex(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + (
        "\ngate:\n  rules:\n    - {name: broken, pattern: '(', weight: 0.5}\n"
    )
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="invalid regex"):
        load_settings(config_path)


def test_unknown_source_type(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "  - {type: screen}\n"
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="unsupported source type"):
        load_settings(config_path)


def test_source_requires_path(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "  - {type: logfile}\n"
    write_config(config_path, text)
    with pytest.raises(ConfigError, match=r"sources\[1\]\.path"):
        load_settings(config_path)


def test_message_adapter_must_be_built(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "  - {type: message, adapter: imap, path: './x'}\n"
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="not built yet"):
        load_settings(config_path)


def test_unknown_source_encoding(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + "  - {type: logfile, path: './x', encoding: made-up}\n"
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="unknown encoding"):
        load_settings(config_path)


def test_redaction_pattern_name_must_be_placeholder(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8") + (
        "\nredaction:\n  patterns:\n    - {name: 'bad name', pattern: 'x'}\n"
    )
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="letters, digits, and underscores"):
        load_settings(config_path)


def test_fake_model_has_no_pricing_by_default(config_path: Path) -> None:
    config = load_settings(config_path)

    assert config.models["triage"].pricing is None


def test_pricing_must_be_complete(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8").replace(
        "model: fake-triage}", "model: fake-triage, pricing: {input_per_mtok: 1.0}}"
    )
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="missing required key"):
        load_settings(config_path)


def test_negative_pricing_is_rejected(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8").replace(
        "model: fake-triage}",
        "model: fake-triage, pricing: {input_per_mtok: -1, output_per_mtok: 1}}",
    )
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="must not be negative"):
        load_settings(config_path)


def test_unknown_provider_is_rejected(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8").replace(
        "provider: fake", "provider: local-llama"
    )
    write_config(config_path, text)
    with pytest.raises(ConfigError, match="unknown provider"):
        load_settings(config_path)


def test_frozen_default_config_sits_next_to_the_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "alice.exe"))

    assert default_config_path() == tmp_path / "config" / "alice.yaml"


def test_cli_data_dir_override(config_path: Path, tmp_path: Path) -> None:
    target = tmp_path / "custom-data"

    config = load_settings(config_path, data_dir=target)

    assert config.data_dir == target
    assert config.seed_dir == config_path.parent.parent / "memory" / "seed"


def test_env_data_dir_override(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "env-data"
    monkeypatch.setenv("ALICE_DATA_DIR", str(target))

    assert load_settings(config_path).data_dir == target


def test_yaml_data_dir_beats_the_environment(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8") + "\ndata_dir: ./from-config\n",
    )
    monkeypatch.setenv("ALICE_DATA_DIR", str(tmp_path / "from-env"))

    assert (
        load_settings(config_path).data_dir
        == config_path.parent.parent / "from-config"
    )


def test_cli_data_dir_beats_yaml(config_path: Path, tmp_path: Path) -> None:
    write_config(
        config_path,
        config_path.read_text(encoding="utf-8") + "\ndata_dir: ./from-config\n",
    )
    target = tmp_path / "from-cli"

    assert load_settings(config_path, data_dir=target).data_dir == target


def test_frozen_run_uses_local_app_data(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "Local"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    config = load_settings(config_path)

    assert config.data_dir == local / "Alice"
    assert config.seed_dir == local / "Alice" / "memory" / "seed"


def test_frozen_run_honours_a_data_dir_override(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "portable-data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    config = load_settings(config_path, data_dir=target)

    assert config.data_dir == target
    assert config.seed_dir == target / "memory" / "seed"


def test_named_profile_data_dir(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("ALICE_PROFILE", "work")

    assert (
        load_settings(config_path).data_dir
        == tmp_path / "Local" / "Alice" / "profiles" / "work"
    )


def test_invalid_profile_is_a_config_error(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALICE_PROFILE", "../escape")

    with pytest.raises(ConfigError, match="ALICE_PROFILE"):
        load_settings(config_path)

