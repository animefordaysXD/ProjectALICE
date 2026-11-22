from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from alice.core.db import init_db

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_stress_generator_and_metrics(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    messages = tmp_path / "messages.txt"
    generated = run(
        "scripts/stress_logs.py",
        "--root",
        str(logs),
        "--messages",
        str(messages),
        "--duration",
        "1",
        "--interval",
        "0.05",
        "--rotate-seconds",
        "1",
        "--truncate-seconds",
        "1",
        "--flood-seconds",
        "1",
    )
    assert generated.returncode == 0, generated.stderr
    assert (logs / "app-0.log").is_file()
    assert messages.is_file()

    db = tmp_path / "alice.db"
    init_db(db)
    csv = tmp_path / "metrics.csv"
    for index in range(2):
        result = run(
            "scripts/stress_metrics.py",
            "--db",
            str(db),
            "--csv",
            str(csv),
            "--data-dir",
            str(tmp_path),
            "--rss-mb",
            str(10 + index),
            "--processes",
            "2",
        )
        assert result.returncode == 0, result.stderr

    summary = tmp_path / "summary.md"
    report = run(
        "scripts/stress_report.py",
        "--csv",
        str(csv),
        "--out",
        str(summary),
        "--handover",
        "test-handover",
    )
    assert report.returncode == 0, report.stderr
    text = summary.read_text(encoding="utf-8")
    assert "# Alice stress summary" in text
    assert "- Samples: 2" in text
    assert "test-handover" in text
