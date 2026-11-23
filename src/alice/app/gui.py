"""Read-only desktop dashboard for Alice (Tkinter, no external UI deps)."""

from __future__ import annotations

import logging
import os
import queue
import threading
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..brain.chat import load_chat_history
from ..contracts.outputs import Suggestion
from ..core.db import connect, init_db
from ..core.logging import setup_logging
from ..inputs.files import FileReader
from ..inputs.redact import Redactor
from ..memory.core_file import CoreMemory
from ..memory.seed import ensure_seed_files
from ..memory.service import MemoryService
from ..models.budget import BudgetGuard, clear_pause, set_pause
from ..outputs.digest import DigestWriter
from ..outputs.notify import NullNotifier
from ..outputs.queue import SuggestionQueue
from .dashboard import Dashboard, load_dashboard
from .settings import AppSettings, load_settings

logger = logging.getLogger(__name__)

BG = "#f4f5f8"
CARD_BG = "#ffffff"
INK = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#1d4ed8"


class AliceWindow:
    def __init__(self, root: tk.Tk, settings: AppSettings) -> None:
        self.root = root
        self.settings = settings
        self.conn = connect(settings.db_path, create=False)
        self.memory = MemoryService(self.conn, settings.memory)
        self.dashboard: Dashboard | None = None
        self._after_id: str | None = None
        self._digest_text = ""
        self._chat_messages = ()
        self._chat_queue: queue.Queue = queue.Queue()
        self._chat_pending = False
        self._chat_poll_id: str | None = None
        self._chat_attachments: list[Path] = []
        self._pending_attachments: list[Path] = []

        self.root.title("Alice")
        self.root.geometry("1180x780")
        self.root.minsize(960, 640)
        self.root.configure(bg=BG)
        self._style()
        self._build()
        self.refresh()
        self._load_chat_history()
        self._after_id = self.root.after(5000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # -- layout ------------------------------------------------------------

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10), background=BG, foreground=INK)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD_BG, relief="flat")
        style.configure("CardTitle.TLabel", background=CARD_BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("CardValue.TLabel", background=CARD_BG, foreground=INK, font=("Segoe UI Semibold", 16))
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Segoe UI Semibold", 22))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7), font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.configure("TButton", padding=(10, 5))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Alice", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="  local secretary · read-only · suggest-only",
            style="Sub.TLabel",
        ).pack(side="left", pady=(8, 0))
        self.updated = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.updated, style="Sub.TLabel").pack(side="right")

        cards = ttk.Frame(outer)
        cards.pack(fill="x", pady=(0, 12))
        self.card_vars = {
            "events": tk.StringVar(value="0"),
            "eligible": tk.StringVar(value="0"),
            "queued": tk.StringVar(value="0"),
            "proposals": tk.StringVar(value="0"),
            "spend": tk.StringVar(value="$0.00"),
            "paused": tk.StringVar(value="no"),
        }
        labels = [
            ("events", "Events"),
            ("eligible", "Above gate"),
            ("queued", "Queued suggestions"),
            ("proposals", "Pending proposals"),
            ("spend", "Spend today"),
            ("paused", "Paused"),
        ]
        for index, (key, title) in enumerate(labels):
            card = ttk.Frame(cards, style="Card.TFrame", padding=12)
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 8) if index < 5 else 0)
            cards.columnconfigure(index, weight=1)
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(
                card, textvariable=self.card_vars[key], style="CardValue.TLabel"
            ).pack(anchor="w", pady=(4, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Button(controls, text="Refresh", command=self.refresh).pack(side="left")
        self.pause_button = ttk.Button(controls, text="Pause calls", command=self._toggle_pause)
        self.pause_button.pack(side="left", padx=6)
        ttk.Button(controls, text="Data folder", command=self._open_data).pack(side="left")
        ttk.Button(controls, text="Core memory", command=self._open_core).pack(side="left", padx=6)
        ttk.Button(controls, text="Digest folder", command=self._open_digests).pack(side="left")

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self._build_events_tab()
        self._build_suggestions_tab()
        self._build_chat_tab()
        self._build_memory_tab()
        self._build_digest_tab()

        self.footer = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.footer, style="Sub.TLabel").pack(
            anchor="w", pady=(8, 0)
        )

    def _tree(self, parent, columns: list[tuple[str, str, int]]) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame, columns=[c[0] for c in columns], show="headings", selectmode="browse"
        )
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w", stretch=key in ("text", "reason"))
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree.tag_configure("new", foreground=ACCENT)
        tree.tag_configure("escalated", foreground="#b91c1c")
        tree.tag_configure("done", foreground="#047857")
        tree.tag_configure("p1", foreground="#b91c1c")
        tree.tag_configure("p2", foreground="#b45309")
        return tree

    def _build_events_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Events")
        self.event_tree = self._tree(
            tab,
            [
                ("id", "ID", 50),
                ("ts", "Time", 155),
                ("source", "Source", 150),
                ("status", "Status", 80),
                ("salience", "Salience", 70),
                ("text", "Redacted text", 620),
            ],
        )

    def _build_suggestions_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Suggestions")
        panes = ttk.PanedWindow(tab, orient="vertical")
        panes.pack(fill="both", expand=True)
        top = ttk.Frame(panes)
        bottom = ttk.LabelFrame(panes, text="Selected suggestion", padding=8)
        panes.add(top, weight=3)
        panes.add(bottom, weight=2)

        self.suggestion_tree = self._tree(
            top,
            [
                ("id", "ID", 45),
                ("priority", "P", 35),
                ("status", "Status", 75),
                ("category", "Category", 90),
                ("confidence", "Conf", 55),
                ("text", "Suggestion", 460),
                ("reason", "Reason", 260),
            ],
        )
        self.suggestion_tree.bind("<<TreeviewSelect>>", self._show_suggestion_detail)

        self.suggestion_detail = tk.Text(
            bottom,
            wrap="word",
            height=9,
            font=("Segoe UI", 9),
            background=CARD_BG,
            foreground=INK,
            relief="flat",
            padx=8,
            pady=6,
            state="disabled",
        )
        self.suggestion_detail.pack(fill="both", expand=True)
        buttons = ttk.Frame(bottom)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Mark done", command=lambda: self._set_suggestion("done")).pack(side="left")
        ttk.Button(buttons, text="Dismiss", command=lambda: self._set_suggestion("dismissed")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Copy suggestion", command=self._copy_suggestion).pack(side="left")
        ttk.Button(buttons, text="Save answer as suggestion", command=self._save_chat_as_suggestion).pack(side="right")

    def _build_chat_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Chat")
        info = ttk.Frame(tab)
        info.pack(fill="x", pady=(0, 6))
        ttk.Label(
            info,
            text="Ask about events, suggestions, memory, or the digest. "
            "Alice is read-only: chat can advise, not act.",
            style="Sub.TLabel",
        ).pack(side="left")

        attach_row = ttk.Frame(tab)
        attach_row.pack(fill="x", pady=(0, 6))
        ttk.Button(
            attach_row, text="Attach file...", command=self._attach_chat_file
        ).pack(side="left")
        ttk.Button(
            attach_row, text="Remove file", command=self._clear_chat_attachments
        ).pack(side="left", padx=6)
        self.chat_attachment_var = tk.StringVar(value="No file attached")
        ttk.Label(
            attach_row, textvariable=self.chat_attachment_var, style="Sub.TLabel"
        ).pack(side="left", padx=6)

        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        self.chat_text = tk.Text(
            frame,
            wrap="word",
            font=("Segoe UI", 10),
            background=CARD_BG,
            foreground=INK,
            relief="flat",
            padx=12,
            pady=10,
            state="disabled",
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=scroll.set)
        self.chat_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.chat_text.tag_configure("user", foreground=ACCENT, font=("Segoe UI Semibold", 10))
        self.chat_text.tag_configure("assistant", foreground=INK)
        self.chat_text.tag_configure("meta", foreground=MUTED, font=("Segoe UI", 8))

        input_frame = ttk.Frame(tab)
        input_frame.pack(fill="x", pady=(8, 0))
        self.chat_input = tk.Text(
            input_frame,
            height=3,
            wrap="word",
            font=("Segoe UI", 10),
            background=CARD_BG,
            foreground=INK,
            relief="flat",
            padx=8,
            pady=6,
        )
        self.chat_input.pack(side="left", fill="both", expand=True)
        self.chat_input.bind("<Control-Return>", self._send_chat_event)
        buttons = ttk.Frame(input_frame)
        buttons.pack(side="right", fill="y", padx=(8, 0))
        self.chat_send_button = ttk.Button(buttons, text="Send", command=self._send_chat)
        self.chat_send_button.pack(fill="x")
        ttk.Button(buttons, text="Clear", command=self._clear_chat).pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Save answer", command=self._save_chat_as_suggestion).pack(
            fill="x", pady=(6, 0)
        )
        self.chat_status = tk.StringVar(value="Ready")
        ttk.Label(tab, textvariable=self.chat_status, style="Sub.TLabel").pack(
            anchor="w", pady=(6, 0)
        )

    def _build_memory_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Memory")

        info = ttk.Frame(tab)
        info.pack(fill="x", pady=(0, 8))
        self.core_label = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.core_label, style="Sub.TLabel").pack(
            side="left"
        )

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        approved_frame = ttk.LabelFrame(panes, text="Approved memory", padding=8)
        proposals_frame = ttk.LabelFrame(panes, text="Pending proposals", padding=8)
        panes.add(approved_frame, weight=1)
        panes.add(proposals_frame, weight=1)

        self.approved_tree = self._tree(
            approved_frame,
            [("key", "Key", 180), ("value", "Value", 350), ("version", "v", 40)],
        )
        self.proposal_tree = self._tree(
            proposals_frame,
            [("id", "ID", 45), ("key", "Key", 140), ("value", "Value", 260), ("reason", "Reason", 200)],
        )
        buttons = ttk.Frame(proposals_frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Approve selected", command=self._approve).pack(side="left")
        ttk.Button(buttons, text="Reject selected", command=self._reject).pack(side="left", padx=6)

    def _build_digest_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Digest")
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="Write digest now", command=self._write_digest).pack(side="left")
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side="left", padx=6)
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        self.digest_text = tk.Text(
            frame,
            wrap="word",
            font=("Cascadia Mono", 10),
            background=CARD_BG,
            foreground=INK,
            relief="flat",
            padx=12,
            pady=12,
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.digest_text.yview)
        self.digest_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.digest_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # -- data --------------------------------------------------------------

    def refresh(self) -> None:
        try:
            dashboard = load_dashboard(self.settings, self.conn)
        except Exception as exc:
            logger.exception("dashboard refresh failed")
            self.footer.set(f"Refresh failed: {exc}")
            return
        self.dashboard = dashboard
        total_events = sum(dashboard.status_counts.values())
        self.card_vars["events"].set(str(total_events))
        self.card_vars["eligible"].set(str(dashboard.new_above_gate))
        self.card_vars["queued"].set(str(dashboard.queued_suggestions))
        self.card_vars["proposals"].set(str(dashboard.pending_proposals))
        self.card_vars["spend"].set(
            f"${dashboard.spend_today:.2f} / ${dashboard.hard_cap:.2f}"
        )
        self.card_vars["paused"].set("yes" if dashboard.paused else "no")
        self.pause_button.configure(
            text="Resume calls" if dashboard.paused else "Pause calls"
        )

        self._fill_events(dashboard)
        self._fill_suggestions(dashboard)
        self._fill_memory(dashboard)
        self._set_digest(dashboard.digest_text)

        self.core_label.set(
            f"Core: {dashboard.core_path}  ·  ~{dashboard.core_tokens} tokens"
            + (f"  ·  {dashboard.core_error}" if dashboard.core_error else "")
        )
        self.updated.set(f"Updated {datetime.now():%H:%M:%S}")
        self.footer.set(
            f"schema v{dashboard.schema_version} · data {dashboard.data_dir}"
            + (
                f" · last consolidation {dashboard.last_consolidation}"
                if dashboard.last_consolidation
                else ""
            )
        )

    def _clear(self, tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _fill_events(self, dashboard: Dashboard) -> None:
        self._clear(self.event_tree)
        for event in dashboard.events:
            self.event_tree.insert(
                "",
                "end",
                values=(
                    event.id,
                    event.ts,
                    event.source,
                    event.status,
                    f"{event.salience:.2f}",
                    event.text,
                ),
                tags=(event.status,),
            )

    def _fill_suggestions(self, dashboard: Dashboard) -> None:
        selected = self._selected_suggestion_id()
        self._clear(self.suggestion_tree)
        for suggestion in dashboard.suggestions:
            item = self.suggestion_tree.insert(
                "",
                "end",
                values=(
                    suggestion.id,
                    suggestion.priority,
                    suggestion.status,
                    suggestion.category or "-",
                    f"{suggestion.confidence:.2f}" if suggestion.confidence is not None else "-",
                    suggestion.text,
                    suggestion.reason or "",
                ),
                tags=(f"p{suggestion.priority}",),
            )
            if selected is not None and suggestion.id == selected:
                self.suggestion_tree.selection_set(item)
                self.suggestion_tree.focus(item)
        self._show_suggestion_detail()

    def _suggestion_by_id(self, suggestion_id: int):
        if self.dashboard is None:
            return None
        for suggestion in self.dashboard.suggestions:
            if suggestion.id == suggestion_id:
                return suggestion
        return None

    def _selected_suggestion_id(self) -> int | None:
        selection = self.suggestion_tree.selection()
        if not selection:
            return None
        values = self.suggestion_tree.item(selection[0], "values")
        return int(values[0])

    def _show_suggestion_detail(self, _event=None) -> None:
        suggestion_id = self._selected_suggestion_id()
        suggestion = self._suggestion_by_id(suggestion_id) if suggestion_id else None
        if suggestion is None:
            text = "Select a suggestion to inspect its steps and evidence."
        else:
            meta = [f"P{suggestion.priority}", suggestion.status]
            if suggestion.category:
                meta.append(suggestion.category)
            if suggestion.confidence is not None:
                meta.append(f"confidence {suggestion.confidence:.2f}")
            lines = [
                " · ".join(meta),
                "",
                suggestion.text,
            ]
            if suggestion.reason:
                lines.extend(["", f"Why: {suggestion.reason}"])
            if suggestion.steps:
                lines.extend(["", "Steps:"])
                lines.extend(
                    f"{index}. {step}" for index, step in enumerate(suggestion.steps, 1)
                )
            if suggestion.evidence:
                lines.extend(["", f"Evidence: {suggestion.evidence}"])
            text = "\n".join(lines)
        self.suggestion_detail.configure(state="normal")
        self.suggestion_detail.delete("1.0", "end")
        self.suggestion_detail.insert("1.0", text)
        self.suggestion_detail.configure(state="disabled")

    def _set_suggestion(self, status: str) -> None:
        suggestion_id = self._selected_suggestion_id()
        if suggestion_id is None:
            messagebox.showinfo("Alice", "Select a suggestion first.")
            return
        try:
            SuggestionQueue(self.conn).set_status(suggestion_id, status)
        except Exception as exc:
            messagebox.showerror("Alice", str(exc))
            return
        self.footer.set(f"Suggestion {suggestion_id} marked {status}.")
        self.refresh()

    def _copy_suggestion(self) -> None:
        suggestion_id = self._selected_suggestion_id()
        suggestion = self._suggestion_by_id(suggestion_id) if suggestion_id else None
        if suggestion is None:
            messagebox.showinfo("Alice", "Select a suggestion first.")
            return
        lines = [suggestion.text]
        if suggestion.reason:
            lines.append(f"Why: {suggestion.reason}")
        for index, step in enumerate(suggestion.steps, 1):
            lines.append(f"{index}. {step}")
        if suggestion.evidence:
            lines.append(f"Evidence: {suggestion.evidence}")
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.footer.set(f"Suggestion {suggestion.id} copied to the clipboard.")

    # -- chat --------------------------------------------------------------

    def _load_chat_history(self) -> None:
        try:
            self._chat_messages = load_chat_history(self.conn, 200)
        except Exception:
            logger.exception("could not load chat history")
            self._chat_messages = ()
        self._render_chat()

    def _render_chat(self) -> None:
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        if not self._chat_messages:
            self.chat_text.insert(
                "end",
                "No chat yet. Ask about an event, a suggestion, or what Alice remembers.",
                "meta",
            )
        for message in self._chat_messages:
            who = "You" if message.role == "user" else "Alice"
            self.chat_text.insert("end", f"{who}\n", "user" if message.role == "user" else "assistant")
            self.chat_text.insert("end", message.content + "\n", "assistant")
            if message.error:
                self.chat_text.insert("end", f"[error: {message.error}]\n", "meta")
            if message.citations:
                labels = []
                for citation in message.citations:
                    label = citation.get("label") if isinstance(citation, dict) else None
                    if label:
                        labels.append(str(label))
                if labels:
                    self.chat_text.insert(
                        "end",
                        "Sources: " + " · ".join(labels[:6]) + "\n",
                        "meta",
                    )
            self.chat_text.insert("end", "\n")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    def _send_chat_event(self, _event=None) -> str:
        self._send_chat()
        return "break"

    def _attach_chat_file(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Attach a text file for Alice to read",
            filetypes=[
                (
                    "Text files",
                    "*.txt *.md *.log *.json *.jsonl *.csv *.py *.yaml *.yml",
                ),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        limit = self.settings.inputs.files.max_files
        if len(paths) > limit:
            messagebox.showinfo(
                "Alice", f"Attach at most {limit} files per chat question."
            )
            return
        self._chat_attachments = [Path(path) for path in paths]
        names = ", ".join(path.name for path in self._chat_attachments)
        self.chat_attachment_var.set(f"Attached: {names}")

    def _clear_chat_attachments(self) -> None:
        self._chat_attachments = []
        self._pending_attachments = []
        self.chat_attachment_var.set("No file attached")

    def _send_chat(self) -> None:
        if self._chat_pending:
            return
        question = self.chat_input.get("1.0", "end").strip()
        if not question:
            self.chat_status.set("Type a question first.")
            return
        self.chat_input.delete("1.0", "end")
        self._chat_pending = True
        self._pending_attachments = list(self._chat_attachments)
        self.chat_send_button.configure(state="disabled")
        if self._pending_attachments:
            self.chat_status.set(
                "Alice is reading the attached file and thinking; this is a budgeted model call..."
            )
        else:
            self.chat_status.set("Alice is thinking; this is a budgeted model call...")
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", "You\n", "user")
        self.chat_text.insert("end", question + "\n\n", "assistant")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

        def worker() -> None:
            chat_conn = None
            try:
                from .bootstrap import build_chat, build_memory, build_models

                redactor = Redactor(self.settings.inputs.redaction.patterns)
                reader = FileReader(
                    self.settings.inputs.files,
                    sanitize=lambda value: redactor.redact(value).text,
                )
                files = (
                    reader.read_many(self._pending_attachments)
                    if self._pending_attachments
                    else ()
                )
                attachments = tuple((str(item.path), item.text) for item in files)

                chat_conn = connect(self.settings.db_path, create=False)
                router = build_models(self.settings, chat_conn)
                memory = build_memory(self.settings, chat_conn)
                service = build_chat(self.settings, chat_conn, router, memory)
                result = service.ask(question, attachments=attachments)
            except Exception as exc:
                self._chat_queue.put((None, exc))
            else:
                self._chat_queue.put((result, None))
            finally:
                if chat_conn is not None:
                    chat_conn.close()

        threading.Thread(target=worker, name="alice-chat", daemon=True).start()
        self._chat_poll_id = self.root.after(250, self._poll_chat)

    def _poll_chat(self) -> None:
        try:
            result, error = self._chat_queue.get_nowait()
        except queue.Empty:
            self._chat_poll_id = self.root.after(250, self._poll_chat)
            return
        self._chat_pending = False
        self.chat_send_button.configure(state="normal")
        if error is not None:
            self.chat_status.set(f"Chat failed: {error}")
        else:
            self.chat_status.set(
                f"Answered · {result.input_tokens:,} in / "
                f"{result.output_tokens:,} out · ${result.cost_usd:.4f}"
            )
            self._clear_chat_attachments()
        self._load_chat_history()
        self.refresh()

    def _clear_chat(self) -> None:
        if not messagebox.askyesno("Alice", "Clear the local chat transcript?"):
            return
        self.conn.execute("DELETE FROM chat_messages")
        self.conn.commit()
        self._chat_messages = ()
        self._clear_chat_attachments()
        self._render_chat()
        self.chat_status.set("Chat cleared.")

    def _save_chat_as_suggestion(self) -> None:
        assistant = next(
            (
                message
                for message in reversed(self._chat_messages)
                if message.role == "assistant" and not message.error
            ),
            None,
        )
        if assistant is None:
            messagebox.showinfo("Alice", "There is no assistant answer to save yet.")
            return
        try:
            suggestion_id = SuggestionQueue(self.conn).submit(
                Suggestion(
                    text=assistant.content.strip()[:2000],
                    priority=2,
                    reason="Saved from chat",
                    category="workflow",
                )
            )
        except Exception as exc:
            messagebox.showerror("Alice", str(exc))
            return
        self.footer.set(f"Saved chat answer as suggestion {suggestion_id}.")
        self.chat_status.set(f"Saved as suggestion {suggestion_id}.")
        self.refresh()

    def _fill_memory(self, dashboard: Dashboard) -> None:
        self._clear(self.approved_tree)
        for row in dashboard.approved:
            self.approved_tree.insert(
                "", "end", values=(row.key, row.value, row.version)
            )
        self._clear(self.proposal_tree)
        for row in dashboard.proposals:
            self.proposal_tree.insert(
                "", "end", values=(row.id, row.key, row.value, row.reason or "")
            )

    def _set_digest(self, text: str) -> None:
        if text == self._digest_text:
            return
        self._digest_text = text
        self.digest_text.configure(state="normal")
        self.digest_text.delete("1.0", "end")
        self.digest_text.insert("1.0", text or "No digest yet. It is written on a run, or use the button above.")
        self.digest_text.configure(state="disabled")

    # -- actions -----------------------------------------------------------

    def _tick(self) -> None:
        self.refresh()
        self._after_id = self.root.after(5000, self._tick)

    def _toggle_pause(self) -> None:
        if self.dashboard and self.dashboard.paused:
            clear_pause(self.settings.data_dir)
            self.footer.set("Model calls resumed.")
        else:
            set_pause(self.settings.data_dir, "gui")
            self.footer.set("Model calls paused.")
        self.refresh()

    def _selected_proposal(self) -> int | None:
        selection = self.proposal_tree.selection()
        if not selection:
            messagebox.showinfo("Alice", "Select a pending proposal first.")
            return None
        values = self.proposal_tree.item(selection[0], "values")
        return int(values[0])

    def _approve(self) -> None:
        proposal_id = self._selected_proposal()
        if proposal_id is None:
            return
        try:
            entry = self.memory.approve(proposal_id)
        except Exception as exc:
            messagebox.showerror("Alice", str(exc))
            return
        self.footer.set(f"Approved {entry.key} v{entry.version}.")
        self.refresh()

    def _reject(self) -> None:
        proposal_id = self._selected_proposal()
        if proposal_id is None:
            return
        try:
            self.memory.reject(proposal_id)
        except Exception as exc:
            messagebox.showerror("Alice", str(exc))
            return
        self.footer.set(f"Rejected proposal {proposal_id}.")
        self.refresh()

    def _write_digest(self) -> None:
        try:
            guard = BudgetGuard(
                self.conn, self.settings.models.budget, self.settings.data_dir
            )

            def snapshot():
                status = guard.status()
                paused = status.paused
                from ..contracts.outputs import BudgetSnapshot

                return BudgetSnapshot(
                    paused=(f"{paused.since} ({paused.reason})" if paused else None),
                    calls_today=status.calls_today,
                    spend_today=status.spend_today,
                    soft_cap=status.soft_cap,
                    hard_cap=status.hard_cap,
                    alerts=guard.alerts_since(_day_start()),
                )

            writer = DigestWriter(
                self.conn, self.settings.data_dir, self.settings.outputs.notify, snapshot
            )
            result = writer.write(notifier=NullNotifier())
        except Exception as exc:
            messagebox.showerror("Alice", str(exc))
            return
        self.footer.set(f"Digest written: {result.path}")
        self.refresh()

    def _open_data(self) -> None:
        self._open(self.settings.data_dir)

    def _open_core(self) -> None:
        self._open(self.settings.memory.core_memory_file)

    def _open_digests(self) -> None:
        self._open(self.settings.data_dir / "digests")

    def _open(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Alice", f"Cannot open {path}: {exc}")

    def close(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        if self._chat_poll_id is not None:
            self.root.after_cancel(self._chat_poll_id)
        try:
            self.conn.close()
        finally:
            self.root.destroy()


def _day_start() -> str:
    from ..models.budget import day_start

    return day_start()


def run_gui(config_path: Path | str | None = None, data_dir: Path | str | None = None) -> None:
    config_path = config_path or os.environ.get("ALICE_CONFIG")
    settings = load_settings(config_path, data_dir=data_dir)
    for directory in (settings.data_dir, settings.log_dir, settings.raw_dir):
        directory.mkdir(parents=True, exist_ok=True)
    setup_logging(settings.log_dir)
    init_db(settings.db_path)
    ensure_seed_files(settings.seed_dir)
    CoreMemory(settings.memory.core_memory_file, settings.memory.hard_max_tokens).ensure()

    root = tk.Tk()
    AliceWindow(root, settings)
    root.mainloop()


def main() -> None:
    try:
        run_gui()
    except Exception as exc:
        logger.exception("GUI failed to start")
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Alice", f"{type(exc).__name__}: {exc}")
            root.destroy()
        except Exception:
            raise
