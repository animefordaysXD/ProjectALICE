"""Read-only tool registry: time, web, system, files, calendar, email.

Only chat/exploration use these tools. They cannot write to the machine, send
mail, delete mail, or run commands. Mutating fixes stay in the separate,
typed-confirmation action runner.
"""

from __future__ import annotations

import email
import email.header
import imaplib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping

from ..contracts.tools import Tool, ToolResult, ToolSpec
from ..inputs.files import FileAccessError, FileReader
from ..inputs.redact import Redactor
from .settings import AppSettings
from .system_info import snapshot as system_snapshot
from .tools_config import ToolsConfig

logger = logging.getLogger(__name__)

MAX_TOOL_TEXT = 12_000


def build_tools(settings: AppSettings, conn) -> "ToolRegistry":
    redactor = Redactor(settings.inputs.redaction.patterns)
    reader = FileReader(
        settings.inputs.files,
        sanitize=lambda text: redactor.redact(text).text,
    )
    return ToolRegistry(
        conn,
        config=settings.tools,
        data_dir=settings.data_dir,
        file_reader=reader,
        sanitize=lambda text: redactor.redact(text).text,
    )


class ToolRegistry:
    def __init__(
        self,
        conn,
        *,
        config: ToolsConfig,
        data_dir: Path,
        file_reader: FileReader,
        sanitize=None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.data_dir = Path(data_dir)
        self.sanitize = sanitize
        self.tools: dict[str, Tool] = {
            "time.now": TimeTool(),
            "system.snapshot": SystemTool(),
            "web.fetch": WebFetchTool(config),
            "file.read": FileReadTool(file_reader),
            "file.list": FileListTool(file_reader),
            "calendar.upcoming": CalendarTool(config),
            "email.recent": EmailTool(config),
        }

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for tool in self.tools.values())

    def execute(self, name: str, args: Mapping[str, Any] | None = None) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(False, f"Unknown tool: {name}")
        payload = dict(args or {})
        started = time.monotonic()
        try:
            result = tool.run(payload)
        except Exception as exc:
            logger.warning("tool %s failed: %s", name, exc)
            result = ToolResult(False, f"{type(exc).__name__}: {exc}")
        if self.sanitize is not None:
            result = ToolResult(result.ok, self.sanitize(result.text), result.data)
        duration = int((time.monotonic() - started) * 1000)
        self._record(name, payload, result, duration)
        return result

    def _record(
        self,
        name: str,
        args: Mapping[str, Any],
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        try:
            self.conn.execute(
                "INSERT INTO tool_calls "
                "(created, tool, args, ok, summary, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _utc_now(),
                    name,
                    json.dumps(dict(args), ensure_ascii=False),
                    1 if result.ok else 0,
                    _short(result.text, 500),
                    duration_ms,
                ),
            )
            self.conn.commit()
        except Exception:
            logger.exception("could not record tool call %s", name)


class TimeTool:
    spec = ToolSpec("time.now", "Current local and UTC date and time.")

    def run(self, args: Mapping[str, Any]) -> ToolResult:
        now = datetime.now().astimezone()
        utc = now.astimezone(timezone.utc)
        text = (
            f"Local: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"UTC:   {utc.strftime('%Y-%m-%d %H:%M:%SZ')}\n"
            f"Timezone: {now.tzname()}"
        )
        return ToolResult(True, text, {"local": now.isoformat(), "utc": utc.isoformat()})


class SystemTool:
    spec = ToolSpec("system.snapshot", "Read CPU, memory, disk, uptime, and process count.")

    def run(self, args: Mapping[str, Any]) -> ToolResult:
        data = system_snapshot()
        text = (
            f"CPU: {data['cpu_percent']}% across {data['cpu_count']} logical cores\n"
            f"Memory: {data['memory_used_gb']} / {data['memory_total_gb']} GB used\n"
            f"Disk {Path.home().anchor or '/'}: {data['disk_used_gb']} / "
            f"{data['disk_total_gb']} GB used ({data['disk_free_gb']} GB free)\n"
            f"Processes: {data['process_count']}\n"
            f"Uptime: {data['uptime_hours']} hours"
        )
        return ToolResult(True, text, data)


