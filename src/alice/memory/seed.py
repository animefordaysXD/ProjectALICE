"""Seed templates for hard memory.

`alice init` creates any missing file in `memory/seed`. Jo edits them; the
files themselves never write to the database. Applying them to hard memory
goes through `alice approve` (M3).
"""

from __future__ import annotations

from pathlib import Path

SEED_TEMPLATES: dict[str, str] = {
    "identity.md": (
        "# Identity\n\n"
        "<!-- Jo: who Alice is, how she should speak, and what she must never do. -->\n"
        "<!-- This file seeds hard memory. Only `alice approve` may apply changes. -->\n\n"
        "- Name: Alice\n"
        "- Role: secretary for Jo, running locally on Jo's Windows PC.\n"
        "- Tone:\n"
    ),
    "rules.md": (
        "# Rules\n\n"
        "<!-- Jo: standing rules for how Alice decides what matters and how she behaves. -->\n"
        "<!-- This file seeds hard memory. Only `alice approve` may apply changes. -->\n\n"
        "- Alice suggests; she never acts on Jo's behalf.\n"
        "- Alice never sends raw logs or raw messages to a model.\n"
    ),
    "facts.md": (
        "# Facts\n\n"
        "<!-- Jo: stable facts Alice should always know (projects, names, preferences). -->\n"
        "<!-- This file seeds hard memory. Only `alice approve` may apply changes. -->\n\n"
    ),
}


def ensure_seed_files(seed_dir: Path) -> list[Path]:
    """Create missing seed files; never overwrite an edited file."""
    seed_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name, text in SEED_TEMPLATES.items():
        path = seed_dir / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            created.append(path)
    return created
