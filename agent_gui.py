"""
Modern GUI for agent.py — CustomTkinter edition.
"""

import customtkinter as ctk
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, filedialog
import threading
import queue
import io
import contextlib
import time
import os
import re
import datetime

import agent
import scheduler
import voice_output
import tray
import tkinter.filedialog as filedialog
from rag_tool import index_documents

import gui_widgets as gw

try:
    import keyboard
except ImportError:
    keyboard = None


# ---------------------------
# THEME
# ---------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

COLOR_BG           = gw.COLOR_BG
COLOR_BG_ALT       = gw.COLOR_BG_ALT
COLOR_HEADER       = gw.COLOR_BG
COLOR_SIDEBAR      = gw.COLOR_SIDEBAR
COLOR_RAISED       = gw.COLOR_RAISED
COLOR_BORDER       = gw.COLOR_BORDER
COLOR_USER_BUBBLE  = gw.COLOR_USER_PILL
COLOR_USER_TEXT    = gw.COLOR_TEXT_HI
COLOR_BOT_BUBBLE   = gw.COLOR_BG
COLOR_BOT_TEXT     = gw.COLOR_TEXT_HI
COLOR_SYS_BUBBLE   = gw.COLOR_BG_ALT
COLOR_SYS_TEXT     = gw.COLOR_TEXT_LOW
COLOR_SCHED_BUBBLE = gw.COLOR_BG
COLOR_SCHED_TEXT   = gw.COLOR_SCHED_TEXT
COLOR_INPUT_BG     = gw.COLOR_INPUT_BG
COLOR_BTN_PRIMARY  = gw.COLOR_ACCENT
COLOR_BTN_DANGER   = gw.COLOR_DANGER
COLOR_BTN_GHOST    = "#2a2e3d"
COLOR_STATUS_OK    = gw.COLOR_ACCENT
COLOR_STATUS_BUSY  = gw.COLOR_WARN
COLOR_TEXT_HI      = gw.COLOR_TEXT_HI
COLOR_TEXT_MID     = gw.COLOR_TEXT_MID
COLOR_TEXT_LOW     = gw.COLOR_TEXT_LOW
COLOR_CODE_BG      = gw.COLOR_CODE_BG
COLOR_HOVER        = gw.COLOR_HOVER
COLOR_SELECTED     = gw.COLOR_SELECTED

SIDEBAR_W = 226
HEADER_H = 48

SLASH_COMMANDS = [
    {"cmd": "/help",           "desc": "Show all commands",                 "args": "",                "icon": "❓"},
    {"cmd": "/send",           "desc": "Send a Telegram message",           "args": "Contact|message", "icon": "✉️"},
    {"cmd": "/edit",           "desc": "Edit a Telegram message",           "args": "Contact|old|new", "icon": "✏️"},
    {"cmd": "/del",            "desc": "Delete a Telegram message",         "args": "Contact|text",    "icon": "🗑️"},
    {"cmd": "/dellast",        "desc": "Delete latest message to a contact","args": "Contact",         "icon": "🗑️"},
    {"cmd": "/find",           "desc": "Find files by name pattern",        "args": "folder|pat1,pat2","icon": "🔍"},
    {"cmd": "/move",           "desc": "Move files matching a pattern",     "args": "src|pat|dest",    "icon": "📦"},
    {"cmd": "/mkdir",          "desc": "Create a folder in Downloads",      "args": "foldername",      "icon": "📁"},
    {"cmd": "/map",            "desc": "Show repo map of a folder",         "args": "folder|depth",    "icon": "🗺️"},
    {"cmd": "/symbols",        "desc": "List functions/classes in a file",  "args": "filepath",        "icon": "🔣"},
    {"cmd": "/scan",           "desc": "Static analysis of a file",         "args": "file_path",       "icon": "🛡️"},
    {"cmd": "/quarantine",     "desc": "Move a suspicious file to Quarantine","args": "file_path",     "icon": "🔒"},
    {"cmd": "/quarantine-list","desc": "List quarantined files",            "args": "",                "icon": "📋"},
    {"cmd": "/rag",            "desc": "Search indexed documents",          "args": "query",           "icon": "📚"},
]


def _fuzzy_score(query, target):
    """Return a match score (higher = better, 0 = no match)."""
    if not query:
        return 1
    q = query.lower().lstrip("/")
    t = target.lower().lstrip("/")
    if not q:
        return 1
    # Exact prefix
    if t.startswith(q):
        return 1000 - len(t)
    # Substring anywhere
    if q in t:
        return 500 + (100 - t.index(q))
    # Subsequence with consecutive bonus
    qi = 0
    score = 0
    last_pos = -1
    for i, ch in enumerate(t):
        if qi < len(q) and ch == q[qi]:
            score += 10
            if i == last_pos + 1:
                score += 5
            last_pos = i
            qi += 1
    if qi == len(q):
        return score
    return 0


SLASH_HISTORY_FILE = os.path.join(
    os.environ.get("APPDATA", "."), "AgentBot", "slash_history.json"
)


