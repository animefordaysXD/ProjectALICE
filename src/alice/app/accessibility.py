"""Bounded Windows UI Automation context.

An accessibility-tree complement to OCR. It reads control names, values, and
document text from the foreground window, with hard node/depth/character caps.
Everywhere-inspired idea: structured UI context is often better than pixels.
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_NODES = 160
MAX_DEPTH = 7
MAX_CHARS = 1800
TYPE_SKIP = {
    "Pane",
    "Group",
    "Custom",
    "Button",
    "ToolBar",
    "Slider",
    "ScrollBar",
    "Separator",
    "MenuItem",
    "MenuBar",
    "TitleBar",
    "Image",
}
CONTENT_TYPES = {
    "Document",
    "Edit",
    "Text",
    "ListItem",
    "DataItem",
    "TreeItem",
    "Hyperlink",
    "TabItem",
    "ComboBox",
    "CheckBox",
    "RadioButton",
    "Window",
}


@dataclass(frozen=True)
class AccessibilityContext:
    text: str
    focused: str
    node_count: int
    process_id: int


def capture_accessibility() -> AccessibilityContext | None:
    if sys.platform != "win32":
        return None
    try:
        import uiautomation as auto
    except Exception:
        logger.debug("uiautomation is not available")
        return None
    try:
        auto.SetGlobalSearchTimeout(0.8)
        root = auto.GetForegroundControl()
        if root is None:
            return None
        focused = None
        try:
            focused = auto.GetFocusedControl()
        except Exception:
            focused = None
        lines: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        queue = deque([(root, 0)])
        nodes = 0
        focused_text = ""
        while queue and nodes < MAX_NODES and sum(len(x) for x in lines) < MAX_CHARS:
            control, depth = queue.popleft()
            nodes += 1
            try:
                name = _clean(control.Name)
                ctype = str(control.ControlTypeName or "").replace("Control", "")
                automation_id = _clean(control.AutomationId)
                value = _value(control)
                text = _document_text(control, ctype)
                is_focused = focused is not None and _same(control, focused)
                if is_focused:
                    focused_text = " | ".join(
                        part for part in (ctype, name, value, text) if part
                    )[:400]
                key = (ctype, name, automation_id or value)
                if key not in seen and (name or value or text):
                    seen.add(key)
                    keep = (
                        ctype in CONTENT_TYPES
                        or bool(value)
                        or is_focused
                        or (ctype not in TYPE_SKIP and bool(text))
                    )
                    if keep:
                        parts = [ctype or "Control"]
                        if name:
                            parts.append(f'"{name[:180]}"')
                        if value and value != name:
                            parts.append(f"value={value[:180]!r}")
                        if text and text != name:
                            parts.append(f"text={text[:300]!r}")
                        if is_focused:
                            parts.append("focused")
                        lines.append(" ".join(parts))
                if depth < MAX_DEPTH:
                    try:
                        for child in control.GetChildren():
                            queue.append((child, depth + 1))
                    except Exception:
                        pass
            except Exception:
                continue
        body = "\n".join(lines)
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS]
        if not body and not focused_text:
            return None
        return AccessibilityContext(
            text=body,
            focused=focused_text,
            node_count=nodes,
            process_id=int(getattr(root, "ProcessId", 0) or 0),
        )
    except Exception:
        logger.exception("accessibility capture failed")
        return None


def _value(control) -> str:
    try:
        return _clean(control.GetValuePattern().Value)
    except Exception:
        return ""


def _document_text(control, ctype: str) -> str:
    if ctype not in ("Document", "Edit", "Text"):
        return ""
    try:
        return _clean(control.GetTextPattern().DocumentRange.GetText(600))
    except Exception:
        return ""


def _same(control, other) -> bool:
    try:
        return bool(control == other)
    except Exception:
        return False


def _clean(value: object) -> str:
    text = str(value or "")
    return " ".join(text.split())[:500]
