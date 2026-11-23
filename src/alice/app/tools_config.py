"""Configuration for read-only web, system, calendar, email, and file tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import (
    ConfigError,
    as_mapping,
    get_boolean,
    get_integer,
    get_string,
    reject_unknown,
    resolve_path,
)


@dataclass(frozen=True)
class ToolsConfig:
    web_domains: tuple[str, ...] = ("wikipedia.org",)
    web_allow_public: bool = False
    web_max_bytes: int = 500_000
    web_timeout_seconds: int = 20
    calendar_file: Path | None = None
    email_enabled: bool = False
    email_host: str = "imap.naver.com"
    email_port: int = 993
    email_user_env: str = "ALICE_NAVER_USER"
    email_password_env: str = "ALICE_NAVER_APP_PASSWORD"
    email_max_messages: int = 5


def parse_tools(raw: Any, data_dir: Path) -> ToolsConfig:
    mapping = as_mapping(raw or {}, "tools")
    reject_unknown(
        mapping,
        {
            "web_domains",
            "web_allow_public",
            "web_max_bytes",
            "web_timeout_seconds",
            "calendar_file",
            "email_enabled",
            "email_host",
            "email_port",
            "email_user_env",
            "email_password_env",
            "email_max_messages",
        },
        "tools",
    )

    domains_raw = mapping.get("web_domains", ["wikipedia.org"])
    if not isinstance(domains_raw, list) or not domains_raw:
        raise ConfigError("tools.web_domains must be a non-empty list")
    domains: list[str] = []
    for index, value in enumerate(domains_raw):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"tools.web_domains[{index}]: expected a host name")
        domain = value.strip().casefold().lstrip("*.")
        if "/" in domain or ":" in domain:
            raise ConfigError(
                f"tools.web_domains[{index}]: expected a host suffix such as wikipedia.org"
            )
        domains.append(domain)

    allow_public = get_boolean(
        mapping, "web_allow_public", "tools", default=False
    )

    max_bytes = get_integer(mapping, "web_max_bytes", "tools", default=500_000)
    if not 1_000 <= max_bytes <= 5_000_000:
        raise ConfigError("tools.web_max_bytes must be between 1000 and 5000000")
    timeout = get_integer(
        mapping, "web_timeout_seconds", "tools", default=20
    )
    if not 1 <= timeout <= 120:
        raise ConfigError("tools.web_timeout_seconds must be between 1 and 120")

    calendar_raw = mapping.get("calendar_file", "calendar.ics")
    if not isinstance(calendar_raw, str) or not calendar_raw.strip():
        raise ConfigError("tools.calendar_file must be a path string")
    calendar_file = resolve_path(Path(calendar_raw).expanduser(), data_dir)

    email_enabled = get_boolean(
        mapping, "email_enabled", "tools", default=False
    )
    email_host = get_string(
        mapping, "email_host", "tools", default="imap.naver.com"
    )
    email_port = get_integer(mapping, "email_port", "tools", default=993)
    if not 1 <= email_port <= 65535:
        raise ConfigError("tools.email_port must be between 1 and 65535")
    user_env = get_string(
        mapping, "email_user_env", "tools", default="ALICE_NAVER_USER"
    )
    password_env = get_string(
        mapping,
        "email_password_env",
        "tools",
        default="ALICE_NAVER_APP_PASSWORD",
    )
    max_messages = get_integer(
        mapping, "email_max_messages", "tools", default=5
    )
    if not 1 <= max_messages <= 50:
        raise ConfigError("tools.email_max_messages must be between 1 and 50")

    return ToolsConfig(
        web_domains=tuple(domains),
        web_allow_public=allow_public,
        web_max_bytes=max_bytes,
        web_timeout_seconds=timeout,
        calendar_file=calendar_file,
        email_enabled=email_enabled,
        email_host=email_host,
        email_port=email_port,
        email_user_env=user_env,
        email_password_env=password_env,
        email_max_messages=max_messages,
    )
