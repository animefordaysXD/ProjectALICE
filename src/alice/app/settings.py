"""Load the single YAML file and compose per-module settings.

This is the only place that knows every config section. Each module owns its
own parser in `<module>/config.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..brain.config import BrainConfig, Triage, parse_brain
from ..core.config import (
    ConfigError,
    CoreConfig,
    Retention,
    default_config_path,
    load_yaml,
    parse_backup,
    parse_retention,
    reject_secrets,
    reject_unknown,
    resolve_path,
)
from ..core.paths import is_frozen, resolve_data_dir
from ..inputs.config import InputsConfig, Source, parse_inputs
from ..memory.config import MemoryConfig, parse_memory
from ..models.config import Budget, ModelsConfig, parse_models
from ..outputs.config import Notify, OutputsConfig, parse_outputs
from ..plugins.config import PluginsConfig, parse_plugins

TOP_LEVEL = {
    "models",
    "budget",
    "triage",
    "sources",
    "gate",
    "redaction",
    "notify",
    "retention",
    "data_dir",
    "memory",
    "plugins",
    "consolidation",
    "backup",
    "files",
}


@dataclass(frozen=True)
class AppSettings:
    path: Path
    root: Path
    core: CoreConfig
    models: ModelsConfig
    inputs: InputsConfig
    memory: MemoryConfig
    brain: BrainConfig
    outputs: OutputsConfig
    plugins: PluginsConfig

    @property
    def data_dir(self) -> Path:
        return self.core.data_dir

    @property
    def seed_dir(self) -> Path:
        return self.core.seed_dir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "alice.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    # Convenience accessors used by the CLI and older call sites. Modules use
    # their own settings object; these exist so the command layer stays terse.
    @property
    def budget(self) -> Budget:
        return self.models.budget

    @property
    def sources(self) -> tuple[Source, ...]:
        return self.inputs.sources

    @property
    def gate(self):
        return self.inputs.gate

    @property
    def redaction(self):
        return self.inputs.redaction

    @property
    def triage(self) -> Triage:
        return self.brain.triage

    @property
    def notify(self) -> Notify:
        return self.outputs.notify

    @property
    def retention(self) -> Retention:
        return self.core.retention


def load_settings(
    path: Path | str | None = None, *, data_dir: Path | str | None = None
) -> AppSettings:
    """Read and validate the config into module-owned settings."""
    config_path = (
        Path(path).expanduser().resolve()
        if path is not None
        else default_config_path().expanduser().resolve()
    )
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    raw = load_yaml(config_path)
    reject_secrets(raw)
    reject_unknown(raw, TOP_LEVEL, "config")

    root = config_path.parent.parent if config_path.parent.name == "config" else config_path.parent
    data_path = _resolve_data_dir(raw, root, data_dir)
    seed_dir = data_path / "memory" / "seed" if is_frozen() else root / "memory" / "seed"

    return AppSettings(
        path=config_path,
        root=root,
        core=CoreConfig(
            data_dir=data_path,
            seed_dir=seed_dir,
            retention=parse_retention(raw.get("retention")),
            backup=parse_backup(raw.get("backup"), data_path),
        ),
        models=parse_models(raw.get("models"), raw.get("budget")),
        inputs=parse_inputs(
            raw.get("sources"),
            raw.get("gate"),
            raw.get("redaction"),
            raw.get("files"),
            root=root,
        ),
        memory=parse_memory(raw.get("memory"), data_path),
        brain=parse_brain(raw.get("triage"), raw.get("consolidation")),
        outputs=parse_outputs(raw.get("notify")),
        plugins=parse_plugins(raw.get("plugins")),
    )


def _resolve_data_dir(
    raw: dict[str, Any], root: Path, override: Path | str | None
) -> Path:
    if override is not None:
        return resolve_path(Path(override).expanduser(), root)
    value = raw.get("data_dir")
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("data_dir must be a non-empty path string")
        return resolve_path(Path(value).expanduser(), root)
    try:
        return resolve_data_dir(root)
    except (ValueError, OSError) as exc:
        raise ConfigError(f"cannot resolve the data directory: {exc}") from exc