class WebFetchTool:
    spec = ToolSpec(
        "web.fetch",
        "Fetch a read-only page from an allowed domain and return text and links.",
    )

    def __init__(self, config: ToolsConfig) -> None:
        self.config = config

    def run(self, args: Mapping[str, Any]) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult(False, "web.fetch needs a url")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ToolResult(False, "Only http(s) URLs are allowed")
        if not self._allowed(parsed.hostname or ""):
            return ToolResult(
                False,
                f"Domain not allowed. Allowed: {', '.join(self.config.web_domains)}",
            )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Alice/0.1 (local read-only secretary)",
                "Accept": "text/html,text/plain",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.web_timeout_seconds
            ) as response:
                final_url = response.geturl()
                final_host = urllib.parse.urlparse(final_url).hostname or ""
                if not self._allowed(final_host):
                    return ToolResult(False, "Redirect left the allowed domains")
                raw = response.read(self.config.web_max_bytes)
                charset = response.headers.get_content_charset() or "utf-8"
        except Exception as exc:
            return ToolResult(False, f"Fetch failed: {type(exc).__name__}: {exc}")

        try:
            html = raw.decode(charset, errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")
        parser = _PageParser(final_url, self.config.web_domains)
        try:
            parser.feed(html)
        except Exception:
            pass
        title = _clean_text(parser.title).strip() or final_url
        text = _clean_text(" ".join(parser.text_parts))[:MAX_TOOL_TEXT]
        links = parser.links[:40]
        header = f"{title}\n{final_url}\n\n"
        if links:
            header += "Links:\n" + "\n".join(f"- {link}" for link in links[:20]) + "\n\n"
        return ToolResult(
            True,
            header + text,
            {"url": final_url, "title": title, "links": links},
        )

    def _allowed(self, host: str) -> bool:
        host = host.casefold().rstrip(".")
        if any(
            host == domain or host.endswith("." + domain)
            for domain in self.config.web_domains
        ):
            return True
        if not self.config.web_allow_public:
            return False
        return _public_host(host)


class FileReadTool:
    spec = ToolSpec(
        "file.read",
        "Read a text file under files.roots. No writes.",
    )

    def __init__(self, reader: FileReader) -> None:
        self.reader = reader

    def run(self, args: Mapping[str, Any]) -> ToolResult:
        path = str(args.get("path") or "").strip()
        if not path:
            return ToolResult(False, "file.read needs a path")
        try:
            result = self.reader.read(path)
        except FileAccessError as exc:
            return ToolResult(False, str(exc))
        text = result.text[:MAX_TOOL_TEXT]
        return ToolResult(
            True,
            f"{result.path}\n\n{text}",
            {
                "path": str(result.path),
                "truncated": result.truncated,
                "size": result.size,
            },
        )


class FileListTool:
    spec = ToolSpec(
        "file.list",
        "List a directory under files.roots. No reads or writes.",
    )

    def __init__(self, reader: FileReader) -> None:
        self.reader = reader

    def run(self, args: Mapping[str, Any]) -> ToolResult:
        path = str(args.get("path") or "").strip()
        if not path:
            return ToolResult(False, "file.list needs a path")
        try:
            rows = self.reader.list_dir(path, limit=int(args.get("limit", 100)))
        except (FileAccessError, ValueError) as exc:
            return ToolResult(False, str(exc))
        lines = [f"{name}  {size:>10,} B  {modified}" for name, size, modified in rows]
        return ToolResult(
            True,
            "\n".join(lines) or "The directory is empty.",
            {"count": len(rows)},
        )


def ensure_calendar_file(path: Path | None) -> bool:
    """Create a valid empty .ics so the calendar tool has a clear target."""
    if path is None or path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Alice//Local secretary//EN\n"
        "CALSCALE:GREGORIAN\n"
        "END:VCALENDAR\n",
        encoding="utf-8",
    )
    return True


class CalendarTool:
    spec = ToolSpec(
        "calendar.upcoming",
        "Read upcoming events from the configured local .ics calendar.",
    )

    def __init__(self, config: ToolsConfig) -> None:
        self.config = config

    def run(self, args: Mapping[str, Any]) -> ToolResult:
        path = self.config.calendar_file
        if path is None or not path.is_file():
            return ToolResult(
                False,
                f"Calendar file not found: {path}. Export an .ics file or configure tools.calendar_file.",
            )
        try:
            events = _read_ics(path, int(args.get("days", 7)))
        except OSError as exc:
            return ToolResult(False, f"Cannot read calendar: {exc}")
        if not events:
            return ToolResult(True, "No upcoming events in the calendar window.")
        lines = [
            f"{start:%Y-%m-%d %H:%M}  {summary}"
            + (f"  ({location})" if location else "")
            for start, summary, location in events
        ]
        return ToolResult(True, "\n".join(lines), {"count": len(events)})


