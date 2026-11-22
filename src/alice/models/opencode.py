"""OpenCode Go provider.

Alice delegates a request to the OpenCode CLI (`opencode run --format json`).
That CLI is already signed in and routes to OpenCode Go models, so Alice needs
no model API key of its own.

Trade-offs (see DECISIONS.md):
- OpenCode adds its own system prompt, so calls cost thousands of input tokens.
  OpenCode Go is a subscription, so the reported dollar cost is 0; token usage
  is still logged.
- Each call is a fresh, archived session in a dedicated empty working
  directory. No session history carries over.
- The `plan` agent is used by default: read-only, no edits or commands.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from .base import Completion, Message, Provider, ProviderError, ProviderNotConfigured

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 180.0
DEFAULT_AGENT = "plan"
CLI_ENV_VARS = ("OPENCODE_CLI", "OPENCODE_CLI_PATH")
_TRANSIENT_HINTS = (
    "rate limit",
    "rate-limit",
    "429",
    "timeout",
    "timed out",
    "temporarily",
    "connection reset",
    "502",
    "503",
    "504",
)

Runner = Callable[[list[str], str, float, str | None], "subprocess.CompletedProcess[str]"]


class OpenCodeProvider(Provider):
    name = "opencode"
    reports_cost = True

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        model: str | None = None,
        agent: str = DEFAULT_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: str | Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.agent = agent
        self.timeout = timeout
        self.cwd = str(cwd) if cwd is not None else None
        self.runner = runner or _run_subprocess
        if runner is None:
            resolved = str(cli_path) if cli_path else find_cli()
            if not resolved:
                raise ProviderNotConfigured(
                    "the OpenCode CLI was not found; install the OpenCode desktop app "
                    "or set OPENCODE_CLI to opencode-cli.exe"
                )
            self.cli_path = resolved
        else:
            self.cli_path = str(cli_path or "opencode-cli")

    def complete(
        self,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        cache_prefix_len: int | None = None,
    ) -> Completion:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        # OpenCode handles its own prompt caching; the prefix hint is unused.
        prompt = flatten_messages(messages)
        command = [
            self.cli_path,
            "run",
            "--format",
            "json",
            "--archived",
            "--model",
            model,
        ]
        if self.agent:
            command.extend(["--agent", self.agent])

        try:
            result = self.runner(command, prompt, self.timeout, self.cwd)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"opencode run timed out after {self.timeout:.0f}s", transient=True
            ) from exc
        except OSError as exc:
            raise ProviderError(f"could not start opencode: {exc}", transient=False) from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            transient = any(hint in stderr.lower() for hint in _TRANSIENT_HINTS)
            raise ProviderError(
                f"opencode exited with code {result.returncode}: {_tail(stderr)}",
                transient=transient,
            )

        return parse_events(result.stdout)


def flatten_messages(messages: Sequence[Message]) -> str:
    """Turn Alice's message list into the single prompt OpenCode accepts."""
    system = "\n\n".join(m.content for m in messages if m.role == "system").strip()
    turns = [message for message in messages if message.role != "system"]
    chunks: list[str] = []
    if system:
        chunks.append(f"<alice_instructions>\n{system}\n</alice_instructions>")
    conversation = "\n\n".join(
        f"{message.role}: {message.content}" for message in turns
    ).strip()
    if conversation:
        chunks.append(f"<conversation>\n{conversation}\n</conversation>")
    return "\n\n".join(chunks)


def parse_events(stdout: str) -> Completion:
    """Read the CLI's NDJSON event stream into a Completion."""
    texts: list[str] = []
    usage: tuple[int, int, int] | None = None
    cost = 0.0

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "text":
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                texts.append(value)
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            usage = (
                int(tokens.get("input") or 0),
                int(tokens.get("output") or 0),
                int(cache.get("read") or 0),
            )
            cost += float(part.get("cost") or 0.0)

    if usage is None:
        raise ProviderError(
            "opencode returned no step_finish metadata; cannot account for tokens",
            transient=False,
        )
    text = "\n".join(texts).strip()
    if not text:
        raise ProviderError("opencode returned no text", transient=False)
    return Completion(
        text=text,
        input_tokens=usage[0],
        output_tokens=usage[1],
        cached_tokens=usage[2],
        cost_usd=cost,
    )


def find_cli() -> str | None:
    """Locate the OpenCode CLI without hardcoding one machine's path."""
    for variable in CLI_ENV_VARS:
        candidate = os.environ.get(variable)
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))

    for name in ("opencode-cli", "opencode"):
        found = shutil.which(name)
        if found:
            return found

    appdata = os.environ.get("APPDATA")
    if appdata:
        root = Path(appdata) / "ai.opencode.desktop" / "cli"
        for candidate in _newest_first(root, "opencode-cli.exe"):
            return str(candidate)

    home = Path.home()
    for candidate in (
        home / ".opencode" / "bin" / "opencode",
        home / ".local" / "bin" / "opencode",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _newest_first(root: Path, filename: str) -> list[Path]:
    try:
        candidates = [path for path in root.glob(f"*/{filename}") if path.is_file()]
    except OSError:
        return []
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)


def _run_subprocess(
    command: list[str], prompt: str, timeout: float, cwd: str | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _tail(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[-limit:] if len(text) > limit else text
