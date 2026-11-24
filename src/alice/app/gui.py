"""Read-only desktop dashboard for Alice (Tkinter, no external UI deps)."""

from __future__ import annotations

import logging
import os
import queue
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from ..brain.chat import load_chat_history
from ..contracts.outputs import Suggestion
from ..core.actions import ActionRow, ActionRunner
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

BG = "#080b14"
SIDEBAR = "#05070d"
SIDEBAR_ACTIVE = "#141a2e"
SIDEBAR_TEXT = "#8b95b5"
SIDEBAR_ACTIVE_TEXT = "#67e8f9"
CARD_BG = "#101625"
INK = "#e5e7eb"
MUTED = "#94a3b8"
BORDER = "#24304a"
ACCENT = "#22d3ee"
ACCENT_SOFT = "#18233b"
CYAN = "#22d3ee"
PINK = "#f472b6"
VIOLET = "#8b5cf6"
GREEN = "#34d399"
AMBER = "#fbbf24"
RED = "#fb7185"
CHAT_BG = "#0b1020"


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
        self._fix_queue: queue.Queue = queue.Queue()
        self._fix_pending = False
        self._fix_poll_id: str | None = None

        self.root.title("Alice · AI Secretary")
        self.root.minsize(1040, 660)
        self.root.configure(bg=BG)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(1280, max(1040, screen_w - 120))
        height = min(820, max(660, screen_h - 120))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
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
        style.configure("Content.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD_BG, relief="flat")
        style.configure(
            "CardTitle.TLabel",
            background=CARD_BG,
            foreground=MUTED,
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "CardValue.TLabel",
            background=CARD_BG,
            foreground=INK,
            font=("Segoe UI Semibold", 15),
        )
        style.configure(
            "PageTitle.TLabel",
            background=BG,
            foreground=INK,
            font=("Segoe UI Semibold", 18),
        )
        style.configure(
            "PageSub.TLabel",
            background=BG,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Greeting.TLabel",
            background=BG,
            foreground=INK,
            font=("Segoe UI Semibold", 20),
        )
        style.configure(
            "Sub.TLabel",
            background=BG,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Primary.TButton",
            padding=(12, 6),
            background="#7c3aed",
            foreground="white",
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#6d28d9"), ("pressed", "#5b21b6")],
            foreground=[("disabled", "#c4b5fd")],
        )
        style.configure(
            "Ghost.TButton",
            padding=(10, 5),
            background="#151b2b",
            foreground="#cbd5e1",
            bordercolor="#2b3650",
            borderwidth=1,
        )
        style.map(
            "Ghost.TButton",
            background=[("active", "#1e2740")],
            foreground=[("active", "#f8fafc")],
        )
        style.configure(
            "TLabelframe",
            background=BG,
            foreground=MUTED,
            bordercolor=BORDER,
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            background=BG,
            foreground=MUTED,
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "TScrollbar",
            background="#151b2b",
            troughcolor=BG,
            arrowcolor=MUTED,
            bordercolor=BG,
        )
        style.configure("TPanedwindow", background=BG)
        style.configure("Sash", background=BORDER)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7), font=("Segoe UI", 10))
        style.configure(
            "Treeview",
            rowheight=26,
            font=("Segoe UI", 9),
            background=CARD_BG,
            fieldbackground=CARD_BG,
            foreground=INK,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI Semibold", 9),
            background="#0f172a",
            foreground=MUTED,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", "#1e2a46")],
            foreground=[("selected", "#f8fafc")],
        )
        style.configure("TButton", padding=(10, 5))

    def _build(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        sidebar = tk.Frame(self.root, bg=SIDEBAR, width=232)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)

        main = tk.Frame(self.root, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(3, weight=1)
        main.grid_columnconfigure(0, weight=1)
        neon = tk.Frame(main, bg=ACCENT, height=2)
        neon.grid(row=0, column=0, sticky="ew")
        tk.Frame(main, bg=PINK, height=1).grid(row=0, column=0, sticky="sew")

        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self.page_host = tk.Frame(main, bg=BG)
        self.page_host.grid(row=3, column=0, sticky="nsew", padx=24, pady=(0, 6))
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)

        self._build_sidebar(sidebar)
        self._build_topbar(main)
        self._build_cards(main)
        self._build_dashboard_tab()
        self._build_events_tab()
        self._build_suggestions_tab()
        self._build_chat_tab()
        self._build_memory_tab()
        self._build_growth_tab()
        self._build_digest_tab()
        self._build_tools_tab()
        self._build_system_tab()
        self._build_footer(main)
        self._show_page("dashboard")

    def _build_sidebar(self, parent: tk.Frame) -> None:
        brand = tk.Frame(parent, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(22, 14))
        avatar = tk.Canvas(
            brand, width=44, height=44, bg=SIDEBAR, highlightthickness=0
        )
        avatar.pack(side="left")
        avatar.create_oval(2, 2, 42, 42, fill=VIOLET, outline=ACCENT, width=2)
        avatar.create_text(22, 22, text="A", fill="white", font=("Segoe UI Semibold", 20))
        text = tk.Frame(brand, bg=SIDEBAR)
        text.pack(side="left", padx=(10, 0))
        tk.Label(
            text,
            text="ALICE",
            bg=SIDEBAR,
            fg=SIDEBAR_ACTIVE_TEXT,
            font=("Segoe UI Semibold", 16),
        ).pack(anchor="w")
        tk.Label(
            text,
            text="AI secretary · local only",
            bg=SIDEBAR,
            fg=SIDEBAR_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor="w")

        tk.Frame(parent, bg="#1e293b", height=1).pack(fill="x", padx=18, pady=(4, 10))

        def section(text: str) -> None:
            tk.Label(
                parent,
                text=text,
                bg=SIDEBAR,
                fg="#64748b",
                font=("Segoe UI Semibold", 8),
            ).pack(anchor="w", padx=22, pady=(6, 3))

        def nav_button(name: str, label: str) -> None:
            button = tk.Button(
                parent,
                text=label,
                command=lambda page=name: self._show_page(page),
                bg=SIDEBAR,
                fg=SIDEBAR_TEXT,
                activebackground=SIDEBAR_ACTIVE,
                activeforeground=SIDEBAR_ACTIVE_TEXT,
                relief="flat",
                bd=0,
                anchor="w",
                padx=22,
                pady=7,
                font=("Segoe UI", 10),
                cursor="hand2",
            )
            button.pack(fill="x", pady=1)
            self.nav_buttons[name] = button

        section("OVERVIEW")
        for name, label in (
            ("dashboard", "◉   Dashboard"),
            ("events", "▤   Events"),
            ("suggestions", "✦   Suggestions"),
        ):
            nav_button(name, label)
        section("ASSISTANT")
        for name, label in (
            ("chat", "✉   Chat"),
            ("memory", "◈   Memory"),
            ("growth", "◒   Growth"),
            ("digest", "▤   Digest"),
        ):
            nav_button(name, label)
        section("SYSTEM")
        for name, label in (
            ("tools", "⌘   Tools & plugins"),
            ("system", "▥   System & usage"),
        ):
            nav_button(name, label)

        tk.Frame(parent, bg=SIDEBAR).pack(fill="both", expand=True)
        status = tk.Frame(parent, bg=SIDEBAR)
        status.pack(fill="x", padx=18, pady=(0, 10))
        self.status_label = tk.Label(
            status,
            text="●  Online",
            bg="#052e2b",
            fg="#6ee7b7",
            font=("Segoe UI Semibold", 9),
            padx=10,
            pady=4,
        )
        self.status_label.pack(anchor="w")

        self.pause_button = tk.Button(
            parent,
            text="Pause calls",
            command=self._toggle_pause,
            bg=SIDEBAR_ACTIVE,
            fg=SIDEBAR_ACTIVE_TEXT,
            activebackground="#334155",
            activeforeground="white",
            relief="flat",
            bd=0,
            anchor="w",
            padx=22,
            pady=8,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        self.pause_button.pack(fill="x", pady=(4, 2))

        for text, command in (
            ("Data folder", self._open_data),
            ("Core memory", self._open_core),
            ("Digest folder", self._open_digests),
        ):
            tk.Button(
                parent,
                text=text,
                command=command,
                bg=SIDEBAR,
                fg=SIDEBAR_TEXT,
                activebackground=SIDEBAR_ACTIVE,
                activeforeground=SIDEBAR_ACTIVE_TEXT,
                relief="flat",
                bd=0,
                anchor="w",
                padx=22,
                pady=4,
                font=("Segoe UI", 8),
                cursor="hand2",
            ).pack(fill="x")
        tk.Frame(parent, bg=SIDEBAR, height=14).pack(fill="x")

    def _build_topbar(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=BG)
        bar.grid(row=1, column=0, sticky="ew", padx=24, pady=(18, 12))
        left = tk.Frame(bar, bg=BG)
        left.pack(side="left")
        hour = datetime.now().hour
        greeting = (
            "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        )
        ttk.Label(left, text=f"{greeting}, Jo", style="Greeting.TLabel").pack(anchor="w")
        ttk.Label(
            left,
            text="Your local AI secretary is watching the noise so you don't have to.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")
        self.updated = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.updated, style="Sub.TLabel").pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(
            right, text="Refresh", style="Ghost.TButton", command=self.refresh
        ).pack(side="right")

    def _build_cards(self, parent: tk.Frame) -> None:
        cards = tk.Frame(parent, bg=BG)
        cards.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 14))
        self.card_vars = {
            "events": tk.StringVar(value="0"),
            "eligible": tk.StringVar(value="0"),
            "queued": tk.StringVar(value="0"),
            "proposals": tk.StringVar(value="0"),
            "spend": tk.StringVar(value="$0.00"),
            "paused": tk.StringVar(value="no"),
        }
        labels = [
            ("events", "Events", "▤", ACCENT),
            ("eligible", "Above gate", "✦", PINK),
            ("queued", "Suggestions", "◈", VIOLET),
            ("proposals", "Proposals", "◇", AMBER),
            ("spend", "Spend today", "$", GREEN),
            ("paused", "State", "●", MUTED),
        ]
        for index, (key, title, icon, color) in enumerate(labels):
            card = tk.Frame(
                cards,
                bg=CARD_BG,
                highlightbackground=BORDER,
                highlightthickness=1,
            )
            card.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=(0, 8) if index < 5 else 0,
            )
            cards.columnconfigure(index, weight=1)
            tk.Frame(card, bg=color, height=3).pack(fill="x")
            body = tk.Frame(card, bg=CARD_BG)
            body.pack(fill="both", expand=True, padx=12, pady=9)
            top = tk.Frame(body, bg=CARD_BG)
            top.pack(fill="x")
            tk.Label(
                top, text=icon, bg=CARD_BG, fg=color, font=("Segoe UI Semibold", 11)
            ).pack(side="left")
            tk.Label(
                top,
                text=title.upper(),
                bg=CARD_BG,
                fg=MUTED,
                font=("Segoe UI Semibold", 8),
            ).pack(side="left", padx=(6, 0))
            tk.Label(
                body,
                textvariable=self.card_vars[key],
                bg=CARD_BG,
                fg=INK,
                font=("Segoe UI Semibold", 15),
            ).pack(anchor="w", pady=(4, 0))

    def _build_footer(self, parent: tk.Frame) -> None:
        footer = tk.Frame(parent, bg=BG)
        footer.grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 10))
        self.footer = tk.StringVar(value="")
        tk.Label(
            footer,
            textvariable=self.footer,
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            footer,
            text="local only · no cloud sync",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(side="right")

    def _tile(
        self,
        parent: tk.Frame,
        column: int,
        title: str,
        color: str,
        variable: tk.StringVar,
        hint: str = "",
    ) -> None:
        card = tk.Frame(
            parent, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 8))
        parent.columnconfigure(column, weight=1)
        tk.Frame(card, bg=color, height=3).pack(fill="x")
        body = tk.Frame(card, bg=CARD_BG)
        body.pack(fill="both", expand=True, padx=12, pady=9)
        tk.Label(
            body,
            text=title.upper(),
            bg=CARD_BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w")
        tk.Label(
            body,
            textvariable=variable,
            bg=CARD_BG,
            fg=INK,
            font=("Segoe UI Semibold", 14),
        ).pack(anchor="w", pady=(3, 0))
        if hint:
            tk.Label(
                body,
                text=hint,
                bg=CARD_BG,
                fg=MUTED,
                font=("Segoe UI", 7),
                anchor="w",
            ).pack(anchor="w")

    def _page(self, name: str, title: str, subtitle: str) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        header = tk.Frame(page, bg=BG)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text=title, style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(header, text=subtitle, style="PageSub.TLabel").pack(
            anchor="w", pady=(1, 0)
        )
        body = ttk.Frame(page, style="Content.TFrame")
        body.pack(fill="both", expand=True)
        self.pages[name] = page
        return body

    def _show_page(self, name: str) -> None:
        page = self.pages.get(name)
        if page is None:
            return
        page.tkraise()
        for key, button in self.nav_buttons.items():
            active = key == name
            button.configure(
                bg=SIDEBAR_ACTIVE if active else SIDEBAR,
                fg=SIDEBAR_ACTIVE_TEXT if active else SIDEBAR_TEXT,
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
        tab = self._page(
            "events",
            "Events",
            "Redacted observations from your logs, messages, and attached sources.",
        )
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
        tab = self._page(
            "suggestions",
            "Suggestions",
            "Clustered advice with evidence, runbooks, and explicit approval.",
        )
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
                ("duplicates", "Dup", 40),
                ("text", "Suggestion", 440),
                ("reason", "Reason", 240),
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
        ttk.Button(
            buttons, text="Mark done", style="Ghost.TButton",
            command=lambda: self._set_suggestion("done")
        ).pack(side="left")
        ttk.Button(
            buttons, text="Dismiss", style="Ghost.TButton",
            command=lambda: self._set_suggestion("dismissed")
        ).pack(side="left", padx=6)
        ttk.Button(
            buttons, text="Copy", style="Ghost.TButton",
            command=self._copy_suggestion
        ).pack(side="left")
        ttk.Button(
            buttons, text="Draft fix", style="Ghost.TButton",
            command=self._draft_fix
        ).pack(side="left", padx=(12, 0))
        ttk.Button(
            buttons, text="Apply fix", style="Primary.TButton",
            command=self._apply_fix
        ).pack(side="left", padx=6)
        ttk.Button(
            buttons, text="Save answer", style="Ghost.TButton",
            command=self._save_chat_as_suggestion
        ).pack(side="right")

    def _build_chat_tab(self) -> None:
        tab = self._page(
            "chat",
            "Chat with Alice",
            "Ask about events, memory, the digest, your PC, calendar, email, or the web.",
        )
        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        info = ttk.Frame(tab)
        info.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(
            info,
            text="Alice can use read-only tools: time, system, files, web, calendar, email. "
            "Changes still need explicit approval.",
            style="Sub.TLabel",
        ).pack(side="left")

        attach_row = ttk.Frame(tab)
        attach_row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(
            attach_row, text="Attach file...", style="Ghost.TButton",
            command=self._attach_chat_file
        ).pack(side="left")
        ttk.Button(
            attach_row, text="Remove file", style="Ghost.TButton",
            command=self._clear_chat_attachments
        ).pack(side="left", padx=6)
        self.chat_attachment_var = tk.StringVar(value="No file attached")
        ttk.Label(
            attach_row, textvariable=self.chat_attachment_var, style="Sub.TLabel"
        ).pack(side="left", padx=6)

        frame = tk.Frame(
            tab, bg=CHAT_BG, highlightbackground=BORDER, highlightthickness=1
        )
        frame.grid(row=2, column=0, sticky="nsew")
        self.chat_text = tk.Text(
            frame,
            wrap="word",
            font=("Segoe UI", 10),
            background=CHAT_BG,
            foreground=INK,
            relief="flat",
            padx=16,
            pady=12,
            state="disabled",
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=scroll.set)
        self.chat_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.chat_text.tag_configure(
            "meta",
            foreground=MUTED,
            font=("Segoe UI", 8),
        )
        self.chat_text.tag_configure(
            "meta_user",
            foreground=MUTED,
            font=("Segoe UI", 8),
            justify="right",
            rmargin=12,
        )
        self.chat_text.tag_configure(
            "user_bubble",
            background=ACCENT_SOFT,
            foreground=INK,
            font=("Segoe UI", 10),
            lmargin1=80,
            lmargin2=80,
            rmargin=12,
            spacing1=4,
            spacing3=6,
            justify="right",
        )
        self.chat_text.tag_configure(
            "assistant_bubble",
            background="#151b2b",
            foreground=INK,
            font=("Segoe UI", 10),
            lmargin1=12,
            lmargin2=12,
            rmargin=80,
            spacing1=4,
            spacing3=6,
        )

        input_frame = tk.Frame(
            tab, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1
        )
        input_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.chat_input = tk.Text(
            input_frame,
            height=3,
            wrap="word",
            font=("Segoe UI", 10),
            background=CARD_BG,
            foreground=INK,
            insertbackground=INK,
            relief="flat",
            padx=10,
            pady=8,
        )
        self.chat_input.pack(side="left", fill="both", expand=True)
        self.chat_input.bind("<Control-Return>", self._send_chat_event)
        buttons = ttk.Frame(input_frame)
        buttons.pack(side="right", fill="y", padx=(8, 8), pady=8)
        self.chat_send_button = ttk.Button(
            buttons, text="Send", style="Primary.TButton", command=self._send_chat
        )
        self.chat_send_button.pack(fill="x")
        ttk.Button(
            buttons, text="Clear", style="Ghost.TButton", command=self._clear_chat
        ).pack(fill="x", pady=(6, 0))
        ttk.Button(
            buttons,
            text="Save answer",
            style="Ghost.TButton",
            command=self._save_chat_as_suggestion,
        ).pack(fill="x", pady=(6, 0))
        self.chat_status = tk.StringVar(value="Ready")
        ttk.Label(tab, textvariable=self.chat_status, style="Sub.TLabel").grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )

    def _build_memory_tab(self) -> None:
        tab = self._page(
            "memory",
            "Memory",
            "Core identity and the facts you approved.",
        )

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
        ttk.Button(
            buttons, text="Approve selected", style="Primary.TButton",
            command=self._approve
        ).pack(side="left")
        ttk.Button(
            buttons, text="Reject selected", style="Ghost.TButton",
            command=self._reject
        ).pack(side="left", padx=6)

    def _build_growth_tab(self) -> None:
        tab = self._page(
            "growth",
            "Growth",
            "Slow-weighted dispositions learned from exploration sessions. "
            "Core memory remains Jo-only.",
        )
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 6))
        self.growth_note = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.growth_note, style="Sub.TLabel").pack(
            side="left"
        )
        ttk.Button(
            row,
            text="Exploration folder",
            style="Ghost.TButton",
            command=self._open_exploration,
        ).pack(side="right")
        ttk.Button(
            row, text="Refresh", style="Ghost.TButton", command=self.refresh
        ).pack(side="right", padx=6)

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        memories = ttk.LabelFrame(panes, text="Learned dispositions", padding=6)
        sessions = ttk.LabelFrame(panes, text="Ingested sessions", padding=6)
        panes.add(memories, weight=3)
        panes.add(sessions, weight=2)
        self.growth_tree = self._tree(
            memories,
            [
                ("weight", "Weight", 60),
                ("kind", "Kind", 70),
                ("seen", "Seen", 50),
                ("last", "Last seen", 130),
                ("text", "Disposition", 430),
            ],
        )
        self.growth_session_tree = self._tree(
            sessions,
            [
                ("id", "ID", 45),
                ("created", "Added", 130),
                ("steps", "Steps", 50),
                ("status", "Status", 80),
                ("notes", "Notes", 45),
            ],
        )

    def _build_digest_tab(self) -> None:
        tab = self._page(
            "digest",
            "Daily digest",
            "The durable summary of what Alice noticed today.",
        )
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(
            buttons, text="Write digest now", style="Primary.TButton",
            command=self._write_digest
        ).pack(side="left")
        ttk.Button(
            buttons, text="Refresh", style="Ghost.TButton",
            command=self.refresh
        ).pack(side="left", padx=6)
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

    def _build_dashboard_tab(self) -> None:
        tab = self._page(
            "dashboard",
            "Dashboard",
            "The short version: health, activity, tokens, memory, and tools.",
        )
        self.home_vars: dict[str, tk.StringVar] = {}
        tiles = tk.Frame(tab, bg=BG)
        tiles.pack(fill="x", pady=(4, 10))
        for column, (key, title, color, hint) in enumerate(
            (
                ("state", "State", GREEN, "model calls"),
                ("cpu", "CPU", ACCENT, "current load"),
                ("memory", "Memory", VIOLET, "system RAM"),
                ("disk", "Disk free", AMBER, "home drive"),
                ("tokens", "Tokens today", PINK, "in + out"),
                ("toolcalls", "Tool calls", ACCENT, "today / total"),
            )
        ):
            variable = tk.StringVar(value="—")
            self.home_vars[key] = variable
            self._tile(tiles, column, title, color, variable, hint)

        glance = tk.Frame(
            tab, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1
        )
        glance.pack(fill="x", pady=(0, 10))
        tk.Frame(glance, bg=ACCENT, height=2).pack(fill="x")
        body = tk.Frame(glance, bg=CARD_BG)
        body.pack(fill="x", padx=14, pady=10)
        tk.Label(
            body,
            text="TODAY AT A GLANCE",
            bg=CARD_BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w")
        self.home_summary = tk.StringVar(value="Loading…")
        tk.Label(
            body,
            textvariable=self.home_summary,
            bg=CARD_BG,
            fg=INK,
            font=("Segoe UI", 10),
            justify="left",
            anchor="w",
            wraplength=950,
        ).pack(anchor="w", pady=(4, 0))

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        events_frame = ttk.LabelFrame(panes, text="Latest events", padding=6)
        tools_frame = ttk.LabelFrame(panes, text="Latest tool calls", padding=6)
        panes.add(events_frame, weight=3)
        panes.add(tools_frame, weight=2)
        self.home_event_tree = self._tree(
            events_frame,
            [
                ("ts", "Time", 120),
                ("source", "Source", 120),
                ("text", "Redacted text", 420),
            ],
        )
        self.home_tool_tree = self._tree(
            tools_frame,
            [
                ("tool", "Tool", 120),
                ("ok", "OK", 40),
                ("summary", "Summary", 320),
            ],
        )

    def _build_tools_tab(self) -> None:
        tab = self._page(
            "tools",
            "Tools & plugins",
            "Read-only integrations Alice can use from chat. Changes still need approval.",
        )
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 6))
        ttk.Button(
            row,
            text="Open Wikipedia",
            style="Ghost.TButton",
            command=lambda: webbrowser.open("https://en.wikipedia.org/wiki/Main_Page"),
        ).pack(side="left")
        ttk.Button(
            row,
            text="Calendar folder",
            style="Ghost.TButton",
            command=self._open_calendar_folder,
        ).pack(side="left", padx=6)
        ttk.Button(
            row,
            text="Exploration session",
            style="Ghost.TButton",
            command=self._open_exploration,
        ).pack(side="left")
        ttk.Button(
            row, text="Refresh", style="Ghost.TButton", command=self.refresh
        ).pack(side="right")
        self.tool_note = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.tool_note, style="Sub.TLabel").pack(
            anchor="w", pady=(0, 6)
        )

        panes = ttk.PanedWindow(tab, orient="vertical")
        panes.pack(fill="both", expand=True)
        top = ttk.Frame(panes)
        bottom = ttk.LabelFrame(panes, text="Recent tool calls", padding=6)
        panes.add(top, weight=3)
        panes.add(bottom, weight=2)
        top.grid_rowconfigure(0, weight=1)
        top.grid_columnconfigure(0, weight=3)
        top.grid_columnconfigure(1, weight=2)
        tools_frame = ttk.LabelFrame(top, text="Available tools", padding=6)
        plugins_frame = ttk.LabelFrame(top, text="Plugins", padding=6)
        tools_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        plugins_frame.grid(row=0, column=1, sticky="nsew")
        self.tool_tree = self._tree(
            tools_frame,
            [
                ("name", "Tool", 130),
                ("access", "Access", 70),
                ("status", "Status", 70),
                ("note", "Detail", 260),
            ],
        )
        self.plugin_tree = self._tree(
            plugins_frame,
            [
                ("id", "Plugin", 150),
                ("version", "Version", 70),
                ("capabilities", "Capabilities", 160),
            ],
        )
        self.tool_call_tree = self._tree(
            bottom,
            [
                ("id", "ID", 45),
                ("created", "Time", 150),
                ("tool", "Tool", 130),
                ("ok", "OK", 40),
                ("duration", "ms", 60),
                ("summary", "Summary", 520),
            ],
        )

    def _build_system_tab(self) -> None:
        tab = self._page(
            "system",
            "System & usage",
            "Live hardware counters plus every model token Alice has accounted for.",
        )
        self.system_vars: dict[str, tk.StringVar] = {}
        tiles = tk.Frame(tab, bg=BG)
        tiles.pack(fill="x", pady=(4, 8))
        for column, (key, title, color, hint) in enumerate(
            (
                ("cpu", "CPU", ACCENT, "logical cores"),
                ("memory", "Memory", VIOLET, "used / total"),
                ("disk", "Disk", AMBER, "free / total"),
                ("uptime", "Uptime", GREEN, "hours"),
                ("processes", "Processes", PINK, "running"),
                ("calls", "Model calls", ACCENT, "all time"),
            )
        ):
            variable = tk.StringVar(value="—")
            self.system_vars[key] = variable
            self._tile(tiles, column, title, color, variable, hint)

        token_row = tk.Frame(tab, bg=BG)
        token_row.pack(fill="x", pady=(0, 8))
        self.system_vars["input"] = tk.StringVar(value="—")
        self.system_vars["output"] = tk.StringVar(value="—")
        self.system_vars["tokens"] = tk.StringVar(value="—")
        self.system_vars["cost"] = tk.StringVar(value="—")
        for column, (key, title, color, hint) in enumerate(
            (
                ("input", "Input tokens", CYAN, "prompt"),
                ("output", "Output tokens", PINK, "completion"),
                ("tokens", "Total tokens", VIOLET, "all time"),
                ("cost", "Cost", GREEN, "reported USD"),
            )
        ):
            self._tile(token_row, column, title, color, self.system_vars[key], hint)

        self.system_note = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.system_note, style="Sub.TLabel").pack(
            anchor="w", pady=(0, 6)
        )
        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        tier_frame = ttk.LabelFrame(panes, text="Usage by tier", padding=6)
        day_frame = ttk.LabelFrame(panes, text="Usage by day", padding=6)
        panes.add(tier_frame, weight=1)
        panes.add(day_frame, weight=1)
        self.usage_tier_tree = self._tree(
            tier_frame,
            [
                ("label", "Tier", 110),
                ("calls", "Calls", 60),
                ("input", "Input", 100),
                ("output", "Output", 100),
                ("cost", "Cost", 85),
            ],
        )
        self.usage_day_tree = self._tree(
            day_frame,
            [
                ("label", "Day", 110),
                ("calls", "Calls", 60),
                ("input", "Input", 100),
                ("output", "Output", 100),
                ("cost", "Cost", 85),
            ],
        )

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
        self.card_vars["paused"].set("Paused" if dashboard.paused else "Online")
        self.pause_button.configure(
            text="Resume calls" if dashboard.paused else "Pause calls"
        )
        if dashboard.paused:
            self.status_label.configure(
                text="●  Paused", bg="#3b2f0b", fg="#fbbf24"
            )
        else:
            self.status_label.configure(
                text="●  Online", bg="#052e2b", fg="#6ee7b7"
            )

        self._fill_events(dashboard)
        self._fill_suggestions(dashboard)
        self._fill_memory(dashboard)
        self._fill_growth(dashboard)
        self._fill_dashboard(dashboard)
        self._fill_tools(dashboard)
        self._fill_system(dashboard)
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
                    suggestion.duplicate_count if suggestion.duplicate_count > 1 else "",
                    suggestion.text,
                    suggestion.reason or "",
                ),
                tags=(f"p{suggestion.priority}",),
            )
            if selected is not None and suggestion.id == selected:
                self.suggestion_tree.selection_set(item)
                self.suggestion_tree.focus(item)
        self._show_suggestion_detail()

    def _fill_dashboard(self, dashboard: Dashboard) -> None:
        hardware = dashboard.hardware
        total_events = sum(dashboard.status_counts.values())
        self.home_vars["state"].set("Paused" if dashboard.paused else "Online")
        self.home_vars["cpu"].set(f"{hardware.get('cpu_percent', 0)}%")
        self.home_vars["memory"].set(
            f"{hardware.get('memory_used_gb', 0)} / "
            f"{hardware.get('memory_total_gb', 0)} GB"
        )
        self.home_vars["disk"].set(f"{hardware.get('disk_free_gb', 0)} GB")
        self.home_vars["tokens"].set(_number(dashboard.usage_today.total_tokens))
        self.home_vars["toolcalls"].set(
            f"{dashboard.tool_calls_today} / {dashboard.tool_calls_total}"
        )

        exploration = dashboard.exploration or {}
        exploration_line = "No exploration session yet."
        if exploration:
            exploration_line = (
                f"Exploration: {exploration.get('status', 'unknown')} · "
                f"{exploration.get('steps', 0)} steps · "
                f"{exploration.get('current_title') or 'starting'}"
            )
        email = next(
            (tool for tool in dashboard.tools if tool.name == "email.recent"), None
        )
        email_line = (
            "email ready"
            if email and email.enabled
            else f"email {email.note if email else 'unavailable'}"
        )
        self.home_summary.set(
            f"{total_events:,} events · {dashboard.new_above_gate} above gate · "
            f"{dashboard.queued_suggestions} queued suggestions · "
            f"{dashboard.tool_calls_total} tool calls · "
            f"{_number(dashboard.usage_total.total_tokens)} tokens all time\n"
            f"Core memory ~{dashboard.core_tokens}/{dashboard.core_max_tokens} tokens · "
            f"{dashboard.approved_count} approved facts · "
            f"{dashboard.active_tasks} open tasks · {dashboard.embedded_count} embeddings\n"
            f"Growth: {dashboard.growth_stats['memories']} dispositions from "
            f"{dashboard.growth_stats['sessions']} "
            f"session{'s' if dashboard.growth_stats['sessions'] != 1 else ''} · "
            f"top weight {dashboard.growth_stats['top_weight']:.2f} · "
            f"{exploration_line} · {email_line}"
        )

        self._clear(self.home_event_tree)
        for event in dashboard.events[:8]:
            self.home_event_tree.insert(
                "",
                "end",
                values=(event.ts[5:16], event.source, event.text[:140]),
                tags=(event.status,),
            )
        self._clear(self.home_tool_tree)
        for call in dashboard.recent_tool_calls[:10]:
            self.home_tool_tree.insert(
                "",
                "end",
                values=(
                    call.tool,
                    "yes" if call.ok else "no",
                    call.summary[:160],
                ),
            )

    def _fill_growth(self, dashboard: Dashboard) -> None:
        self._clear(self.growth_tree)
        for memory in dashboard.growth_memories:
            self.growth_tree.insert(
                "",
                "end",
                values=(
                    f"{memory.weight:.2f}",
                    memory.kind,
                    memory.evidence_count,
                    memory.last_seen[:16].replace("T", " "),
                    memory.text,
                ),
            )
        self._clear(self.growth_session_tree)
        for session in dashboard.growth_sessions:
            self.growth_session_tree.insert(
                "",
                "end",
                values=(
                    session.id,
                    session.created[:16].replace("T", " "),
                    session.steps,
                    session.status or "-",
                    len(session.themes),
                ),
            )
        session_count = len(dashboard.growth_sessions)
        self.growth_note.set(
            f"{len(dashboard.growth_memories)} active dispositions · "
            f"{session_count} recent session{'s' if session_count != 1 else ''} · "
            "weights decay slowly and rise only with repetition"
        )

    def _fill_tools(self, dashboard: Dashboard) -> None:
        self._clear(self.tool_tree)
        ready = 0
        for tool in dashboard.tools:
            ready += 1 if tool.enabled else 0
            self.tool_tree.insert(
                "",
                "end",
                values=(
                    tool.name,
                    "read-only" if tool.read_only else "ACTION",
                    "ready" if tool.enabled else "off",
                    f"{tool.note} · {tool.description}"[:220],
                ),
            )
        self.tool_note.set(
            f"{ready}/{len(dashboard.tools)} tools ready · "
            f"{dashboard.tool_calls_today} calls today · "
            f"web allowlist configured in alice.yaml"
        )
        self._clear(self.plugin_tree)
        if dashboard.plugins:
            for plugin in dashboard.plugins:
                self.plugin_tree.insert(
                    "",
                    "end",
                    values=(
                        plugin.id,
                        plugin.version,
                        ", ".join(plugin.capabilities) or "-",
                    ),
                )
        else:
            self.plugin_tree.insert(
                "", "end", values=("(none registered)", "-", "-")
            )
        self._clear(self.tool_call_tree)
        for call in dashboard.recent_tool_calls:
            self.tool_call_tree.insert(
                "",
                "end",
                values=(
                    call.id,
                    call.created,
                    call.tool,
                    "yes" if call.ok else "no",
                    call.duration_ms,
                    call.summary[:220],
                ),
            )

    def _fill_system(self, dashboard: Dashboard) -> None:
        hardware = dashboard.hardware
        usage = dashboard.usage_total
        self.system_vars["cpu"].set(
            f"{hardware.get('cpu_percent', 0)}% · {hardware.get('cpu_count', 0)} cores"
        )
        self.system_vars["memory"].set(
            f"{hardware.get('memory_used_gb', 0)} / "
            f"{hardware.get('memory_total_gb', 0)} GB"
        )
        self.system_vars["disk"].set(
            f"{hardware.get('disk_free_gb', 0)} / "
            f"{hardware.get('disk_total_gb', 0)} GB free"
        )
        self.system_vars["uptime"].set(f"{hardware.get('uptime_hours', 0)} h")
        self.system_vars["processes"].set(str(hardware.get("process_count", 0)))
        self.system_vars["calls"].set(f"{usage.calls:,}")
        self.system_vars["input"].set(_number(usage.input_tokens))
        self.system_vars["output"].set(_number(usage.output_tokens))
        self.system_vars["tokens"].set(_number(usage.total_tokens))
        self.system_vars["cost"].set(f"${usage.cost_usd:.4f}")
        self.system_note.set(
            f"schema v{dashboard.schema_version} · "
            f"core {dashboard.core_tokens}/{dashboard.core_max_tokens} tokens · "
            f"{dashboard.summary_count} summaries · "
            f"last consolidation {dashboard.last_consolidation or 'never'} · "
            f"{dashboard.data_dir}"
        )
        self._clear(self.usage_tier_tree)
        for row in dashboard.usage_by_tier:
            self.usage_tier_tree.insert(
                "",
                "end",
                values=(
                    row.label,
                    row.calls,
                    _number(row.input_tokens),
                    _number(row.output_tokens),
                    f"${row.cost_usd:.4f}",
                ),
            )
        self._clear(self.usage_day_tree)
        for row in dashboard.usage_by_day:
            self.usage_day_tree.insert(
                "",
                "end",
                values=(
                    row.label,
                    row.calls,
                    _number(row.input_tokens),
                    _number(row.output_tokens),
                    f"${row.cost_usd:.4f}",
                ),
            )

    def _open_calendar_folder(self) -> None:
        path = self.settings.tools.calendar_file
        if path is None:
            messagebox.showinfo("Alice", "No calendar file is configured.")
            return
        from .tools import ensure_calendar_file

        ensure_calendar_file(path)
        self._open(path.parent)

    def _open_exploration(self) -> None:
        self._open(self.settings.data_dir / "exploration")

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
            if suggestion.duplicate_count > 1:
                meta.append(f"merged from {suggestion.duplicate_count} reports")
            if suggestion.cluster:
                meta.append(f"cluster {suggestion.cluster}")
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

    # -- fix actions -------------------------------------------------------

    def _draft_fix(self) -> None:
        suggestion_id = self._selected_suggestion_id()
        if suggestion_id is None:
            messagebox.showinfo("Alice", "Select a suggestion first.")
            return
        if self._fix_pending:
            return
        self._fix_pending = True
        self.footer.set("Alice is drafting a fix plan...")

        def worker() -> None:
            conn = None
            try:
                from .bootstrap import build_fix_planner, build_models

                conn = connect(self.settings.db_path, create=False)
                router = build_models(self.settings, conn)
                planner = build_fix_planner(self.settings, conn, router)
                plan = planner.plan(suggestion_id)
            except Exception as exc:
                self._fix_queue.put((suggestion_id, None, exc))
            else:
                self._fix_queue.put((suggestion_id, plan, None))
            finally:
                if conn is not None:
                    conn.close()

        threading.Thread(target=worker, name="alice-fix-plan", daemon=True).start()
        self._fix_poll_id = self.root.after(250, self._poll_fix)

    def _poll_fix(self) -> None:
        try:
            suggestion_id, plan, error = self._fix_queue.get_nowait()
        except queue.Empty:
            self._fix_poll_id = self.root.after(250, self._poll_fix)
            return
        self._fix_pending = False
        if error is not None:
            self.footer.set(f"Fix failed: {error}")
            messagebox.showerror("Alice", f"Fix failed: {error}")
            return

        if isinstance(plan, ActionRow):
            output = (plan.output or "")[:4000]
            detail = (
                f"Action #{plan.id}: {plan.status}\n"
                f"Exit code: {plan.exit_code}\n"
                f"Error: {plan.error or 'none'}\n\n{output}"
            )
            self.footer.set(f"Action #{plan.id} {plan.status}.")
            messagebox.showinfo("Action result", detail)
            self.refresh()
            return

        action_id = None
        action_note = "No command proposed; use the manual steps."
        if plan.program:
            try:
                action_id = ActionRunner(
                    self.conn, self.settings.core.actions, self.settings.root
                ).propose(
                    suggestion_id,
                    summary=plan.summary,
                    program=plan.program,
                    args=list(plan.args),
                    cwd=plan.cwd,
                    risk=plan.risk,
                )
                action_note = f"Proposed action #{action_id}. Use Apply fix to approve it."
            except Exception as exc:
                action_note = f"Command was not saved: {exc}"

        command = (
            " ".join([plan.program, *plan.args]) if plan.program else "(none)"
        )
        lines = [
            plan.summary,
            "",
            f"Command: {command}",
            f"Working directory: {plan.cwd or self.settings.root}",
            f"Risk: {plan.risk}",
        ]
        if plan.manual_steps:
            lines.extend(["", "Manual steps:"])
            lines.extend(f"{index}. {step}" for index, step in enumerate(plan.manual_steps, 1))
        lines.extend(["", action_note])
        self.footer.set(action_note)
        messagebox.showinfo("Fix proposal", "\n".join(lines))
        self.refresh()

    def _apply_fix(self) -> None:
        suggestion_id = self._selected_suggestion_id()
        if suggestion_id is None:
            messagebox.showinfo("Alice", "Select a suggestion first.")
            return
        if self._fix_pending:
            return
        runner = ActionRunner(
            self.conn, self.settings.core.actions, self.settings.root
        )
        action = runner.latest(suggestion_id)
        if action is None:
            messagebox.showinfo("Alice", "Draft a fix first, then approve it here.")
            return
        command = " ".join([action.program, *action.args])
        confirmation = simpledialog.askstring(
            "Approve action",
            f"{action.summary}\n\n{command}\n\n"
            f"Risk: {action.risk or 'unknown'}\n\n"
            "Type RUN exactly to approve and execute:",
            parent=self.root,
        )
        if confirmation != "RUN":
            self.footer.set("Action cancelled; nothing was run.")
            return
        self._fix_pending = True
        self.footer.set(f"Running action {action.id}...")

        def worker() -> None:
            conn = None
            try:
                conn = connect(self.settings.db_path, create=False)
                runner = ActionRunner(
                    conn, self.settings.core.actions, self.settings.root
                )
                result = runner.execute(action.id, confirmation="RUN")
            except Exception as exc:
                self._fix_queue.put((suggestion_id, None, exc))
            else:
                self._fix_queue.put((suggestion_id, result, None))
            finally:
                if conn is not None:
                    conn.close()

        threading.Thread(target=worker, name="alice-fix-run", daemon=True).start()
        self._fix_poll_id = self.root.after(250, self._poll_fix)

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
            is_user = message.role == "user"
            stamp = message.created[11:16] if len(message.created) >= 16 else ""
            who = f"You  {stamp}" if is_user else f"Alice · AI secretary  {stamp}"
            self.chat_text.insert(
                "end", f"{who}\n", "meta_user" if is_user else "meta"
            )
            self.chat_text.insert(
                "end",
                message.content + "\n",
                "user_bubble" if is_user else "assistant_bubble",
            )
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
                        "meta_user" if is_user else "meta",
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
        stamp = datetime.now().strftime("%H:%M")
        self.chat_text.insert("end", f"You  {stamp}\n", "meta_user")
        self.chat_text.insert("end", question + "\n\n", "user_bubble")
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
            if path.suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Alice", f"Cannot open {path}: {exc}")

    def close(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        if self._chat_poll_id is not None:
            self.root.after_cancel(self._chat_poll_id)
        if self._fix_poll_id is not None:
            self.root.after_cancel(self._fix_poll_id)
        try:
            self.conn.close()
        finally:
            self.root.destroy()


def _number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:,}"


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
    from .tools import ensure_calendar_file

    ensure_calendar_file(settings.tools.calendar_file)

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