def _load_slash_history():
    import json
    if not os.path.exists(SLASH_HISTORY_FILE):
        return []
    try:
        with open(SLASH_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_slash_history(history):
    import json
    try:
        os.makedirs(os.path.dirname(SLASH_HISTORY_FILE), exist_ok=True)
        with open(SLASH_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception:
        pass


def _record_slash_use(cmd):
    history = _load_slash_history()
    if cmd in history:
        history.remove(cmd)
    history.insert(0, cmd)
    history = history[:10]
    _save_slash_history(history)

FONT_UI = "Segoe UI"
BUBBLE_FONT_SIZE = 12
BUBBLE_PAD_X = 12
BUBBLE_PAD_Y = 7
BUBBLE_GAP = 3
BUBBLE_RADIUS = 16
MAX_BUBBLE_FRACTION = 0.72

_CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)

_CODE_EXT_MAP = {
    "python": "py", "py": "py",
    "javascript": "js", "js": "js", "jsx": "jsx",
    "typescript": "ts", "ts": "ts", "tsx": "tsx",
    "html": "html", "htm": "html", "css": "css", "scss": "scss",
    "json": "json", "yaml": "yaml", "yml": "yml", "toml": "toml",
    "bash": "sh", "sh": "sh", "shell": "sh", "zsh": "sh",
    "powershell": "ps1", "ps1": "ps1", "ps": "ps1",
    "sql": "sql", "md": "md", "markdown": "md",
    "c": "c", "cpp": "cpp", "c++": "cpp", "h": "h", "hpp": "hpp",
    "java": "java", "go": "go", "rust": "rs", "rs": "rs",
    "ruby": "rb", "rb": "rb", "php": "php",
    "xml": "xml", "csv": "csv", "txt": "txt", "text": "txt",
}


def _extract_first_code_block(text):
    m = _CODE_FENCE_RE.search(text)
    if not m:
        return None, None
    lang = (m.group(1) or "").lower().strip()
    code = m.group(2)
    ext = _CODE_EXT_MAP.get(lang, "txt")
    return code, ext


class AgentGUI:
    def __init__(self, root):
        # Apply settings before building UI
        try:
            import config
            _cfg = config.load_config()
            _theme = _cfg.get("THEME", "dark")
            if _theme in ("dark", "light", "system"):
                ctk.set_appearance_mode(_theme)

            global BUBBLE_FONT_SIZE, MAX_BUBBLE_FRACTION, BUBBLE_RADIUS
            try:
                BUBBLE_FONT_SIZE = int(_cfg.get("FONT_SIZE", "12"))
            except Exception:
                pass
            try:
                MAX_BUBBLE_FRACTION = max(
                    0.3, min(0.95, int(_cfg.get("BUBBLE_WIDTH", "72")) / 100.0)
                )
            except Exception:
                pass
            try:
                BUBBLE_RADIUS = int(_cfg.get("BUBBLE_RADIUS", "16"))
            except Exception:
                pass
        except Exception as e:
            print(f"[settings] could not apply startup settings: {e}")

        self.root = root
        root.title("Agent")
        root.geometry("880x760+100+100")
        root.minsize(700, 600)
        root.configure(fg_color=COLOR_BG)

        self.conversation_history = agent.load_conversation_history()
        self.result_queue = queue.Queue()
        self.processing = False
        self.recording = False
        self.bubble_log = []
        self.attachments = []
        self._attach_thumbs = []
        self._slash_popup = None      # Toplevel, created lazily
        self._slash_visible = False
        self._slash_matches = []      # current filtered list
        self._slash_index = 0         # selected row index
        self._user_scrolled_up = False
        self._scroll_after_ids = []
        self.log_visible = False
        self.tray_icon = None
        self._bubble_labels = []
        self._md_widgets = []
        self._char_px = None
        self._reflowing = False
        self._last_key = None

        # SHELL — sidebar + main column
        self.sidebar_visible = True
        self.sidebar = ctk.CTkFrame(root, fg_color=COLOR_SIDEBAR,
                                    corner_radius=0, width=SIDEBAR_W)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        self.main_col = ctk.CTkFrame(root, fg_color=COLOR_BG, corner_radius=0)
        self.main_col.pack(side="left", fill="both", expand=True)

        # HEADER — 48px, deliberately sparse
        header = ctk.CTkFrame(self.main_col, fg_color=COLOR_BG,
                              corner_radius=0, height=HEADER_H)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        self._icon_btn(header, "\u2630", self.toggle_sidebar,
                       size=14).pack(side="left", padx=(10, 4))

        ctk.CTkLabel(
            header, text="Agent",
            font=ctk.CTkFont(family=FONT_UI, size=15, weight="bold"),
            text_color=COLOR_BOT_TEXT
        ).pack(side="left", padx=(6, 10))

        self.status_dot = ctk.CTkLabel(
            header, text="●",
            font=ctk.CTkFont(size=10),
            text_color=COLOR_STATUS_OK
        )
        self.status_dot.pack(side="left")

        self.status_label = ctk.CTkLabel(
            header, text="Ready",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=COLOR_SYS_TEXT
        )
        self.status_label.pack(side="left", padx=(5, 0))

        self._icon_btn(header, "\u2699", self.open_settings_dialog,
                       size=14).pack(side="right", padx=(2, 10))
        self._icon_btn(header, "\u21bb", self._new_chat,
                       size=13).pack(side="right", padx=2)

        ctk.CTkFrame(self.main_col, fg_color=COLOR_BORDER, height=1,
                     corner_radius=0).pack(fill="x", side="top")

        # CHAT AREA
        self.chat_frame = ctk.CTkScrollableFrame(
            self.main_col, fg_color=COLOR_BG, corner_radius=0,
            scrollbar_button_color="#22262f",
            scrollbar_button_hover_color="#313745"
        )
        self.chat_frame.pack(fill="both", expand=True, padx=0, pady=0)

        try:
            canvas = self.chat_frame._parent_canvas
            canvas.bind("<MouseWheel>", self._on_user_scroll, add="+")
            canvas.bind("<Button-4>", self._on_user_scroll, add="+")
            canvas.bind("<Button-5>", self._on_user_scroll, add="+")
            canvas.bind("<Button-1>", self._on_user_scroll, add="+")
            canvas.bind("<Configure>", self._on_chat_canvas_configure, add="+")
            self.chat_frame.bind("<Configure>", self._on_chat_content_configure, add="+")
            # The canvas-level binds above only fire when the pointer is over the
            # canvas itself. Messages are separate widgets covering it, so in
            # practice a wheel over the chat never reached them and the
            # scrolled-up flag was never set. The toplevel is in every widget's
            # bindtags, so binding there always fires.
            self.root.bind("<MouseWheel>", self._on_user_scroll, add="+")
            self.root.bind("<Button-1>", self._on_user_scroll, add="+")
        except Exception:
            pass

        if self.conversation_history:
            self._add_system_bubble(f"Resumed — {len(self.conversation_history)} messages from last session")
        else:
            self._add_system_bubble("Hi! Ask me anything, attach files with 📎, or hold F9 to talk. Type /help for commands.")

        # TYPING INDICATOR
        self._rebuild_typing_frame()

        # INPUT AREA
        self._input_outer = ctk.CTkFrame(self.main_col, fg_color=COLOR_BG,
                                         corner_radius=0)
        self._input_outer.pack(fill="x", side="bottom")

        self.attach_strip = ctk.CTkFrame(self._input_outer, fg_color="transparent")

        pill = ctk.CTkFrame(
            self._input_outer,
            fg_color=COLOR_INPUT_BG,
            corner_radius=22,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        pill.pack(fill="x", padx=14, pady=12)
        self._input_row = pill

        self.entry = ctk.CTkTextbox(
            pill,
            font=ctk.CTkFont(family=FONT_UI, size=13),
            fg_color="transparent",
            border_width=0,
            corner_radius=0,
            height=30,
            wrap="word",
        )
        self.entry.pack(side="left", fill="both", expand=True, padx=(16, 4), pady=9)

        # Read ENTER_SENDS
        _enter_sends = True
        try:
            import config
            _enter_sends = str(config.load_config().get("ENTER_SENDS", "true")).lower() in ("true", "1", "yes", "on")
        except Exception:
            pass

        if _enter_sends:
            self.entry.bind("<Return>", self._on_send_key)
            self.entry.bind("<KP_Enter>", self._on_send_key)
            self.entry.bind("<Shift-Return>", self._on_newline_key)
            self.entry.bind("<Shift-KP_Enter>", self._on_newline_key)
        else:
            self.entry.bind("<Control-Return>", self._on_send_key)
            self.entry.bind("<Control-KP_Enter>", self._on_send_key)
            self.entry.bind("<Return>", self._on_newline_key)
            self.entry.bind("<KP_Enter>", self._on_newline_key)

        self.entry.bind("<<Paste>>", lambda e: self.root.after(10, self._autogrow_entry))
        self.entry.bind("<<Modified>>", lambda e: self.root.after(10, self._autogrow_entry))
        self.entry.bind("<KeyRelease>", self._autogrow_entry)

        # Slash autocomplete: detect text changes and dismiss on focus loss
        try:
            inner = self.entry._textbox
            inner.bind("<KeyRelease>", lambda e: self._maybe_show_slash_popup(), add="+")
            inner.bind("<FocusOut>", lambda e: self._hide_slash_popup(), add="+")
            inner.bind("<Up>", self._on_slash_up, add="+")
            inner.bind("<Down>", self._on_slash_down, add="+")
            inner.bind("<Tab>", self._on_slash_tab, add="+")
            inner.bind("<Escape>", self._on_slash_escape, add="+")
        except Exception:
            pass

        self._icon_clip = gw.make_icon("clip", 18, gw.COLOR_TEXT_MID)
        self._icon_mic = gw.make_icon("mic", 18, gw.COLOR_ACCENT)
        self._icon_mic_rec = gw.make_icon("mic", 18, "#ffffff")
        self._icon_send = gw.make_icon("send", 16, "#06281a")

        BTN = 32
        btn_cluster = ctk.CTkFrame(pill, fg_color="transparent")
        btn_cluster.pack(side="right", padx=(0, 8), pady=6)

        self.attach_btn = ctk.CTkButton(
            btn_cluster,
            text="" if self._icon_clip else "📎",
            image=self._icon_clip,
            width=BTN, height=BTN,
            fg_color="transparent", hover_color=COLOR_RAISED,
            corner_radius=8,
            command=self._pick_files,
        )
        self.attach_btn.pack(side="left", padx=2)

        self.mic_btn = ctk.CTkButton(
            btn_cluster,
            text="" if self._icon_mic else "🎤",
            image=self._icon_mic,
            width=BTN, height=BTN,
            fg_color="transparent", hover_color=COLOR_RAISED,
            corner_radius=8,
            command=self._noop,
        )
        self.mic_btn.pack(side="left", padx=2)
        self.mic_btn.bind("<ButtonPress-1>", self.on_mic_press)
        self.mic_btn.bind("<ButtonRelease-1>", self.on_mic_release)

        self.send_btn = ctk.CTkButton(
            btn_cluster,
            text="" if self._icon_send else "↑",
            image=self._icon_send,
            width=36, height=36,
            fg_color=COLOR_BTN_PRIMARY, hover_color=gw.COLOR_ACCENT_HOVER,
            corner_radius=18,
            command=self.send,
        )
        self.send_btn.pack(side="left", padx=(4, 0))

        # ACTIVITY LOG — hidden drawer, toggled above the input pill
        self.log_box = ctk.CTkTextbox(
            self.main_col, height=126, corner_radius=0,
            fg_color=COLOR_RAISED,
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=COLOR_TEXT_MID
        )

        # SETTINGS OVERLAY — rendered inside this window, not a Toplevel
        self._settings_overlay = ctk.CTkFrame(root, fg_color=COLOR_BG,
                                              corner_radius=0)

        # BACKGROUND SERVICES
        scheduler.start_scheduler(self.on_scheduled_trigger)
        agent.register_watcher_callback(self.on_scheduled_trigger)

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._setup_global_hotkey()

        self.root.bind("<Control-c>", self._copy_last_agent)
        self.root.bind("<Control-C>", self._copy_last_agent)
        self.root.bind("<Escape>", lambda e: self.stop_voice())
        self.root.bind("<Control-comma>", lambda e: self.open_settings_dialog())
        self.root.bind("<Configure>", self._on_root_configure)

        self.entry.focus()
        self.root.after(100, self.poll_queue)
        self.root.after(500, self._tick_typing)
        self.root.after(800, self._start_tray)
        self.root.after(5000, self._auto_index_startup)

        # Warm up TTS in the background so the first reply is instant
        try:
            voice_output.warmup()
        except Exception as e:
            print(f"[tts] warmup skipped: {e}")

        self.root.after(1200, self._settle_chat)

    # ------------------------------------------------------------------
    # Chrome helpers — sidebar, icon buttons, markdown reflow
    # ------------------------------------------------------------------
    def _rebuild_typing_frame(self):
        self.typing_frame = ctk.CTkFrame(
            self.chat_frame, fg_color=COLOR_RAISED,
            corner_radius=BUBBLE_RADIUS, height=32)
        self.typing_label = ctk.CTkLabel(
            self.typing_frame, text="● ● ●",
            font=ctk.CTkFont(family=FONT_UI, size=12),
            text_color=COLOR_TEXT_MID
        )
        self.typing_label.pack(padx=14, pady=6)
        self.typing_frame_visible = False
        self.typing_dots = 0

    def _icon_btn(self, parent, glyph, cmd, size=14, w=32):
        return ctk.CTkButton(
            parent, text=glyph, width=w, height=30, corner_radius=8,
            fg_color="transparent", hover_color=COLOR_HOVER,
            text_color=COLOR_TEXT_MID,
            font=ctk.CTkFont(family=FONT_UI, size=size),
            command=cmd,
        )

    def _nav_btn(self, parent, glyph, label, cmd, active=False):
        b = ctk.CTkButton(
            parent, text=f"  {glyph}   {label}", anchor="w",
            height=34, corner_radius=9,
            fg_color=COLOR_SELECTED if active else "transparent",
            hover_color=COLOR_HOVER,
            text_color=COLOR_TEXT_HI if active else COLOR_TEXT_MID,
            font=ctk.CTkFont(family=FONT_UI, size=12),
            command=cmd,
        )
        b.pack(fill="x", padx=10, pady=1)
        return b

    def _build_sidebar(self):
        s = self.sidebar
        ctk.CTkLabel(
            s, text="  AgentBot",
            font=ctk.CTkFont(family=FONT_UI, size=14, weight="bold"),
            text_color=COLOR_TEXT_HI,
        ).pack(fill="x", pady=(14, 10))

        ctk.CTkButton(
            s, text="  \uff0b   New chat", anchor="w", height=36,
            corner_radius=10, fg_color=COLOR_BTN_PRIMARY,
            hover_color=gw.COLOR_ACCENT_HOVER, text_color="#06281a",
            font=ctk.CTkFont(family=FONT_UI, size=12, weight="bold"),
            command=self._new_chat,
        ).pack(fill="x", padx=10, pady=(0, 14))

        ctk.CTkLabel(
            s, text="  CONVERSATION",
            font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
            text_color=COLOR_TEXT_LOW,
        ).pack(fill="x", pady=(0, 5))
        self._nav_btn(s, "\U0001f4ac", "Current chat", lambda: None, active=True)

        ctk.CTkFrame(s, fg_color=COLOR_BORDER, height=1,
                     corner_radius=0).pack(fill="x", padx=10, pady=12, side="bottom")

        bottom = ctk.CTkFrame(s, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", pady=(0, 10))
        self._nav_btn(bottom, "\u2699", "Settings", self.open_settings_dialog)
        self._nav_btn(bottom, "\U0001f4c5", "Tasks", self.open_schedule_dialog)
        self._nav_btn(bottom, "\U0001f4da", "Index docs", self.on_index_documents)
        self._nav_btn(bottom, "\U0001f50d", "Activity log", self.toggle_log)
        self._nav_btn(bottom, "\U0001f4cb", "Copy all", self._copy_all)
        self._nav_btn(bottom, "\U0001f4e4", "Export chat", self.export_conversation)

    def toggle_sidebar(self):
        if self.sidebar_visible:
            self.sidebar.pack_forget()
            self.sidebar_visible = False
        else:
            self.sidebar.pack(side="left", fill="y", before=self.main_col)
            self.sidebar_visible = True
        self._reflow_chat()

    def _new_chat(self):
        self.clear_chat()

    def _reflow_chat(self):
        if self._reflowing:
            return
        self._reflowing = True
        try:
            w = self.chat_frame.winfo_width()
            if w < 40:
                return
            if self._char_px is None:
                f = tkfont.Font(font=(FONT_UI, 12))
                sample = "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJ 0123456789"
                self._char_px = max(4.0, f.measure(sample) / len(sample))
            chars = max(38, int((w - 46) / self._char_px))
            key = (chars, w)
            if key == self._last_key:
                return
            self._last_key = key
            for widget in self._md_widgets:
                try:
                    widget.reflow(chars)
                except Exception:
                    pass
        finally:
            self._reflowing = False

    def _settle_chat(self):
        self._last_key = None
        self._reflow_chat()
        for widget in self._md_widgets:
            try:
                widget.fit_height()
            except Exception:
                pass

    def _reflow_new(self, widget):
        """Apply the current wrap width to a widget added after the last reflow.

        `_reflow_chat` early-returns when the pane width is unchanged, so a
        freshly created MdText/CodeBlock would otherwise keep its default
        width (92 chars) and compute its height for the wrong wrap — clipping
        the bubble until some later resize forces a reflow.
        """
        try:
            if self._last_key is not None:
                widget.reflow(self._last_key[0])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Settings dialog
    # ------------------------------------------------------------------
    def _show_settings_overlay(self):
        for child in self._settings_overlay.winfo_children():
            child.destroy()
        self._settings_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._settings_overlay.lift()

    def _hide_settings_overlay(self):
        self._settings_overlay.place_forget()

    def open_settings_dialog(self):
        try:
            import config
        except ImportError:
            messagebox.showerror(
                "Settings",
                "config.py not found. Create it in the AgentBot folder."
            )
            return

        cfg = config.load_config()

        self._show_settings_overlay()
        win = ctk.CTkFrame(self._settings_overlay, fg_color=COLOR_BG,
                           corner_radius=0)
        win.pack(fill="both", expand=True)

        bar = ctk.CTkFrame(win, fg_color=COLOR_SIDEBAR, corner_radius=0,
                           height=HEADER_H)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._icon_btn(bar, "\u2190", self._hide_settings_overlay,
                       size=14).pack(side="left", padx=(10, 4))
        ctk.CTkLabel(
            bar, text="Settings",
            font=ctk.CTkFont(family=FONT_UI, size=15, weight="bold"),
            text_color=COLOR_TEXT_HI,
        ).pack(side="left", padx=6)
        ctk.CTkLabel(
            bar, text=f"saved to {config.CONFIG_PATH}",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color=COLOR_TEXT_LOW,
        ).pack(side="left", padx=14)

        tabs = ctk.CTkTabview(
            win,
            fg_color=COLOR_BG_ALT,
            segmented_button_fg_color=COLOR_BG_ALT,
            segmented_button_selected_color=COLOR_BTN_PRIMARY,
            segmented_button_selected_hover_color="#1d4ed8",
            segmented_button_unselected_color=COLOR_BTN_GHOST,
            segmented_button_unselected_hover_color="#3a3f52",
            text_color="#e5e7eb",
            corner_radius=10,
        )
        tabs.pack(fill="both", expand=True, padx=14, pady=(10, 8))

        tab_keys     = tabs.add("API Keys")
        tab_chat     = tabs.add("Chat")
        tab_voice    = tabs.add("Voice")
        tab_look     = tabs.add("Appearance")
        tab_telegram = tabs.add("Telegram")
        tab_advanced = tabs.add("Advanced")

        entries = {}

        def make_field(parent, label, key, secret=False, width=380):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=4, padx=6)

            ctk.CTkLabel(
                row, text=label,
                font=ctk.CTkFont(family=FONT_UI, size=12),
                width=180, anchor="w",
            ).pack(side="left", padx=(0, 8))

            entry = ctk.CTkEntry(
                row,
                font=ctk.CTkFont(family="Consolas", size=11),
                fg_color=COLOR_INPUT_BG,
                border_color="#2a2e3d",
                show="•" if secret else "",
                width=width,
            )
            entry.pack(side="left", fill="x", expand=True)
            entry.insert(0, cfg.get(key, ""))
            entries[key] = entry
            return entry

        def make_dropdown(parent, label, key, choices):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=4, padx=6)

            ctk.CTkLabel(
                row, text=label,
                font=ctk.CTkFont(family=FONT_UI, size=12),
                width=180, anchor="w",
            ).pack(side="left", padx=(0, 8))

            saved = str(cfg.get(key, choices[0]))
            if saved not in choices:
                choices = list(choices) + [saved]

            var = ctk.StringVar(value=saved)
            ctk.CTkOptionMenu(
                row,
                values=choices,
                variable=var,
                width=200,
                fg_color=COLOR_INPUT_BG,
                button_color=COLOR_BTN_GHOST,
                button_hover_color="#3a3f52",
            ).pack(side="left")

            entries[key] = var
            return var

        def make_section(parent, text):
            ctk.CTkLabel(
                parent, text=text,
                font=ctk.CTkFont(family=FONT_UI, size=13, weight="bold"),
                anchor="w",
                text_color="#9ca3af",
            ).pack(fill="x", padx=6, pady=(14, 6))

        # ---------- API Keys tab ----------
        make_section(tab_keys, "Groq")
        make_field(tab_keys, "API key", "GROQ_API_KEY", secret=True)

        make_section(tab_keys, "Google Gemini")
        make_field(tab_keys, "API key", "GEMINI_API_KEY", secret=True)

        make_section(tab_keys, "OpenRouter")
        make_field(tab_keys, "API key", "OPENROUTER_API_KEY", secret=True)

        make_section(tab_keys, "Cerebras")
        make_field(tab_keys, "API key", "CEREBRAS_API_KEY", secret=True)

        make_section(tab_keys, "Mistral")
        make_field(tab_keys, "API key", "MISTRAL_API_KEY", secret=True)

        make_section(tab_keys, "Cloudflare Workers AI")
        make_field(tab_keys, "API key", "CLOUDFLARE_API_KEY", secret=True)
        make_field(tab_keys, "Account ID", "CLOUDFLARE_ACCOUNT_ID")

        make_section(tab_keys, "Cohere")
        make_field(tab_keys, "API key", "COHERE_API_KEY", secret=True)

        make_section(tab_keys, "HuggingFace")
        make_field(tab_keys, "API key", "HUGGINGFACE_API_KEY", secret=True)

        make_section(tab_keys, "DeepSeek")
        make_field(tab_keys, "API key", "DEEPSEEK_API_KEY", secret=True)

        # ---------- Chat tab ----------
        make_section(tab_chat, "Input")
        make_dropdown(tab_chat, "Enter key behaviour", "ENTER_SENDS",
                      ["true", "false"])
        ctk.CTkLabel(
            tab_chat,
            text="true = Enter sends  ·  false = Ctrl+Enter sends",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color="#6b7280",
            anchor="w",
        ).pack(fill="x", padx=6, pady=(0, 6))

        make_section(tab_chat, "Display")
        make_dropdown(tab_chat, "Typing indicator", "SHOW_TYPING_INDICATOR",
                      ["true", "false"])
        make_dropdown(tab_chat, "Show system messages", "SHOW_SYSTEM_MESSAGES",
                      ["true", "false"])
        make_dropdown(tab_chat, "Show timestamps", "SHOW_TIMESTAMPS",
                      ["true", "false"])
        make_dropdown(tab_chat, "Autocorrect outgoing messages", "AUTOCORRECT_ENABLED",
                      ["true", "false"])

        make_section(tab_chat, "History")
        make_field(tab_chat, "Conversation history length", "HISTORY_LENGTH")
        ctk.CTkButton(
            tab_chat, text="Reset conversation memory",
            height=32, corner_radius=8,
            fg_color=COLOR_BTN_DANGER, hover_color="#b91c1c",
            font=ctk.CTkFont(family=FONT_UI, size=12, weight="bold"),
            command=self.reset_memory,
        ).pack(fill="x", padx=6, pady=(4, 2))
        ctk.CTkLabel(
            tab_chat,
            text="Forgets the saved history only — the chat on screen is kept.",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color="#6b7280",
            anchor="w",
        ).pack(fill="x", padx=6, pady=(0, 6))

        # ---------- Voice tab ----------
        make_section(tab_voice, "Voice output")
        make_dropdown(tab_voice, "Voice on", "TTS_ENABLED", ["true", "false"])
        make_dropdown(tab_voice, "Auto-speak replies", "TTS_AUTO_SPEAK",
                      ["true", "false"])

        VOICE_CHOICES = [
            "af_heart",
            "af_bella",
            "af_nicole",
            "af_sarah",
            "am_michael",
            "am_adam",
            "bf_emma",
            "bf_isabella",
            "bm_george",
            "bm_lewis",
        ]
        make_dropdown(tab_voice, "Voice", "TTS_VOICE", VOICE_CHOICES)
        make_dropdown(tab_voice, "Speed", "TTS_SPEED",
                      ["0.75", "0.9", "1.0", "1.1", "1.25", "1.5"])
        make_dropdown(tab_voice, "Language", "TTS_LANG", ["a", "b"])
        make_field(tab_voice, "Max characters per reply", "TTS_MAX_CHARS")

        ctk.CTkLabel(
            tab_voice,
            text=("a = American voices (af_, am_)   b = British voices (bf_, bm_)\n"
                  "Voice name must match language, or Kokoro errors."),
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color="#6b7280",
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=6, pady=(6, 6))

        def test_voice():
            try:
                tmp_cfg = dict(cfg)
                for k, e in entries.items():
                    try:
                        tmp_cfg[k] = str(e.get()).strip()
                    except Exception:
                        pass
                config.save_config(tmp_cfg)

                import importlib
                import voice_output
                importlib.reload(voice_output)
                voice_output.warmup()

                threading.Thread(
                    target=voice_output.speak,
                    args=("This is how I sound. Speed and voice applied.",),
                    daemon=True,
                ).start()
            except Exception as e:
                messagebox.showerror("Test failed", str(e))

        ctk.CTkButton(
            tab_voice, text="▶  Test voice", width=140,
            fg_color=COLOR_BTN_PRIMARY, hover_color="#1d4ed8",
            command=test_voice,
        ).pack(padx=6, pady=(10, 6), anchor="w")

        # ---------- Appearance tab ----------
        make_section(tab_look, "Theme")
        make_dropdown(tab_look, "Appearance mode", "THEME",
                      ["dark", "light", "system"])

        make_section(tab_look, "Chat bubbles")
        make_dropdown(tab_look, "Font size", "FONT_SIZE",
                      ["10", "11", "12", "13", "14", "15", "16"])
        make_dropdown(tab_look, "Max width (% of window)", "BUBBLE_WIDTH",
                      ["50", "60", "65", "72", "80", "90"])
        make_dropdown(tab_look, "Corner radius", "BUBBLE_RADIUS",
                      ["0", "8", "12", "16", "20", "24"])

        ctk.CTkLabel(
            tab_look,
            text="Appearance changes require a restart.",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color="#6b7280",
            anchor="w",
        ).pack(fill="x", padx=6, pady=(10, 6))

        # ---------- Telegram tab ----------
        make_section(tab_telegram, "Bot API (notifications to you)")
        make_field(tab_telegram, "Bot token", "TELEGRAM_BOT_TOKEN", secret=True)
        make_field(tab_telegram, "Your user ID", "TELEGRAM_USER_ID")

        make_section(tab_telegram, "Personal account (message contacts)")
        make_field(tab_telegram, "API ID", "TELEGRAM_API_ID")
        make_field(tab_telegram, "API hash", "TELEGRAM_API_HASH", secret=True)

        ctk.CTkLabel(
            tab_telegram,
            text=("Get API ID and hash from https://my.telegram.org\n"
                  "After saving, run Extras\\telegram_login.bat once."),
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color="#6b7280",
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=6, pady=(10, 6))

       
        # ---------- Advanced tab ----------
        make_section(tab_advanced, "Brain")
        make_field(tab_advanced, "Provider priority", "AGENT_BRAIN_PRIORITY")
        make_field(tab_advanced, "Max steps per turn", "MAX_STEPS_PER_TURN")
        make_field(tab_advanced, "Temperature (0.0-1.0)", "TEMPERATURE")

        make_section(tab_advanced, "Ollama (local)")
        make_field(tab_advanced, "Server URL", "OLLAMA_URL")
        make_field(tab_advanced, "Model name", "OLLAMA_MODEL")
        make_field(tab_advanced, "Timeout (seconds)", "OLLAMA_TIMEOUT")

        make_section(tab_advanced, "Other")
        make_dropdown(tab_advanced, "Check for updates", "CHECK_UPDATES",
                      ["true", "false"])
        make_dropdown(tab_advanced, "Log level", "LOG_LEVEL",
                      ["info", "debug", "off"])

        # ---------- Bottom buttons ----------
        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 14))

        def do_save():
            new_cfg = dict(cfg)
            for key, widget in entries.items():
                try:
                    new_cfg[key] = str(widget.get()).strip()
                except Exception:
                    pass
            ok, err = config.save_config(new_cfg)
            if ok:
                messagebox.showinfo(
                    "Saved",
                    f"Saved to:\n{config.CONFIG_PATH}\n\n"
                    f"Restart AgentBot for changes to take effect."
                )
                self._hide_settings_overlay()
            else:
                messagebox.showerror("Save failed", err)
        def do_test():
            new_cfg = dict(cfg)
            for key, widget in entries.items():
                try:
                    new_cfg[key] = str(widget.get()).strip()
                except Exception:
                    pass
            ok, err = config.save_config(new_cfg)
            if not ok:
                messagebox.showerror("Test failed", err)
                return
            try:
                import subprocess, sys
                r = subprocess.run(
                    [sys.executable, "-c",
                     "import agent; print(agent.list_brains())"],
                    capture_output=True, text=True, timeout=60,
                )
                out = (r.stdout or "") + (r.stderr or "")
                messagebox.showinfo("Brain status", out[:2000] or "(no output)")
            except Exception as e:
                messagebox.showerror("Test failed", str(e))

        def do_reset():
            if not messagebox.askyesno(
                "Reset settings",
                "Reset ALL settings to defaults?\n"
                "This wipes your API keys too."
            ):
                return
            ok, err = config.save_config(dict(config.DEFAULTS))
            if ok:
                messagebox.showinfo("Reset", "Settings reset to defaults.")
                self._hide_settings_overlay()
            else:
                messagebox.showerror("Reset failed", err)

        def do_export():
            path = filedialog.asksaveasfilename(
                title="Export settings",
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                import shutil
                shutil.copy(config.CONFIG_PATH, path)
                messagebox.showinfo("Exported", f"Saved to:\n{path}")
            except Exception as e:
                messagebox.showerror("Export failed", str(e))

        def do_import():
            path = filedialog.askopenfilename(
                title="Import settings",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                import json as _json
                with open(path, "r", encoding="utf-8") as f:
                    imported = _json.load(f)
                merged = dict(cfg)
                merged.update(imported)
                ok, err = config.save_config(merged)
                if ok:
                    messagebox.showinfo("Imported", "Settings imported. Restart to apply.")
                    self._hide_settings_overlay()
                else:
                    messagebox.showerror("Import failed", err)
            except Exception as e:
                messagebox.showerror("Import failed", str(e))

        def open_config_dir():
            try:
                os.startfile(os.path.dirname(config.CONFIG_PATH))
            except Exception as e:
                messagebox.showerror("Error", str(e))

        ctk.CTkButton(
            btn_row, text="Reset", width=90,
            fg_color=COLOR_BTN_DANGER, hover_color="#b91c1c",
            command=do_reset
        ).pack(side="left")

        ctk.CTkButton(
            btn_row, text="Import", width=90,
            fg_color=COLOR_BTN_GHOST, hover_color="#3a3f52",
            command=do_import
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Export", width=90,
            fg_color=COLOR_BTN_GHOST, hover_color="#3a3f52",
            command=do_export
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Open folder", width=110,
            fg_color=COLOR_BTN_GHOST, hover_color="#3a3f52",
            command=open_config_dir
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Close", width=80,
            fg_color=COLOR_BTN_GHOST, hover_color="#3a3f52",
            command=self._hide_settings_overlay
        ).pack(side="right")

        ctk.CTkButton(
            btn_row, text="Save", width=90,
            fg_color=COLOR_BTN_PRIMARY, hover_color="#1d4ed8",
            command=do_save
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Test connection", width=130,
            fg_color=COLOR_BTN_PRIMARY, hover_color="#1d4ed8",
            command=do_test
        ).pack(side="right", padx=(8, 0))

    # ------------------------------------------------------------------
    # Input helpers
    # ------------------------------------------------------------------
    def _on_send_key(self, event=None):
        if self._slash_visible:
            return self._on_slash_enter(event)
        self.send()
        return "break"

    def _on_newline_key(self, event=None):
        try:
            self.entry.insert("insert", "\n")
        except Exception:
            pass
        return "break"

    def _autogrow_entry(self, event=None):
        try:
            try:
                result = self.entry.count("1.0", "end-1c", "displaylines")
                display_lines = result[0] if isinstance(result, tuple) else result
            except Exception:
                display_lines = self.entry.get("1.0", "end-1c").count("\n") + 1
            target_lines = max(1, min(8, display_lines))
            new_height = target_lines * 20
            if abs(self.entry.winfo_height() - new_height) > 4:
                self.entry.configure(height=new_height)
        except Exception:
            pass

    def _get_entry_text(self):
        try:
            return self.entry.get("1.0", "end-1c").strip()
        except Exception:
            return ""

    def _clear_entry(self):
        try:
            self.entry.delete("1.0", "end")
        except Exception:
            pass

    def _set_entry_text(self, text):
        try:
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Slash command autocomplete popup
    # ------------------------------------------------------------------
    def _build_slash_popup(self):
        """Create the popup Toplevel (once) and return it."""
        if self._slash_popup is not None:
            return self._slash_popup
        pop = ctk.CTkToplevel(self.root)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(fg_color=COLOR_INPUT_BG)
        # Border via a wrapper frame
        wrapper = ctk.CTkFrame(pop, fg_color=COLOR_INPUT_BG, corner_radius=10,
                               border_width=1, border_color="#2a2e3d")
        wrapper.pack(fill="both", expand=True, padx=1, pady=1)
        # Rows container
        pop._rows_frame = ctk.CTkFrame(wrapper, fg_color="transparent")
        pop._rows_frame.pack(fill="both", expand=True, padx=4, pady=4)
        pop.withdraw()  # hidden until needed
        self._slash_popup = pop
        return pop

    def _hide_slash_popup(self):
        if self._slash_popup is None:
            return
        try:
            self._slash_popup.withdraw()
        except Exception:
            pass
        self._slash_visible = False

    def _show_slash_popup(self, matches):
        pop = self._build_slash_popup()
        # Clear old rows
        for w in pop._rows_frame.winfo_children():
            w.destroy()
        if not matches:
            self._hide_slash_popup()
            return
        self._slash_matches = matches
        self._slash_index = 0
        # Build one row per match (cap at 5 visible)
        for i, item in enumerate(matches[:5]):
            row = ctk.CTkFrame(pop._rows_frame, fg_color="transparent",
                               corner_radius=6, height=28)
            row.pack(fill="x", pady=1)
            row.pack_propagate(False)
            # Left icon
            ctk.CTkLabel(row, text=item["icon"], width=22,
                         font=ctk.CTkFont(family=FONT_UI, size=13)
                         ).pack(side="left", padx=(6, 2))
            # Command name
            ctk.CTkLabel(row, text=item["cmd"], width=150, anchor="w",
                         font=ctk.CTkFont(family="Consolas", size=12, weight="bold"),
                         text_color="#e6e8ec"
                         ).pack(side="left", padx=(0, 6))
            # Description
            ctk.CTkLabel(row, text=item["desc"], anchor="w",
                         font=ctk.CTkFont(family=FONT_UI, size=11),
                         text_color="#9ca3af"
                         ).pack(side="left", fill="x", expand=True, padx=(0, 6))
            # Store index on the row for click handlers
            row._slash_index = i
            # Click handler
            for w in (row,) + tuple(row.winfo_children()):
                w.bind("<Button-1>", lambda e, idx=i: self._on_slash_click(idx))
                # Hover
                w.bind("<Enter>", lambda e, idx=i: self._on_slash_hover(idx))
        # Highlight the first row
        self._highlight_slash_row(0)
        # Position above entry
        self._position_slash_popup()
        pop.deiconify()
        pop.lift()
        self._slash_visible = True

    def _position_slash_popup(self):
        """Place popup above the entry, left-aligned."""
        if self._slash_popup is None:
            return
        self.root.update_idletasks()
        ex = self.entry.winfo_rootx()
        ey = self.entry.winfo_rooty()
        # Width: same as entry_wrap (the CTkFrame around the textbox)
        ew = self.entry.master.winfo_width()
        # Compute popup height from row count
        n = min(len(self._slash_matches), 5)
        ph = n * 30 + 16  # row height 30 + padding
        self._slash_popup.geometry(f"{ew}x{ph}+{ex}+{ey - ph - 6}")

    def _highlight_slash_row(self, idx):
        """Change the bg colour of the selected row."""
        pop = self._slash_popup
        if pop is None:
            return
        rows = pop._rows_frame.winfo_children()
        for i, row in enumerate(rows):
            try:
                if i == idx:
                    row.configure(fg_color=COLOR_BTN_PRIMARY)
                else:
                    row.configure(fg_color="transparent")
            except Exception:
                pass

    def _on_slash_hover(self, idx):
        self._slash_index = idx
        self._highlight_slash_row(idx)

    def _on_slash_click(self, idx):
        if 0 <= idx < len(self._slash_matches):
            self._insert_slash_command(self._slash_matches[idx]["cmd"])

    def _maybe_show_slash_popup(self):
        """Called on every keystroke. Decides if popup should show/hide."""
        try:
            content = self.entry.get("1.0", "end-1c")
        except Exception:
            return
        # Only when: content starts with '/', no newline, no attachments
        if (not content.startswith("/")
                or "\n" in content
                or self.attachments):
            self._hide_slash_popup()
            return
        # If content has a space → user has moved to args, hide popup
        if " " in content:
            self._hide_slash_popup()
            return
        query = content[1:]  # drop the leading /
        # Score each command
        scored = []
        for item in SLASH_COMMANDS:
            s = _fuzzy_score(query, item["cmd"])
            if s > 0:
                scored.append((s, item))
        # If empty query, sort by history first
        if not query:
            history = _load_slash_history()
            def sort_key(pair):
                s, item = pair
                try:
                    rank = history.index(item["cmd"])
                except ValueError:
                    rank = 999
                return (rank, item["cmd"])
            scored.sort(key=sort_key)
        else:
            scored.sort(key=lambda p: -p[0])
        matches = [item for _, item in scored]
        if matches:
            self._show_slash_popup(matches)
        else:
            self._hide_slash_popup()

    def _on_slash_up(self, event=None):
        if not self._slash_visible:
            return None
        self._slash_index = max(0, self._slash_index - 1)
        self._highlight_slash_row(self._slash_index)
        return "break"

    def _on_slash_down(self, event=None):
        if not self._slash_visible:
            return None
        self._slash_index = min(
            len(self._slash_matches) - 1, self._slash_index + 1
        )
        self._highlight_slash_row(self._slash_index)
        return "break"

    def _on_slash_tab(self, event=None):
        if not self._slash_visible:
            return None
        if 0 <= self._slash_index < len(self._slash_matches):
            cmd = self._slash_matches[self._slash_index]["cmd"]
            self._insert_slash_command(cmd, execute_if_no_args=True)
        return "break"

    def _on_slash_enter(self, event=None):
        if not self._slash_visible:
            return None  # let the normal send handler run
        if 0 <= self._slash_index < len(self._slash_matches):
            cmd = self._slash_matches[self._slash_index]["cmd"]
            self._insert_slash_command(cmd)
        return "break"

    def _on_slash_escape(self, event=None):
        if not self._slash_visible:
            return None
        self._hide_slash_popup()
        return "break"

    def _insert_slash_command(self, cmd, execute_if_no_args=False):
        """Insert the command into the entry (with trailing space if it takes args)."""
        # Find the command item
        item = next((c for c in SLASH_COMMANDS if c["cmd"] == cmd), None)
        takes_args = bool(item and item["args"])
        # Replace the entry's content
        try:
            self.entry.delete("1.0", "end")
            if takes_args:
                self.entry.insert("1.0", cmd + " ")
            else:
                self.entry.insert("1.0", cmd)
            self.entry.focus()
        except Exception:
            pass
        self._hide_slash_popup()
        # Record in history
        _record_slash_use(cmd)
        # If no args and caller wants execution, trigger send
        if execute_if_no_args and not takes_args:
            self.send()

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    def _handle_slash_command(self, text):
        if not text.startswith("/"):
            return False

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/help", "/?"):
            self._add_bubble(
                "Slash commands — bypass the AI and go straight to the tool:\n\n"
                "/send  Contact|message\n"
                "       e.g.  /send JEE|helo\n\n"
                "/edit  Contact|old text|new text\n"
                "       e.g.  /edit JEE|helo|hello there\n\n"
                "/del   Contact|text             → preview\n"
                "/del   Contact|text|confirm     → delete\n"
                "/del   Contact|text|all         → preview all matches\n"
                "/del   Contact|text|all|confirm → delete all matches\n"
                "       e.g.  /del JEE|helo\n\n"
                "/dellast Contact       → delete latest outgoing message\n\n"
                "Use 'me' as contact for Saved Messages.\n\n"
                "File commands — no AI, straight to the tool:\n\n"
                "/find  Folder|pat1,pat2\n"
                "       e.g.  /find telegram desktop|SPM,CTU,LCC\n"
                "       Folder alone works too:  /find SPM,CTU\n\n"
                "/move  Source|pat1,pat2|Dest            → preview\n"
                "/move  Source|pat1,pat2|Dest|confirm    → execute\n"
                "       e.g.  /move downloads|SPM,CTU|semester1\n\n"
                "/mkdir Name       → creates under Downloads (or give a full path)\n\n"
                "Code inspection — read-only, cannot modify anything:\n\n"
                "/symbols file.py  → every class/function with line numbers\n"
                "       e.g.  /symbols config.py\n\n"
                "/map   [Folder|max_files]  → symbol map of a whole folder\n"
                "       e.g.  /map agentbot|40     (no args = the AgentBot folder)\n\n"
                "Browser recipes — deterministic click/type scripts:\n\n"
                "/recipe Name      → run a recipe from recipes.json\n"
                "       e.g.  /recipe clock_out\n\n"
                "Security scanning — static analysis, no AI:\n\n"
                "/scan  File        → scan a file (hash, entropy, verdict)\n"
                "       e.g.  /scan C:\\Users\\USER\\Downloads\\file.exe\n\n"
                "/quarantine File   → move a file to Downloads\\Quarantine\\\n\n"
                "/quarantine-list   → list quarantined files\n\n"
                "/rag   query       → search your indexed documents\n\n"
                "Group GIF library — memes the bot stored from Telegram:\n\n"
                "/gif-stats         → how many GIFs, occurrences and labels\n\n"
                "/label-gifs        → label every unlabeled GIF (costs API calls)\n"
                "/label-gifs 50     → label the 50 most-reused only\n"
                "/label-gifs min 2  → only GIFs sent 2+ times (cheapest start)\n\n"
                "Telegram group export (private, stays outside the repo):\n\n"
                "/import-chat <result.json>\n"
                "       → parse a Telegram Desktop export into scrubbed monthly\n"
                "         files under C:\\Users\\USER\\PrivateExport\n\n"
                "/reindex           → re-index RAG_AUTO_INDEX_FOLDERS",
                "system",
            )
            return True

        # File commands run before the telegram_user import so they still work
        # when Telethon is missing or not logged in.
        if cmd == "/find":
            if not args:
                result = "ERROR: format is /find Folder|pat1,pat2"
            else:
                result = agent.find_files(args)
            return self._finish_slash_command(text, result)

        if cmd == "/move":
            if args.count("|") < 2:
                result = "ERROR: format is /move Source|pat1,pat2|Dest (add |confirm to execute)"
            else:
                result = agent.move_files(args)
            return self._finish_slash_command(text, result)

        if cmd == "/mkdir":
            if not args:
                result = "ERROR: format is /mkdir Name"
            else:
                result = agent.make_folder(args)
            return self._finish_slash_command(text, result)

        # Code inspection — both read-only, neither can modify anything.
        if cmd == "/symbols":
            if not args:
                result = "ERROR: format is /symbols file.py"
            else:
                result = agent.list_symbols(args)
            return self._finish_slash_command(text, result)

        if cmd == "/map":
            target = args or os.path.dirname(os.path.abspath(agent.__file__))
            result = agent.repo_map(target)
            return self._finish_slash_command(text, result)

        if cmd == "/recipe":
            if not args:
                result = "ERROR: format is /recipe Name"
            else:
                from recipes import run_recipe
                result = run_recipe(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/scan":
            if not args:
                result = "ERROR: format is /scan <file_path>"
            else:
                import security_tools
                result = security_tools.scan_file(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/quarantine":
            if not args:
                result = "ERROR: format is /quarantine <file_path>"
            else:
                import security_tools
                result = security_tools.quarantine_file(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/quarantine-list":
            import security_tools
            result = security_tools.list_quarantine()
            return self._finish_slash_command(text, result)

        if cmd == "/rag":
            if not args:
                result = "ERROR: format is /rag <query>"
            else:
                from rag_tool import search_documents
                result = search_documents(args.strip())
            return self._finish_slash_command(text, result)

        if cmd == "/import-chat":
            if not args.strip():
                self._add_bubble(
                    "Usage: /import-chat <path to result.json>\n"
                    "Parses a Telegram Desktop export into scrubbed monthly "
                    "files under C:\\Users\\USER\\PrivateExport.", "system")
                return True
            try:
                import telegram_export_parser  # noqa: F401
            except Exception as e:
                self._add_bubble(text, "user")
                self._add_bubble(f"ERROR: {e}", "system")
                return True

            self._add_bubble(text, "user")
            self._add_system_bubble("📥 Parsing and scrubbing Telegram export…")
            self.set_status("Parsing export…", busy=True)

            def work():
                try:
                    import telegram_export_parser as tep
                    r = tep.parse_json_export(args.strip())
                    msg = (
                        f"✅ Chat import complete:\n"
                        f"  Chat: {r['chat_name']}\n"
                        f"  Files written: {r['files_written']}\n"
                        f"  Messages indexed: {r['total_messages']}\n"
                        f"  Dropped (address/blocklist): {r['dropped_lines']}\n"
                        f"  Redacted: {r['modified_lines']}\n"
                        f"  Output: {r['output_dir']}\n\n"
                        f"Review the scrub report before indexing:\n"
                        f"  {r['report_path']}\n\n"
                        f"Run /reindex to add it to RAG."
                    )
                except Exception as e:
                    msg = f"❌ Import failed: {e}"
                try:
                    self.root.after(0, lambda m=msg: self._add_system_bubble(m))
                except Exception:
                    print("[import-chat] finished; could not post result to UI")

            threading.Thread(target=work, daemon=True).start()
            return True

        if cmd == "/reindex":
            try:
                import config  # noqa: F401
                import rag_tool  # noqa: F401
            except Exception as e:
                self._add_bubble(text, "user")
                self._add_bubble(f"ERROR: {e}", "system")
                return True

            self._add_bubble(text, "user")
            self._add_system_bubble("🔍 Re-indexing configured folders…")
            self.set_status("Re-indexing…", busy=True)

            def work():
                try:
                    import config
                    import rag_tool
                    cfg = config.load_config()
                    folders = cfg.get("RAG_AUTO_INDEX_FOLDERS", [])
                    if isinstance(folders, str):
                        folders = [f.strip() for f in folders.split(",") if f.strip()]
                    lines = []
                    skipped = 0
                    for folder in folders:
                        if not os.path.isdir(folder):
                            skipped += 1
                            continue
                        try:
                            res = rag_tool.index_documents(folder)
                            first = (res or "done").splitlines()[0] if res else "done"
                        except Exception as e:
                            first = f"failed: {e}"
                        # Only the folder name, never its contents.
                        lines.append(f"  {os.path.basename(folder) or folder}: {first}")
                    msg = "✅ Reindex complete:\n" + "\n".join(lines)
                    if skipped:
                        msg += f"\n  ({skipped} configured folder(s) not found, skipped)"
                except Exception as e:
                    msg = f"❌ Reindex failed: {e}"
                try:
                    self.root.after(0, lambda m=msg: self._add_system_bubble(m))
                except Exception:
                    print("[reindex] finished; could not post result to UI")

            threading.Thread(target=work, daemon=True).start()
            return True

        if cmd == "/gif-stats":
            try:
                import gif_library
                result = gif_library.library_stats()
            except Exception as e:
                result = f"ERROR: {e}"
            return self._finish_slash_command(text, result)

        if cmd == "/label-gifs":
            # /label-gifs        -> every unlabeled GIF
            # /label-gifs 50     -> the 50 most-reused
            # /label-gifs min 2  -> only GIFs used 2+ times
            parts_l = args.split()
            limit = None
            min_occ = 1
            if len(parts_l) == 1 and parts_l[0].isdigit():
                limit = int(parts_l[0])
            elif len(parts_l) == 2 and parts_l[0].lower() == "min":
                if not parts_l[1].isdigit():
                    self._add_bubble(text, "user")
                    self._add_bubble(
                        "ERROR: /label-gifs min N — N must be a number", "system")
                    return True
                min_occ = int(parts_l[1])

            try:
                import gif_library  # noqa: F401  (fail fast if unavailable)
            except Exception as e:
                self._add_bubble(text, "user")
                self._add_bubble(f"ERROR: {e}", "system")
                return True

            # Labeling costs an API call per GIF and runs for minutes, so it
            # goes on a worker thread with the result pushed back on the UI
            # thread via root.after — same shape as indexing.
            self._add_bubble(text, "user")
            self._add_system_bubble("🏷️ Labeling GIFs… this can take a while.")
            self.set_status("Labeling GIFs…", busy=True)

            def work():
                try:
                    import gif_library
                    msg = gif_library.label_gifs(min_occurrences=min_occ,
                                                 limit=limit)
                except Exception as e:
                    msg = f"❌ Labeling failed: {e}"
                # Never let the callback itself be the thing that fails: if
                # posting the result back raises, the user is left with the
                # "Labeling…" bubble forever.
                try:
                    self.root.after(0, lambda m=msg: self._label_done(m))
                except Exception:
                    print(f"[label-gifs] {msg}")

            threading.Thread(target=work, daemon=True).start()
            return True

        try:
            import telegram_user
        except ImportError:
            self._add_bubble("ERROR: telegram_user module not found.", "system")
            return True

        result = None
        if cmd == "/send":
            if "|" not in args:
                result = "ERROR: format is /send Contact|message"
            else:
                result = telegram_user.send_telegram_tool(args)
        elif cmd == "/edit":
            if args.count("|") < 2:
                result = "ERROR: format is /edit Contact|old text|new text"
            else:
                result = telegram_user.edit_tool(args)
        elif cmd in ("/del", "/delete"):
            if "|" not in args:
                result = "ERROR: format is /del Contact|text (add |confirm to delete)"
            else:
                result = telegram_user.delete_tool(args)
        elif cmd in ("/dellast", "/dl"):
            if not args:
                result = "ERROR: format is /dellast Contact"
            else:
                try:
                    result = telegram_user.delete_latest_tool(args.strip())
                except AttributeError:
                    result = "ERROR: telegram_user.delete_latest_tool not available."
        else:
            return False

        return self._finish_slash_command(text, result)

    def _finish_slash_command(self, text, result):
        if not isinstance(result, str):
            result = str(result) if result is not None else "(no result)"

        self._add_bubble(text, "user")
        kind = "system" if result.startswith("ERROR") or result.startswith("PREVIEW") else "agent"
        self._add_bubble(result, kind)

        self.conversation_history.append(f"User: {text}")
        self.conversation_history.append(f"System: {result}")
        try:
            agent.save_conversation_history(self.conversation_history)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # Natural-language fast-path
    # ------------------------------------------------------------------
    def _try_nl_fastpath(self, text):
        t = (text or "").strip()
        low = t.lower()
        if len(t) < 6:
            return False

        m = re.match(r"^send\s+(.+?)\s+to\s+(.+?)\s*$", t, re.IGNORECASE)
        if m:
            msg, contact = m.group(1).strip(), m.group(2).strip()
            if contact and len(contact) < 60:
                return self._dispatch_tg("send", contact, msg)

        m = re.match(r"^message\s+(.+?)\s+(?:saying|that|with)\s+(.+?)\s*$", t, re.IGNORECASE)
        if m:
            contact, msg = m.group(1).strip(), m.group(2).strip()
            if contact and len(contact) < 60:
                return self._dispatch_tg("send", contact, msg)

        m = re.match(
            r"^edit\s+(.+?)(?:'s)?\s+message\s+[\"']?(.+?)[\"']?\s+to\s+[\"']?(.+?)[\"']?\s*$",
            t, re.IGNORECASE
        )
        if m:
            contact, old, new = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            if contact and old and new and len(contact) < 60:
                return self._dispatch_tg("edit", contact, old, new)

        if low.startswith("edit ") and t.count("|") >= 2:
            return self._dispatch_tg("edit_raw", t[5:].strip())

        m = re.match(
            r"^delete\s+(.+?)(?:'s)?\s+message\s+[\"']?(.+?)[\"']?\s*$",
            t, re.IGNORECASE
        )
        if m:
            contact, old = m.group(1).strip(), m.group(2).strip()
            if contact and old and len(contact) < 60:
                return self._dispatch_tg("delete", contact, old)

        return False

    def _dispatch_tg(self, op, *args):
        try:
            import telegram_user
        except ImportError:
            self._add_bubble("ERROR: telegram_user module not found.", "system")
            return True

        if op == "send":
            contact, msg = args
            result = telegram_user.send_telegram_tool(f"{contact}|{msg}")
            display = f"send {contact}|{msg}"
        elif op == "edit":
            contact, old, new = args
            result = telegram_user.edit_tool(f"{contact}|{old}|{new}")
            display = f"edit {contact}|{old}|{new}"
        elif op == "edit_raw":
            result = telegram_user.edit_tool(args[0])
            display = f"edit {args[0]}"
        elif op == "delete":
            contact, old = args
            result = telegram_user.delete_tool(f"{contact}|{old}")
            display = f"delete {contact}|{old}"
        else:
            return False

        self._add_bubble(display, "user")
        kind = "system" if result.startswith("ERROR") or result.startswith("PREVIEW") else "agent"
        self._add_bubble(result, kind)

        self.conversation_history.append(f"User: {display}")
        self.conversation_history.append(f"System: {result}")
        try:
            agent.save_conversation_history(self.conversation_history)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    def _pick_files(self):
        paths = filedialog.askopenfilenames(
            title="Attach files",
            filetypes=[
                ("All supported",
                 "*.txt *.md *.py *.js *.ts *.json *.csv *.log "
                 "*.html *.xml *.yaml *.yml *.ini *.cfg *.sh *.bat *.ps1 *.sql "
                 "*.pdf *.docx *.xlsx *.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                ("Documents",   "*.pdf *.docx *.xlsx"),
                ("Text / code", "*.txt *.md *.py *.js *.ts *.json *.csv *.log *.html *.xml *.yaml *.yml"),
                ("Images",      "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                ("All files",   "*.*"),
            ],
        )
        if not paths:
            return
        for p in paths:
            if p not in self.attachments:
                self.attachments.append(p)
        self._render_attachments()

    def _render_attachments(self):
        for w in self.attach_strip.winfo_children():
            w.destroy()
        self._attach_thumbs = []
        if not self.attachments:
            self.attach_strip.pack_forget()
            return

        for i, path in enumerate(self.attachments):
            chip = ctk.CTkFrame(self.attach_strip, fg_color=COLOR_BTN_GHOST, corner_radius=10)
            chip.pack(side="left", padx=(0, 6), pady=2)

            name = os.path.basename(path)
            if len(name) > 32:
                name = name[:29] + "…"

            thumb = None
            if path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                try:
                    from PIL import Image
                    img = Image.open(path)
                    img.thumbnail((60, 60))
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                    self._attach_thumbs.append(ctk_img)
                    thumb = ctk.CTkLabel(chip, image=ctk_img, text="", width=60, height=60)
                    thumb.pack(side="left", padx=(8, 4), pady=4)
                except Exception:
                    thumb = None

            label_text = f"{name}" if thumb else f"📎 {name}"
            ctk.CTkLabel(
                chip, text=label_text,
                font=ctk.CTkFont(family=FONT_UI, size=11),
                text_color="#e5e7eb",
            ).pack(side="left", padx=(8, 4) if not thumb else (0, 4), pady=4)

            ctk.CTkButton(
                chip, text="✕", width=22, height=22,
                fg_color="transparent", hover_color="#3a3f52",
                font=ctk.CTkFont(size=11),
                command=lambda idx=i: self._remove_attachment(idx),
            ).pack(side="left", padx=(0, 4))

        self.attach_strip.pack(fill="x", padx=16, pady=(8, 0), before=self._input_row)

    def _remove_attachment(self, idx):
        if 0 <= idx < len(self.attachments):
            self.attachments.pop(idx)
        self._render_attachments()

    # ------------------------------------------------------------------
    # Bubbles (tk.Label based — wraps correctly)
    # ------------------------------------------------------------------
    def _add_bubble(self, text, kind):
        if kind == "system":
            try:
                import config
                if str(config.load_config().get("SHOW_SYSTEM_MESSAGES", "true")).lower() not in ("true", "1", "yes", "on"):
                    return
            except Exception:
                pass

        self.bubble_log.append((kind, text))

        if kind == "system":
            self._add_system_status(text, COLOR_SYS_TEXT)
            return

        if kind == "user":
            row = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=(9, 3))
            holder = ctk.CTkFrame(row, fg_color="transparent")
            holder.pack(side="right")
            bubble = ctk.CTkFrame(holder, fg_color=COLOR_USER_BUBBLE,
                                  corner_radius=16)
            bubble.pack()
            avail = max(220, int(self.root.winfo_width() * MAX_BUBBLE_FRACTION))
            label = tk.Label(
                bubble, text=text,
                font=(FONT_UI, BUBBLE_FONT_SIZE),
                fg=COLOR_USER_TEXT, bg=COLOR_USER_BUBBLE,
                justify="left", anchor="w",
                wraplength=max(140, avail - 28),
                padx=0, pady=0, bd=0, highlightthickness=0,
            )
            label.pack(padx=14, pady=8)
            self._bubble_labels.append(label)
            for w in (row, holder, bubble, label):
                w.bind("<Button-3>", lambda e, t=text: self._show_bubble_menu(e, t))
            if not self._user_scrolled_up:
                self._safe_scroll_to_bottom(delay=100)
            return

        # agent / scheduled — bubble-less rich text, full width
        fg = COLOR_SCHED_TEXT if kind == "scheduled" else COLOR_BOT_TEXT
        row = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(4, 8))

        made = []
        first_text = True
        for seg in gw.split_fences(text):
            if seg[0] == "code":
                block = gw.CodeBlock(row, seg[1], seg[2], self._copy_text)
                block.pack(fill="x", pady=(2, 6))
                self._reflow_new(block)
                made.append(block)
                self._md_widgets.append(block)
                continue
            chunk = seg[1]
            if kind == "scheduled" and first_text:
                chunk = "\U0001f4c5  " + chunk
            first_text = False
            if not chunk.strip():
                continue
            md = gw.MdText(row, chunk, bg=COLOR_BG, fg=fg)
            md.pack(fill="x", anchor="w", pady=(1, 3))
            self._reflow_new(md)
            made.append(md)
            self._md_widgets.append(md)

        COLLAPSE_AT = 6000
        if len(text) > COLLAPSE_AT:
            state = {"expanded": False}
            toggle_btn = ctk.CTkLabel(
                row, text="expand",
                font=ctk.CTkFont(family=FONT_UI, size=10, underline=True),
                text_color=COLOR_SYS_TEXT, cursor="hand2",
            )
            toggle_btn.pack(anchor="w")

            def toggle(event=None):
                state["expanded"] = not state["expanded"]
                toggle_btn.configure(text="collapse" if state["expanded"] else "expand")
                for w in made:
                    if isinstance(w, gw.MdText):
                        if state["expanded"]:
                            w.fit_height()
                        else:
                            w.configure(height=24)
                if not self._user_scrolled_up:
                    self._safe_scroll_to_bottom(delay=100)

            toggle_btn.bind("<Button-1>", toggle)
            for w in made:
                if isinstance(w, gw.MdText):
                    w.configure(height=24)

        for w in [row] + made:
            try:
                w.bind("<Button-3>", lambda e, t=text: self._show_bubble_menu(e, t))
            except Exception:
                pass

        if not self._user_scrolled_up:
            self._safe_scroll_to_bottom(delay=100)

    def _add_system_status(self, text, fg):
        row = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(6, 6))

        ctk.CTkLabel(
            row, text=text,
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color=fg,
            justify="center",
        ).pack(anchor="center")

    def _add_system_bubble(self, text):
        self._add_bubble(text, "system")

    def _add_activity(self, summary, detail):
        chip = gw.ActivityChip(self.chat_frame, summary, detail)
        chip.pack(fill="x", padx=18, pady=(2, 4))
        if not self._user_scrolled_up:
            self._safe_scroll_to_bottom(delay=100)

    def _update_wraplengths(self):
        try:
            avail = max(180, int(self.root.winfo_width() * MAX_BUBBLE_FRACTION))
            wl = max(100, avail - (BUBBLE_PAD_X * 2) - 6)
            for lbl in getattr(self, "_bubble_labels", []):
                try:
                    lbl.configure(wraplength=wl)
                except Exception:
                    pass
        except Exception:
            pass
        self._reflow_chat()

    def _on_root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._hide_slash_popup()
        try:
            if hasattr(self, "_resize_after_id"):
                self.root.after_cancel(self._resize_after_id)
        except Exception:
            pass
        self._resize_after_id = self.root.after(200, self._update_wraplengths)
        # A resize (windowed <-> fullscreen) forces Tk to re-lay out the whole
        # pane. Re-pin to the bottom afterwards so the view can't be left
        # showing empty space below the last message.
        self._safe_scroll_to_bottom(delay=150)

    # ------------------------------------------------------------------
    # Scroll
    # ------------------------------------------------------------------
    def _on_user_scroll(self, event=None):
        # CustomTkinter scrolls the canvas from a bind_all handler, which runs
        # *after* any per-widget binding. Reading the position here would see
        # the pre-scroll state, so re-read once the canvas has actually moved.
        try:
            self.root.after_idle(self._refresh_scroll_flag)
        except Exception:
            self._refresh_scroll_flag()

    def _refresh_scroll_flag(self):
        # Pixel-accurate, not a fraction of the content. CustomTkinter sets
        # yscrollincrement=1, so one wheel notch moves the canvas ~20px: a
        # percentage threshold would need a huge scroll on a long chat and the
        # flag would stay False, leaving the reader to be yanked down anyway.
        # More than a few px off the bottom means the user is reading history.
        try:
            canvas = self.chat_frame._parent_canvas
            bbox = canvas.bbox("all")
            if not bbox:
                return
            hidden_below = bbox[3] - (canvas.canvasy(0) + canvas.winfo_height())
            self._user_scrolled_up = hidden_below > 5
        except Exception:
            pass

    def _on_chat_canvas_configure(self, event=None):
        # Keep the scrollregion honest when the window is resized — CTk only
        # refreshes it from the inner frame's <Configure>, which does not fire
        # on pure canvas resizes (windowed / non-fullscreen mode).
        try:
            canvas = self.chat_frame._parent_canvas
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass
        self._reflow_chat()

    def _on_chat_content_configure(self, event=None):
        # Fires whenever the inner frame changes size, i.e. whenever a bubble
        # grows (MdText.fit_height runs asynchronously after it is mapped).
        # Refresh the scrollregion ONLY — never scroll from here. This fires
        # many times while a long reply renders, and scrolling on every firing
        # is what yanked the user back to the bottom mid-read.
        try:
            canvas = self.chat_frame._parent_canvas
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

    def _scroll_if_pinned(self):
        try:
            canvas = self.chat_frame._parent_canvas
            at_bottom = canvas.yview()[1] >= 0.995
        except Exception:
            at_bottom = True
        if at_bottom and not self._user_scrolled_up:
            self._safe_scroll_to_bottom(delay=50)

    def _scroll_to_bottom(self):
        self._user_scrolled_up = False
        self._safe_scroll_to_bottom(delay=50)

    def _safe_scroll_to_bottom(self, delay=100):
        """Scroll to bottom after layout settles. Handles CTkScrollableFrame
        lag where the scrollregion hasn't updated yet. Never moves the view if
        the user has scrolled up to read history."""
        # Bail out up front: no point scheduling timers the user would only be
        # dragged down by.
        if getattr(self, "_user_scrolled_up", False):
            return

        def do_scroll():
            # Re-check at fire time: the user may have scrolled up during the
            # delay, in which case we must leave them where they are.
            if getattr(self, "_user_scrolled_up", False):
                return
            try:
                canvas = self.chat_frame._parent_canvas
                # Force Tk to finish laying out the new content
                self.root.update_idletasks()
                # Recalculate the scrollable region based on current content
                bbox = canvas.bbox("all")
                if bbox:
                    canvas.configure(scrollregion=bbox)
                # Now the scrollregion is accurate — scroll to real bottom
                canvas.yview_moveto(1.0)
            except Exception as e:
                print(f"[scroll] {e}")

        # Content can keep growing after the first frame (MdText height
        # retries, code blocks, images), which would leave the scrollregion
        # stale and scroll into empty space. Pass several times and let only
        # the newest request's timers survive.
        for old in getattr(self, "_scroll_after_ids", []):
            try:
                self.root.after_cancel(old)
            except Exception:
                pass
        self._scroll_after_ids = [
            self.root.after(d, do_scroll)
            for d in (delay, delay + 150, delay + 350, delay + 700, delay + 1100)
        ]

    # ------------------------------------------------------------------
    # Copy / Save
    # ------------------------------------------------------------------
    def _copy_text(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            self._add_system_bubble("📋 Copied to clipboard")
        except Exception as e:
            print(f"[copy error] {e}")

    def _show_bubble_menu(self, event, text):
        menu = tk.Menu(
            self.root, tearoff=0,
            bg="#1a1d2e", fg="#e5e7eb",
            activebackground="#2563eb", activeforeground="#ffffff",
            borderwidth=0, font=(FONT_UI, 10)
        )
        menu.add_command(label="Copy message",
                         command=lambda t=text: self._copy_text(t))
        menu.add_command(label="Save as…",
                         command=lambda t=text: self._save_bubble_as(t))

        code, ext = _extract_first_code_block(text)
        if code:
            menu.add_command(
                label=f"Save code block as .{ext}…",
                command=lambda c=code, e=ext: self._save_code_block(c, e),
            )

        menu.add_separator()
        menu.add_command(label="Copy full conversation",
                         command=self._copy_all)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _save_bubble_as(self, text):
        default_name = "message_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Save message",
            initialfile=default_name,
            defaultextension=".txt",
            filetypes=[
                ("Text file",     "*.txt"),
                ("Markdown",      "*.md"),
                ("PDF document",  "*.pdf"),
                ("Word document", "*.docx"),
                ("All files",     "*.*"),
            ],
        )
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            import export_tools
            bubbles = [("agent", text)]
            if ext == ".pdf":
                export_tools.export_pdf(bubbles, path, "Message")
            elif ext == ".docx":
                export_tools.export_docx(bubbles, path, "Message")
            elif ext == ".md":
                export_tools.export_md(bubbles, path, "Message")
            else:
                export_tools.export_txt(bubbles, path, "Message")
            messagebox.showinfo("Saved", f"Saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _save_code_block(self, code, ext):
        default_name = "code_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "." + ext
        path = filedialog.asksaveasfilename(
            title="Save code block",
            initialfile=default_name,
            defaultextension="." + ext,
            filetypes=[
                (f"{ext.upper()} file", f"*.{ext}"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            messagebox.showinfo("Saved", f"Saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _copy_all(self):
        if not self.bubble_log:
            self._add_system_bubble("Nothing to copy")
            return
        lines = []
        for kind, text in self.bubble_log:
            if kind == "user":
                lines.append(f"You: {text}")
            elif kind == "agent":
                lines.append(f"Agent: {text}")
            elif kind == "scheduled":
                lines.append(f"[Scheduled] {text}")
            else:
                lines.append(f"— {text}")
        self._copy_text("\n\n".join(lines))

    def _copy_last_agent(self, event=None):
        try:
            if self.root.focus_get() is self.entry:
                return None
        except Exception:
            pass
        for kind, text in reversed(self.bubble_log):
            if kind == "agent":
                self._copy_text(text)
                return "break"
        self._add_system_bubble("No agent message to copy yet")
        return "break"

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_conversation(self):
        if not self.bubble_log:
            messagebox.showinfo("Export", "Nothing to export yet.")
            return

        default_name = "conversation_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        path = filedialog.asksaveasfilename(
            title="Export conversation",
            initialfile=default_name,
            defaultextension=".pdf",
            filetypes=[
                ("PDF document", "*.pdf"),
                ("Word document", "*.docx"),
                ("Markdown", "*.md"),
                ("Text file", "*.txt"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        title = "Agent Conversation"
        try:
            import export_tools
            if ext == ".pdf":
                export_tools.export_pdf(self.bubble_log, path, title)
            elif ext == ".docx":
                export_tools.export_docx(self.bubble_log, path, title)
            elif ext == ".md":
                export_tools.export_md(self.bubble_log, path, title)
            else:
                export_tools.export_txt(self.bubble_log, path, title)
            messagebox.showinfo("Exported", f"Saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------
    def _show_typing(self):
        try:
            import config
            if str(config.load_config().get("SHOW_TYPING_INDICATOR", "true")).lower() not in ("true", "1", "yes", "on"):
                return
        except Exception:
            pass
        if self.typing_frame_visible:
            return
        self.typing_frame.pack(anchor="w", padx=14, pady=(4, 4))
        self.typing_frame_visible = True
        self._scroll_if_pinned()

    def _hide_typing(self):
        if not self.typing_frame_visible:
            return
        self.typing_frame.pack_forget()
        self.typing_frame_visible = False

    def _tick_typing(self):
        if self.typing_frame_visible:
            self.typing_dots = (self.typing_dots + 1) % 3
            dots = "● " * (self.typing_dots + 1) + "  " * (2 - self.typing_dots)
            try:
                self.typing_label.configure(text=dots.strip())
            except Exception:
                pass
        self.root.after(400, self._tick_typing)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def set_status(self, text, busy=False):
        self.status_label.configure(text=text)
        self.status_dot.configure(
            text_color=(COLOR_STATUS_BUSY if busy else COLOR_STATUS_OK)
        )

    # ------------------------------------------------------------------
    # Send / Clear
    # ------------------------------------------------------------------
    def send(self):
        if self.processing:
            return
        user_input = self._get_entry_text()
        if not user_input and not self.attachments:
            return

        if user_input.startswith("/") and not self.attachments:
            self._clear_entry()
            try:
                self.entry.configure(height=30)
            except Exception:
                pass
            self._handle_slash_command(user_input)
            return

        if not self.attachments and self._try_nl_fastpath(user_input):
            self._clear_entry()
            try:
                self.entry.configure(height=30)
            except Exception:
                pass
            return

        attached_paths = list(self.attachments)
        self.attachments.clear()
        self._render_attachments()

        self._clear_entry()
        try:
            self.entry.configure(height=30)
        except Exception:
            pass

        if attached_paths:
            names = ", ".join(os.path.basename(p) for p in attached_paths)
            header = f"📎 {names}"
            display = f"{header}\n{user_input}" if user_input else header
        else:
            display = user_input
        self._add_bubble(display, "user")

        self.processing = True
        self.send_btn.configure(state="disabled")
        self.mic_btn.configure(state="disabled")
        self.attach_btn.configure(state="disabled")
        self.set_status("Reading attachments…" if attached_paths else "Thinking…", busy=True)
        self._show_typing()

        threading.Thread(
            target=self.run_turn_background,
            args=(user_input, False, attached_paths),
            daemon=True,
        ).start()
        
    def stop_voice(self):
        """Kill any speech currently playing or queued."""
        try:
            import voice_output
            voice_output.stop()
            self._add_system_bubble("🔇 Voice stopped")
        except Exception as e:
            print(f"[stop_voice] {e}")
    def reset_memory(self):
        """Forget saved conversation history; leave the on-screen chat alone."""
        if not messagebox.askyesno(
                "Reset memory",
                "Forget the saved conversation (memory)?\n\n"
                "The chat on screen stays as-is.\n"
                "The next session starts fresh."):
            return
        self.conversation_history.clear()
        try:
            agent.save_conversation_history(self.conversation_history)
        except Exception:
            pass
        self._add_system_bubble("Memory reset — saved history cleared, chat kept.")

    def clear_chat(self):
        if not messagebox.askyesno("Clear chat", "Clear conversation and start fresh?"):
            return
        self.conversation_history.clear()
        try:
            agent.save_conversation_history(self.conversation_history)
        except Exception:
            pass
        for child in self.chat_frame.winfo_children():
            child.destroy()
        self.bubble_log.clear()
        self._bubble_labels.clear()
        self._md_widgets.clear()
        self._rebuild_typing_frame()
        self._add_system_bubble("Chat cleared. Fresh start ✨")
# ------------------------------------------------------------------
    # Document indexing (RAG)
    # ------------------------------------------------------------------
    def on_index_documents(self):
        folder = filedialog.askdirectory(title="Pick a folder to index")
        if not folder:
            return
        self._add_system_bubble(f"📚 Indexing {folder} … this can take a minute.")
        self.set_status("Indexing documents…", busy=True)

        def work():
            try:
                result = index_documents(folder)
                msg = f"✅ Indexing done:\n{result}"
            except Exception as e:
                msg = f"❌ Indexing failed: {e}"
            self.root.after(0, lambda m=msg: self._index_done(m))
        # Interrupt any ongoing speech so replies don't overlap
        try:
            import voice_output
            voice_output.stop()
        except Exception:
            pass
        threading.Thread(target=work, daemon=True).start()

    def _index_done(self, msg):
        self._add_system_bubble(msg)
        self.set_status("Ready")

    def _label_done(self, msg):
        self._add_system_bubble(msg)
        self.set_status("Ready")

    # ------------------------------------------------------------------
    # Auto-index configured folders (RAG)
    # ------------------------------------------------------------------
    def _auto_index_startup(self):
        """Re-index configured folders in the background at startup."""
        try:
            import config
            cfg = config.load_config()
        except Exception:
            return

        # Read folders from config. Stored as a list, but be forgiving
        # if the user typed a single string or a comma-separated string.
        raw = cfg.get("RAG_AUTO_INDEX_FOLDERS", [])
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.split(",") if p.strip()]
        elif not isinstance(raw, list):
            raw = []

        folders = [p for p in raw if isinstance(p, str) and p.strip()]
        if not folders:
            return

        def work():
            from rag_tool import index_documents
            for folder in folders:
                if not os.path.isdir(folder):
                    print(f"[auto-index] skipped (not a folder): {folder}")
                    continue
                try:
                    print(f"[auto-index] indexing {folder} ...")
                    result = index_documents(folder)
                    self.root.after(
                        0,
                        lambda r=result, f=folder: self._add_system_bubble(
                            f"📚 {f}:\n{r}"
                        ),
                    )
                except Exception as e:
                    self.root.after(
                        0,
                        lambda f=folder, err=str(e): self._add_system_bubble(
                            f"⚠️ Auto-index failed for {f}: {err}"
                        ),
                    )

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # Background work
    # ------------------------------------------------------------------
    def run_turn_background(self, user_input, is_scheduled, attached_paths=None):
        log_capture = io.StringIO()
        try:
            effective_input = user_input
            if attached_paths:
                try:
                    import file_tools
                    block = file_tools.build_attachment_block(attached_paths)
                    effective_input = (
                        f"{block}\n\nUser question: "
                        f"{user_input or '(none — analyze the attached files)'}"
                    )
                except Exception as e:
                    effective_input = f"[attachment read failed: {e}]\n\n{user_input}"

            suffix = f" [attached: {len(attached_paths)} file(s)]" if attached_paths else ""
            self.conversation_history.append(f"User: {user_input}{suffix}")

            with contextlib.redirect_stdout(log_capture):
                step_log = agent.run_agent_turn(effective_input, self.conversation_history)
        except Exception as e:
            step_log = [f"System: unexpected error - {e}"]
        self.result_queue.put((step_log, log_capture.getvalue(), is_scheduled, user_input))

    def on_scheduled_trigger(self, request_text):
        if request_text.startswith("REMINDER:"):
            reminder_text = request_text[len("REMINDER:"):].strip()
            self._fire_reminder_direct(reminder_text)
            return
        self.run_turn_background(request_text, True)

    def _fire_reminder_direct(self, reminder_text):
        self._add_bubble(f"[REMINDER] {reminder_text}", "scheduled")

        def do_it():
            try:
                result = agent.execute_reminder(reminder_text)
            except Exception as e:
                result = f"ERROR: {e}"
            self.root.after(0, lambda r=result: self._reminder_done(r, reminder_text))

        threading.Thread(target=do_it, daemon=True).start()
        threading.Thread(
            target=voice_output.speak,
            args=(f"Reminder: {reminder_text}",),
            daemon=True
        ).start()

    def _reminder_done(self, result, reminder_text):
        if self.log_visible:
            try:
                self.log_box.insert("end", f"[Reminder] {result}\n")
                self.log_box.see("end")
            except Exception:
                pass
        try:
            from winotify import Notification
            Notification(
                app_id="Agent",
                title="⏰ Reminder",
                msg=reminder_text,
                duration="long"
            ).show()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Queue polling
    # ------------------------------------------------------------------
    def poll_queue(self):
        try:
            while True:
                step_log, log_text, is_scheduled, source_input = self.result_queue.get_nowait()
                self.handle_result(step_log, log_text, is_scheduled, source_input)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def handle_result(self, step_log, log_text, is_scheduled, source_input):
        self._hide_typing()

        if step_log is None:
            step_log = ["System: agent returned None — check the terminal for a traceback."]
        if log_text is None:
            log_text = ""

        if log_text.strip():
            try:
                self.log_box.insert("end", log_text + "\n")
                self.log_box.see("end")
            except Exception:
                pass

        final_message = None

        for line in reversed(step_log):
            if line.startswith("Action: chat(") and "-> Result: " in line:
                candidate = line.split("-> Result: ", 1)[-1]
                if candidate.startswith("AI: "):
                    candidate = candidate[4:]
                if candidate.strip():
                    final_message = candidate
                    break
            if line.startswith("Done:"):
                final_message = line[5:].strip()
                break

        if not final_message:
            for line in reversed(step_log):
                if "AI: " in line:
                    candidate = line.split("AI: ", 1)[-1].strip()
                    if candidate:
                        final_message = candidate
                        break

        if not final_message:
            for line in reversed(step_log):
                s = line.strip()
                if s and not s.startswith(("Action:", "System:", "Done:")):
                    final_message = s
                    break

        if final_message:
            tools_ran = any(
                l.startswith("Action: ") and "-> Result:" in l
                and not l.startswith("Action: chat(")
                for l in step_log
            )
            claim_words = (
                "i've moved", "i moved", "i've created", "i created",
                "i've sent", "i sent", "i've deleted", "i deleted",
                "i've found", "i found ", "i've opened", "i opened",
                "files moved", "folder created",
            )
            low = final_message.lower()
            if any(w in low for w in claim_words) and not tools_ran:
                final_message = (
                    "⚠️ I didn't actually do that — no tool ran. "
                    "Say it again or use /find, /move, /mkdir to run it directly."
                )

        if is_scheduled:
            self._add_bubble(f"[{source_input}]", "scheduled")
            if final_message:
                self._add_bubble(final_message, "scheduled")
            try:
                agent.save_conversation_history(self.conversation_history)
            except Exception:
                pass
            return

        if final_message:
            tool_lines = [
                l for l in step_log
                if l.startswith("Action: ") and not l.startswith("Action: chat(")
            ]
            if tool_lines:
                names = []
                for l in tool_lines:
                    name = l[len("Action: "):].split("(")[0].strip()
                    if name and name not in names:
                        names.append(name)
                steps = len(tool_lines)
                self._add_activity(
                    f"used {', '.join(names)}  \u00b7  {steps} step"
                    f"{'s' if steps != 1 else ''}",
                    "\n".join(step_log),
                )
            self._add_bubble(final_message, "agent")
            auto_speak = True
            try:
                import config
                auto_speak = str(config.load_config().get("TTS_AUTO_SPEAK", "true")).lower() in ("true", "1", "yes", "on")
            except Exception:
                pass
            if auto_speak:
                threading.Thread(
                    target=voice_output.speak, args=(final_message,), daemon=True
                ).start()
        else:
            self._add_system_bubble("(no reply produced — check Log)")

        try:
            agent.save_conversation_history(self.conversation_history)
        except Exception:
            pass

        try:
            brain = agent.get_last_brain() or "none"
        except Exception:
            brain = "?"
        self.set_status(f"Ready · {brain}")
        self.processing = False
        self.send_btn.configure(state="normal")
        self.mic_btn.configure(state="normal")
        self.attach_btn.configure(state="normal")
        self.entry.focus()
    # ------------------------------------------------------------------
    # Global hotkey
    # ------------------------------------------------------------------
    def _setup_global_hotkey(self):
        if keyboard is None:
            print("[hotkey] 'keyboard' not installed. Run: pip install keyboard")
            return
        try:
            keyboard.on_press_key("f9", self._hotkey_press, suppress=False)
            keyboard.on_release_key("f9", self._hotkey_release, suppress=False)
            print("[hotkey] F9 -> hold to record, release to send")
        except Exception as e:
            print(f"[hotkey] failed to register: {e}")

    def _hotkey_press(self, event=None):
        self.root.after(0, lambda: self.on_mic_press(None))

    def _hotkey_release(self, event=None):
        self.root.after(0, lambda: self.on_mic_release(None))

    # ------------------------------------------------------------------
    # Push-to-talk
    # ------------------------------------------------------------------
    def _noop(self):
        pass

    def on_mic_press(self, event):
        if self.processing or self.recording:
            return
        self.recording = True
        self.processing = True
        self.send_btn.configure(state="disabled")
        self.attach_btn.configure(state="disabled")
        self.mic_btn.configure(
            image=self._icon_mic_rec,
            text="" if self._icon_mic_rec else "🔴",
            fg_color=COLOR_BTN_DANGER, hover_color="#b91c1c")
        self.set_status("Recording… release to send", busy=True)

        def start():
            try:
                import voice
                voice.start_recording()
            except Exception as err:
                msg = f"Mic error: {err}"
                self.root.after(0, lambda m=msg: self._mic_error(m))

        threading.Thread(target=start, daemon=True).start()

    def on_mic_release(self, event):
        if not self.recording:
            return
        self.recording = False
        self.mic_btn.configure(
            image=self._icon_mic,
            text="" if self._icon_mic else "🎤",
            fg_color="transparent", hover_color=COLOR_RAISED)
        self.set_status("Transcribing…", busy=True)

        def stop_and_send():
            try:
                import voice
                text = voice.stop_and_transcribe()
                self.root.after(0, lambda t=text: self._handle_transcript(t))
            except Exception as err:
                msg = f"Transcribe error: {err}"
                self.root.after(0, lambda m=msg: self._mic_error(m))

        threading.Thread(target=stop_and_send, daemon=True).start()

    def _handle_transcript(self, text):
        self.processing = False
        if text and text.strip():
            self._set_entry_text(text.strip())
            self.send()
        else:
            self.set_status("No speech detected — try again")
            self.send_btn.configure(state="normal")
            self.mic_btn.configure(state="normal")
            self.attach_btn.configure(state="normal")

    def _mic_error(self, err):
        self.set_status(err, busy=False)
        self.mic_btn.configure(
            image=self._icon_mic,
            text="" if self._icon_mic else "🎤",
            fg_color="transparent", hover_color=COLOR_RAISED)
        self.recording = False
        self.processing = False
        self.send_btn.configure(state="normal")
        self.mic_btn.configure(state="normal")
        self.attach_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # Activity log toggle
    # ------------------------------------------------------------------
    def toggle_log(self):
        if self.log_visible:
            self.log_box.pack_forget()
            self.log_visible = False
        else:
            self.log_box.pack(fill="x", side="bottom", after=self._input_outer)
            self.log_visible = True
            try:
                self.log_box.see("end")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Scheduled-tasks dialog
    # ------------------------------------------------------------------
    def open_schedule_dialog(self):
        win = ctk.CTkToplevel(self.root)
        win.title("Scheduled tasks")
        win.geometry("520x480")
        win.configure(fg_color=COLOR_BG)
        win.attributes("-topmost", True)

        ctk.CTkLabel(
            win, text="Scheduled tasks",
            font=ctk.CTkFont(family=FONT_UI, size=16, weight="bold"),
        ).pack(pady=(16, 4), padx=16, anchor="w")

        ctk.CTkLabel(
            win, text="Auto-refreshes. Reminders are one-shot; daily tasks repeat.",
            font=ctk.CTkFont(family=FONT_UI, size=12),
            text_color="#9ca3af"
        ).pack(padx=16, anchor="w")

        box = ctk.CTkTextbox(
            win, height=280, corner_radius=10,
            fg_color=COLOR_INPUT_BG,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        box.pack(fill="both", expand=True, padx=16, pady=12)

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 16))

        last_snapshot = {"val": None}

        def build_listing():
            lines = []
            try:
                reminders = scheduler.load_reminders() if hasattr(scheduler, 'load_reminders') else []
            except Exception:
                reminders = []
            if reminders:
                lines.append("── PENDING REMINDERS (one-shot) ──")
                for i, r in enumerate(reminders):
                    when = datetime.datetime.fromtimestamp(r["fire_at"]).strftime("%Y-%m-%d %H:%M")
                    lines.append(f"  [{i}] {when}  →  {r['text']}")
            else:
                lines.append("── PENDING REMINDERS (one-shot) ──")
                lines.append("  (none)")
            lines.append("")

            try:
                tasks = scheduler.load_tasks()
            except Exception:
                tasks = []
            if tasks:
                lines.append("── DAILY TASKS (repeat) ──")
                for i, t in enumerate(tasks):
                    lines.append(f"  [{i}] {t['time']}  →  {t['request']}")
            else:
                lines.append("── DAILY TASKS (repeat) ──")
                lines.append("  (none)")
            return "\n".join(lines)

        def refresh():
            try:
                if not win.winfo_exists():
                    return
            except Exception:
                return
            snapshot = build_listing()
            if snapshot != last_snapshot["val"]:
                last_snapshot["val"] = snapshot
                try:
                    box.configure(state="normal")
                    box.delete("1.0", "end")
                    box.insert("1.0", snapshot)
                    box.configure(state="disabled")
                except Exception:
                    pass
            try:
                win.after(3000, refresh)
            except Exception:
                pass

        refresh()

        def do_add():
            time_str = ctk.CTkInputDialog(text="Time (24-hour, e.g. 08:00):", title="Add task").get_input()
            if not time_str:
                return
            time_str = time_str.strip()
            if len(time_str) != 5 or time_str[2] != ":":
                messagebox.showerror("Invalid time", "Use HH:MM format.")
                return
            req = ctk.CTkInputDialog(text="What should the agent do at that time?", title="Add task").get_input()
            if not req:
                return
            scheduler.add_task(time_str, req.strip())
            last_snapshot["val"] = None
            refresh()

        def do_remove_task():
            idx_str = ctk.CTkInputDialog(text="Daily task number to remove:", title="Remove task").get_input()
            if idx_str is None:
                return
            try:
                idx = int(idx_str.strip())
            except ValueError:
                messagebox.showerror("Invalid", "Enter a number.")
                return
            if scheduler.remove_task(idx):
                last_snapshot["val"] = None
                refresh()
            else:
                messagebox.showerror("Not found", f"No daily task {idx}.")

        def do_clear_reminders():
            if not messagebox.askyesno("Clear reminders", "Remove ALL pending one-shot reminders?"):
                return
            try:
                scheduler.save_reminders([])
                last_snapshot["val"] = None
                refresh()
            except Exception as e:
                messagebox.showerror("Error", str(e))

        ctk.CTkButton(
            btn_row, text="+ Daily task", width=110,
            fg_color=COLOR_BTN_PRIMARY, hover_color="#1d4ed8",
            command=do_add
        ).pack(side="left")

        ctk.CTkButton(
            btn_row, text="− Daily task", width=110,
            fg_color=COLOR_BTN_DANGER, hover_color="#b91c1c",
            command=do_remove_task
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Clear reminders", width=130,
            fg_color=COLOR_BTN_GHOST, hover_color="#3a3f52",
            command=do_clear_reminders
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Close", width=80,
            fg_color=COLOR_BTN_GHOST, hover_color="#3a3f52",
            command=win.destroy
        ).pack(side="right")

    # ------------------------------------------------------------------
    # Tray / lifecycle
    # ------------------------------------------------------------------
    def _start_tray(self):
        try:
            self.tray_icon = tray.start_tray(
                on_show=lambda: self.root.after(0, self.show_window),
                on_quit=lambda: self.root.after(0, self.quit_app),
            )
            print(f"[gui] tray_icon = {self.tray_icon}")
        except Exception as e:
            import traceback
            print(f"[gui] tray failed: {e}")
            traceback.print_exc()
            self.tray_icon = None

    def hide_to_tray(self):
        try:
            agent.save_conversation_history(self.conversation_history)
        except Exception:
            pass
        self.root.withdraw()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self):
        try:
            agent.save_conversation_history(self.conversation_history)
            print(f"[Saved {len(self.conversation_history)} messages]")
        except Exception as e:
            print(f"[save error] {e}")
        try:
            voice_output.stop()
        except Exception:
            pass
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        if keyboard is not None:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        self.root.destroy()

    def on_close(self):
        self.quit_app()


def main():
    root = ctk.CTk()
    AgentGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()