class EmailTool:
    spec = ToolSpec(
        "email.recent",
        "Read recent email headers over read-only IMAP. No bodies, send, or delete.",
    )

    def __init__(self, config: ToolsConfig) -> None:
        self.config = config

    def run(self, args: Mapping[str, Any]) -> ToolResult:
        if not self.config.email_enabled:
            return ToolResult(
                False,
                "Email is disabled. Set tools.email_enabled: true when credentials are ready.",
            )
        user = os.environ.get(self.config.email_user_env, "").strip()
        password = os.environ.get(self.config.email_password_env, "").strip()
        if not user or not password:
            return ToolResult(
                False,
                "Email credentials are not configured. Set "
                f"{self.config.email_user_env} and {self.config.email_password_env}.",
            )
        limit = min(int(args.get("limit", self.config.email_max_messages)), 20)
        try:
            client = imaplib.IMAP4_SSL(
                self.config.email_host,
                self.config.email_port,
                timeout=15,
            )
            try:
                client.login(user, password)
                client.select("INBOX", readonly=True)
                status, data = client.search(None, "ALL")
                if status != "OK" or not data:
                    return ToolResult(False, "IMAP search failed")
                ids = data[0].split()[-limit:]
                lines: list[str] = []
                for message_id in reversed(ids):
                    status, fetched = client.fetch(
                        message_id,
                        "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])",
                    )
                    if status != "OK" or not fetched:
                        continue
                    raw = fetched[0][1]
                    message = email.message_from_bytes(raw)
                    subject = _decode_header(message.get("Subject", ""))
                    sender = _decode_header(message.get("From", ""))
                    date = message.get("Date", "")
                    try:
                        when = parsedate_to_datetime(date).strftime("%Y-%m-%d %H:%M")
                    except (TypeError, ValueError):
                        when = date
                    lines.append(f"- {when} | {sender} | {subject}")
                return ToolResult(True, "\n".join(lines) or "No messages found.")
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
        except Exception as exc:
            return ToolResult(False, f"IMAP failed: {type(exc).__name__}: {exc}")


class _PageParser(HTMLParser):
    def __init__(self, base_url: str, domains: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.domains = domains
        self.title = ""
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if tag in ("script", "style", "noscript"):
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                absolute = urllib.parse.urljoin(self.base_url, href)
                parsed = urllib.parse.urlparse(absolute)
                host = (parsed.hostname or "").casefold()
                if parsed.scheme in ("http", "https") and any(
                    host == domain or host.endswith("." + domain)
                    for domain in self.domains
                ):
                    if absolute not in self.links:
                        self.links.append(absolute)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip:
            return
        if data.strip():
            self.text_parts.append(data)


def _public_host(host: str) -> bool:
    """Allow a public host only; never loopback, private, or reserved IPs."""
    if not host or host in ("localhost", "localhost.localdomain") or host.endswith(
        (".local", ".internal", ".home", ".lan")
    ):
        return False
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except OSError:
        return False
    if not addresses:
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return False
    return True


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _short(value: str, limit: int) -> str:
    clean = _clean_text(value)
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value)
    out: list[str] = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            out.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(raw)
    return "".join(out).strip()


def _read_ics(path: Path, days: int) -> list[tuple[datetime, str, str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)
    now = datetime.now().astimezone()
    end = now + timedelta(days=max(1, min(days, 365)))
    events: list[tuple[datetime, str, str]] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.split(";", 1)[0].upper()
            fields[key] = value.strip()
        start = _parse_ics_datetime(fields.get("DTSTART", ""))
        if start is None or start < now or start > end:
            continue
        events.append(
            (
                start,
                fields.get("SUMMARY", "(untitled)"),
                fields.get("LOCATION", ""),
            )
        )
    return sorted(events)


def _parse_ics_datetime(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%dT%H%MZ", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt.endswith("Z"):
                return parsed.replace(tzinfo=timezone.utc).astimezone()
            return parsed.astimezone()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None
