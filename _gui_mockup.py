"""
Standalone visual mockup of the proposed AgentBot GUI rehaul.

Does NOT import agent.py, does NOT touch agent_gui.py, no new dependencies.
    python _gui_mockup.py            # interactive
    python _gui_mockup.py --shot     # render, screenshot to _gui_mockup.png, exit
"""
import os
import sys
import re
import customtkinter as ctk
import tkinter as tk
from tkinter import font as tkfont

# ---------------- palette (DeepSeek-leaning dark) ----------------
BG          = "#0d0f14"
BG_SIDEBAR  = "#111319"
BG_RAISED   = "#171a22"
BG_CODE     = "#080a0e"
BG_INPUT    = "#14171f"
BORDER      = "#23262f"
USER_PILL   = "#232838"
TEXT_HI     = "#e8eaef"
TEXT_MID    = "#9aa1af"
TEXT_LOW    = "#5f6673"
ACCENT      = "#4d7cfe"
OK          = "#3ecf8e"
HOVER       = "#1c2029"
SEL         = "#1b2130"

FONT = "Segoe UI"
MONO = "Consolas"
SIDEBAR_W = 226
HDR_H = 48

_INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\*[^*]+\*)")


# ---------------- mini markdown renderer (no deps) ----------------
class MdText(tk.Text):
    """Read-only tk.Text that renders a small subset of markdown via tags."""

    def __init__(self, master, md, bg=BG, size=12):
        super().__init__(master, wrap="word", bd=0, highlightthickness=0,
                         padx=0, pady=0, cursor="arrow", bg=bg, fg=TEXT_HI,
                         font=(FONT, size), state="normal", width=92)
        self._bg = bg
        self._size = size
        self.tag_configure("h2", font=(FONT, size + 3, "bold"),
                           foreground=TEXT_HI)
        self.tag_configure("bold", font=(FONT, size, "bold"), foreground=TEXT_HI)
        self.tag_configure("ital", font=(FONT, size, "italic"), foreground=TEXT_MID)
        self.tag_configure("code", font=(MONO, size - 1), foreground="#8fd3ff",
                           background="#161b24")
        self.tag_configure("li", foreground=TEXT_HI, lmargin1=14, lmargin2=26)
        self.tag_configure("p", foreground=TEXT_HI)
        self.tag_configure("dim", foreground=TEXT_MID)
        self._render(md)
        self.configure(state="disabled")
        self.fit_height()

    def _render(self, md):
        first = True
        for raw in md.split("\n"):
            line = raw.rstrip()
            if not first:
                self.insert("end", "\n")
            first = False
            if line.startswith("## "):
                self.insert("end", line[3:], "h2")
            elif line.startswith("- ") or line.startswith("* "):
                self.insert("end", "\u2022  ", "li")
                self._inline(line[2:], "li")
            else:
                self._inline(line, "p")

    def _inline(self, text, base):
        for part in _INLINE_RE.split(text):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                self.insert("end", part[2:-2], (base, "bold"))
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                self.insert("end", part[1:-1], (base, "code"))
            elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                self.insert("end", part[1:-1], (base, "ital"))
            else:
                self.insert("end", part, base)

    def fit_height(self):
        mapped = self.winfo_ismapped()
        if mapped:
            self.update_idletasks()
        try:
            res = self.count("1.0", "end", "displaylines")
            n = int(res[0]) if isinstance(res, (tuple, list)) else int(res)
            lines = n
        except Exception:
            lines = int(self.index("end-1c").split(".")[0])
        self.configure(height=max(1, lines))
        if mapped:
            self.update_idletasks()
        self._refits = getattr(self, "_refits", 0) + 1
        if not mapped and self._refits < 6:
            self.after(200, self.fit_height)

    def reflow(self, chars):
        self.configure(width=chars)
        self.fit_height()


class CodeBlock(tk.Frame):
    """Code fence: title bar with language + Copy, mono body."""

    def __init__(self, master, lang, code, on_copy):
        super().__init__(master, bg=BG_CODE, highlightthickness=1,
                         highlightbackground=BORDER)
        bar = tk.Frame(self, bg="#10141b")
        bar.pack(fill="x")
        tk.Label(bar, text=lang or "code", bg="#10141b", fg=TEXT_LOW,
                 font=(MONO, 9)).pack(side="left", padx=8, pady=3)
        copy_lbl = tk.Label(bar, text="Copy", bg="#10141b", fg=ACCENT,
                            cursor="hand2", font=(FONT, 9, "bold"))
        copy_lbl.pack(side="right", padx=8)
        copy_lbl.bind("<Button-1>", lambda e: on_copy(code))
        self.body = tk.Text(self, wrap="none", bd=0, highlightthickness=0,
                            padx=10, pady=8, bg=BG_CODE, fg="#d6deeb",
                            font=(MONO, 11), cursor="arrow", state="normal")
        self.body.insert("end", code.rstrip("\n"))
        self.body.configure(state="disabled")
        self.body.pack(fill="x")
        self.fit_height()

    def fit_height(self):
        self.body.update_idletasks()
        n = int(self.body.index("end-1c").split(".")[0])
        self.body.configure(height=max(1, n))

    def reflow(self, chars):
        self.body.configure(width=max(20, chars - 2))
        self.fit_height()


class Mockup:
    def __init__(self, root):
        self.root = root
        root.title("Agent  \u2014  GUI mockup")
        root.geometry("1120x780+60+40")
        root.minsize(860, 600)
        root.configure(fg_color=BG)
        ctk.set_appearance_mode("dark")

        self._md = []
        self._activity_flips = []
        self._last_key = None
        self._char_px = None
        self._reflowing = False
        self.sidebar_visible = True
        self.log_visible = False
        self.settings_visible = False
        self._copy_count = 0

        # ======================= SIDEBAR =======================
        self.sidebar = ctk.CTkFrame(root, fg_color=BG_SIDEBAR, corner_radius=0,
                                    width=SIDEBAR_W)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self._build_sidebar()

        # ======================= MAIN COLUMN =======================
        self.main = ctk.CTkFrame(root, fg_color=BG, corner_radius=0)
        self.main.pack(side="left", fill="both", expand=True)

        # --- header: 48px, near-empty ---
        hdr = ctk.CTkFrame(self.main, fg_color=BG, corner_radius=0, height=HDR_H)
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)

        self._icon_btn(hdr, "\u2630", self.toggle_sidebar).pack(side="left", padx=(10, 4))

        ctk.CTkLabel(hdr, text="Agent", font=ctk.CTkFont(FONT, 15, "bold"),
                     text_color=TEXT_HI).pack(side="left", padx=(6, 10))

        self.dot = ctk.CTkLabel(hdr, text="\u25cf", font=ctk.CTkFont(size=10),
                                text_color=OK)
        self.dot.pack(side="left")
        ctk.CTkLabel(hdr, text="ready", font=ctk.CTkFont(FONT, 11),
                     text_color=TEXT_LOW).pack(side="left", padx=(5, 0))

        self._icon_btn(hdr, "\u2699", self.toggle_settings).pack(side="right", padx=(2, 10))
        self._icon_btn(hdr, "\U0001f5d1", lambda: None).pack(side="right", padx=2)
        self._icon_btn(hdr, "\u21bb", lambda: None).pack(side="right", padx=2)

        ctk.CTkFrame(self.main, fg_color=BORDER, height=1,
                     corner_radius=0).pack(fill="x", side="top")

        # --- chat ---
        self.chat = ctk.CTkScrollableFrame(self.main, fg_color=BG, corner_radius=0,
                                           scrollbar_button_color="#22262f",
                                           scrollbar_button_hover_color="#313745")
        self.chat.pack(fill="both", expand=True)

        self._build_conversation()

        # --- log drawer: fixed height, off by default ---
        self.log = ctk.CTkFrame(self.main, fg_color=BG_RAISED, corner_radius=0,
                                height=126)
        self.log.pack_propagate(False)
        lbar = ctk.CTkFrame(self.log, fg_color="transparent")
        lbar.pack(fill="x", padx=12, pady=(7, 2))
        ctk.CTkLabel(lbar, text="Activity log", font=ctk.CTkFont(FONT, 10, "bold"),
                     text_color=TEXT_LOW).pack(side="left")
        self._log_body = tk.Text(self.log, wrap="none", bd=0, highlightthickness=0,
                                 bg=BG_RAISED, fg=TEXT_MID, font=(MONO, 10))
        for ln in ["12:04:11  brain=groq  model=openai/gpt-oss-120b  812ms",
                   "12:04:11  tool=find_files  input='downloads|SPM,CTU,LCC'",
                   "12:04:11  find_files -> 7 matches in 0.31s",
                   "12:04:12  brain=groq  640ms  (final answer, no tool)",
                   "12:04:12  turn complete  steps=2  tokens=1841"]:
            self._log_body.insert("end", ln + "\n")
        self._log_body.configure(state="disabled")
        self._log_body.pack(fill="both", expand=True, padx=12, pady=(0, 9))

        # --- input pill ---
        inp = ctk.CTkFrame(self.main, fg_color=BG, corner_radius=0)
        self.inp = inp
        inp.pack(fill="x", side="bottom")

        pill = ctk.CTkFrame(inp, fg_color=BG_INPUT, corner_radius=22,
                            border_width=1, border_color=BORDER)
        pill.pack(fill="x", padx=14, pady=12)

        self.entry = ctk.CTkTextbox(pill, font=ctk.CTkFont(FONT, 13),
                                    fg_color="transparent", border_width=0,
                                    corner_radius=0, height=26, wrap="word")
        self.entry.pack(side="left", fill="x", expand=True, padx=(16, 4), pady=9)
        self.entry.insert("1.0", "Ask anything, or type /help\u2026")
        self.entry.configure(text_color=TEXT_LOW)

        for glyph, cb in (("\U0001f4ce", lambda: None), ("\U0001f3a4", lambda: None)):
            self._icon_btn(pill, glyph, cb, size=15).pack(side="left", padx=2)

        send = ctk.CTkButton(pill, text="\u2191", width=32, height=32,
                             corner_radius=16, fg_color=ACCENT,
                             hover_color="#3f6ae0", text_color="#ffffff",
                             font=ctk.CTkFont(FONT, 15, "bold"),
                             command=lambda: None)
        send.pack(side="right", padx=(6, 8), pady=4)

        # ======================= SETTINGS OVERLAY =======================
        self.overlay = ctk.CTkFrame(root, fg_color=BG, corner_radius=0)
        self._build_settings_overlay()

        root.bind("<Configure>", lambda e: self._reflow())
        try:
            self.chat._parent_canvas.bind("<Configure>",
                                          lambda e: self._reflow(), add="+")
        except Exception:
            pass
        root.after(80, self._reflow)
        root.after(1200, self.settle)

    # ---------------- helpers ----------------
    def _icon_btn(self, parent, glyph, cmd, size=14, w=32):
        b = ctk.CTkButton(parent, text=glyph, width=w, height=30, corner_radius=8,
                          fg_color="transparent", hover_color=HOVER,
                          text_color=TEXT_MID, font=ctk.CTkFont(FONT, size),
                          command=cmd)
        return b

    def _nav_btn(self, parent, glyph, label, cmd, active=False):
        b = ctk.CTkButton(parent, text=f"  {glyph}   {label}", anchor="w",
                          height=34, corner_radius=9,
                          fg_color=SEL if active else "transparent",
                          hover_color=HOVER,
                          text_color=TEXT_HI if active else TEXT_MID,
                          font=ctk.CTkFont(FONT, 12), command=cmd)
        b.pack(fill="x", padx=10, pady=1)
        return b

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._copy_count += 1
        self.dot.configure(text_color=ACCENT)
        self.root.after(700, lambda: self.dot.configure(text_color=OK))

    def toggle_sidebar(self):
        if self.sidebar_visible:
            self.sidebar.pack_forget()
            self.sidebar_visible = False
        else:
            self.sidebar.pack(side="left", fill="y", before=self.main)
            self.sidebar_visible = True
        self._reflow()

    def toggle_settings(self):
        if self.settings_visible:
            self.overlay.place_forget()
            self.settings_visible = False
        else:
            self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.overlay.lift()
            self.settings_visible = True

    def toggle_log(self):
        if self.log_visible:
            self.log.pack_forget()
            self.log_visible = False
        else:
            self.log.pack(fill="x", side="bottom", after=self.inp)
            self.log_visible = True

    def expand_all_activity(self):
        for flip in self._activity_flips:
            flip()
        self.settle()

    def settle(self):
        self._last_key = None
        self._reflow()
        for widget in self._md:
            widget.fit_height()

    def _reflow(self):
        if self._reflowing:
            return
        self._reflowing = True
        try:
            w = self.chat.winfo_width()
            if w < 40:
                return
            if self._char_px is None:
                f = tkfont.Font(font=(FONT, 12))
                sample = "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJ 0123456789"
                self._char_px = max(4.0, f.measure(sample) / len(sample))
            chars = max(38, int((w - 40) / self._char_px))
            key = (chars, w)
            if key == self._last_key:
                return
            self._last_key = key
            for widget in self._md:
                widget.reflow(chars)
        finally:
            self._reflowing = False

    # ---------------- sidebar ----------------
    def _build_sidebar(self):
        s = self.sidebar
        ctk.CTkLabel(s, text="  AgentBot", font=ctk.CTkFont(FONT, 14, "bold"),
                     text_color=TEXT_HI).pack(fill="x", pady=(14, 10))

        ctk.CTkButton(s, text="  \uff0b   New chat", anchor="w", height=36,
                      corner_radius=10, fg_color=ACCENT, hover_color="#3f6ae0",
                      text_color="#ffffff", font=ctk.CTkFont(FONT, 12, "bold"),
                      command=lambda: None).pack(fill="x", padx=10, pady=(0, 14))

        ctk.CTkLabel(s, text="  RECENT", font=ctk.CTkFont(FONT, 10, "bold"),
                     text_color=TEXT_LOW).pack(fill="x", pady=(0, 5))

        chats = [("find my SPM and CTU files", True),
                 ("RAG index is slow on Downloads", False),
                 ("provider failover order", False),
                 ("patch_file vs write_file", False)]
        for label, active in chats:
            self._nav_btn(s, "\U0001f4ac", label, lambda: None, active=active)

        ctk.CTkLabel(s, text="  YESTERDAY", font=ctk.CTkFont(FONT, 10, "bold"),
                     text_color=TEXT_LOW).pack(fill="x", pady=(12, 5))
        for label in ("deep dive: local 3090 models", "telegram bot token setup"):
            self._nav_btn(s, "\U0001f4ac", label, lambda: None)

        ctk.CTkFrame(s, fg_color=BORDER, height=1,
                     corner_radius=0).pack(fill="x", padx=10, pady=12, side="bottom")

        bottom = ctk.CTkFrame(s, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", pady=(0, 10))
        self._nav_btn(bottom, "\u2699", "Settings", self.toggle_settings)
        self._nav_btn(bottom, "\U0001f4c5", "Tasks", lambda: None)
        self._nav_btn(bottom, "\U0001f4da", "Index docs", lambda: None)
        self._nav_btn(bottom, "\U0001f50d", "Activity log", self.toggle_log)

    # ---------------- conversation ----------------
    def _user(self, text):
        row = ctk.CTkFrame(self.chat, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(9, 3))
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(side="right")
        b = ctk.CTkFrame(inner, fg_color=USER_PILL, corner_radius=16)
        b.pack()
        ctk.CTkLabel(b, text=text, font=ctk.CTkFont(FONT, 12),
                     text_color=TEXT_HI, justify="right",
                     wraplength=520).pack(padx=14, pady=8)

    def _agent(self, md):
        row = ctk.CTkFrame(self.chat, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(3, 6))
        t = MdText(row, md)
        t.pack(fill="x", anchor="w", pady=(3, 5))
        self._md.append(t)

    def _code(self, lang, code):
        row = ctk.CTkFrame(self.chat, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(2, 8))
        cb = CodeBlock(row, lang, code, self._copy)
        cb.pack(fill="x")
        self._md.append(cb)

    def _activity(self, summary, detail):
        row = ctk.CTkFrame(self.chat, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(2, 4))
        chip = ctk.CTkFrame(row, fg_color=BG_RAISED, corner_radius=9,
                            border_width=1, border_color=BORDER)
        chip.pack(anchor="w")
        holder = ctk.CTkFrame(chip, fg_color="transparent")
        holder.pack(fill="x", padx=10, pady=5)
        arrow = ctk.CTkLabel(holder, text="\u25b8", font=ctk.CTkFont(FONT, 10),
                             text_color=TEXT_LOW)
        arrow.pack(side="left")
        ctk.CTkLabel(holder, text=summary, font=ctk.CTkFont(FONT, 11),
                     text_color=TEXT_MID).pack(side="left", padx=6)

        body = ctk.CTkFrame(row, fg_color="transparent")
        state = {"open": False}

        def flip(_=None):
            state["open"] = not state["open"]
            if state["open"]:
                arrow.configure(text="\u25be")
                body.pack(fill="x", pady=(5, 0), padx=(2, 0))
            else:
                arrow.configure(text="\u25b8")
                body.pack_forget()

        chip.bind("<Button-1>", flip)
        for child in holder.winfo_children():
            child.bind("<Button-1>", flip)
        chip.configure(cursor="hand2")
        self._activity_flips.append(flip)

        inner = ctk.CTkFrame(body, fg_color=BG_RAISED, corner_radius=8)
        inner.pack(fill="x")
        mono = tk.Text(inner, wrap="none", bd=0, highlightthickness=0,
                       bg=BG_RAISED, fg=TEXT_LOW, font=(MONO, 10), cursor="arrow")
        mono.insert("end", detail)
        mono.configure(state="disabled", height=len(detail.split("\n")))
        mono.pack(fill="x", padx=10, pady=8)

    def _system_line(self, text):
        ctk.CTkLabel(self.chat, text=text, font=ctk.CTkFont(FONT, 10),
                     text_color=TEXT_LOW).pack(padx=18, pady=(8, 2), anchor="w")

    def _build_conversation(self):
        self._system_line("Resumed \u2014 24 messages from last session")

        self._user("find my SPM, CTU and LCC files in downloads and tell me what's duplicated")

        self._activity(
            "used find_files, read_file  \u00b7  2 steps  \u00b7  1.4s",
            '{"tool":"find_files","input":"downloads|SPM,CTU,LCC"}\n'
            '  -> 7 matches\n'
            '{"tool":"read_file","input":"C:\\\\Users\\\\USER\\\\Downloads\\\\SPM-2024.pdf"}\n'
            '  -> 41 KB read\n'
            'brain=groq  model=openai/gpt-oss-120b  steps=2')

        self._agent(
            "Found **7 matches** across three patterns. Two are duplicates:\n"
            "- `SPM-2024.pdf` appears in both **Downloads** and **Downloads\\Fire Writing** "
            "(identical, 41 KB)\n"
            "- `CTU-syllabus.docx` appears twice with different modified dates \u2014 the newer "
            "one is *not* the larger one, so check before deleting\n\n"
            "The rest are unique. Nothing in `LCC`.")

        self._user("show me the fix for the config comma bug")

        self._activity("used patch_file  \u00b7  1 step  \u00b7  0.6s",
                       '{"tool":"patch_file","input":"config.py|||...|||..."}\n'
                       '  -> 1 occurrence replaced, re-parsed OK')

        self._code("python",
                   'DEFAULTS = {\n'
                   '    "RAG_AUTO_INDEX_FOLDERS": [\n'
                   '        "C:\\\\Users\\\\USER\\\\Downloads\\\\Fire Writing",\n'
                   '        "C:\\\\Users\\\\USER\\\\Downloads",   # <- comma was missing\n'
                   '        "C:\\\\Users\\\\USER\\\\Documents",\n'
                   '    ],\n'
                   '}')

        self._agent(
            "That missing comma made Python **concatenate** the two strings, so Documents "
            "was never indexed. `patch_file` refuses to write when the search text matches "
            "more than once, and re-parses `.py` files after writing.")

        self._system_line("\U0001f4c5  scheduled \u2014 daily standup reminder fired at 09:00")

    # ---------------- settings overlay ----------------
    def _build_settings_overlay(self):
        o = self.overlay
        bar = ctk.CTkFrame(o, fg_color=BG_SIDEBAR, corner_radius=0, height=HDR_H)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._icon_btn(bar, "\u2190", self.toggle_settings).pack(side="left", padx=(10, 4))
        ctk.CTkLabel(bar, text="Settings", font=ctk.CTkFont(FONT, 15, "bold"),
                     text_color=TEXT_HI).pack(side="left", padx=6)
        ctk.CTkLabel(bar, text="saved to %APPDATA%\\AgentBot\\config.json",
                     font=ctk.CTkFont(FONT, 10),
                     text_color=TEXT_LOW).pack(side="left", padx=14)
        ctk.CTkButton(bar, text="Save", width=76, height=30, corner_radius=8,
                      fg_color=ACCENT, hover_color="#3f6ae0",
                      font=ctk.CTkFont(FONT, 12, "bold"),
                      command=self.toggle_settings).pack(side="right", padx=12)

        body = ctk.CTkFrame(o, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True)

        rail = ctk.CTkFrame(body, fg_color=BG, corner_radius=0, width=170)
        rail.pack(side="left", fill="y", padx=(16, 0), pady=16)
        rail.pack_propagate(False)
        for i, label in enumerate(("Brain / providers", "Voice & TTS",
                                   "RAG indexing", "Appearance",
                                   "Telegram", "Scheduled tasks")):
            b = ctk.CTkButton(rail, text=f"  {label}", anchor="w", height=32,
                              corner_radius=8,
                              fg_color=SEL if i == 0 else "transparent",
                              hover_color=HOVER,
                              text_color=TEXT_HI if i == 0 else TEXT_MID,
                              font=ctk.CTkFont(FONT, 12), command=lambda: None)
            b.pack(fill="x", pady=1)

        panel = ctk.CTkScrollableFrame(body, fg_color=BG_RAISED, corner_radius=14,
                                       border_width=1, border_color=BORDER)
        panel.pack(side="left", fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(panel, text="Failover order",
                     font=ctk.CTkFont(FONT, 13, "bold"),
                     text_color=TEXT_HI).pack(anchor="w", padx=16, pady=(14, 2))
        ctk.CTkLabel(panel, text="Drag to reorder. Dead providers are skipped automatically.",
                     font=ctk.CTkFont(FONT, 11),
                     text_color=TEXT_LOW).pack(anchor="w", padx=16, pady=(0, 10))

        for name, model, state, color in (
                ("groq", "openai/gpt-oss-120b", "ok  \u00b7  812ms", OK),
                ("gemini", "gemini-flash-latest", "ok  \u00b7  1.2s", OK),
                ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free", "ok  \u00b7  2.4s", OK),
                ("meta", "\u2014", "ok  \u00b7  1.9s", OK),
                ("huggingface", "meta-llama/Llama-3.3-70B-Instruct", "ok  \u00b7  3.1s", OK),
                ("mistral", "mistral-large-latest", "429 free tier", "#eab308"),
                ("cohere", "command-r", "ok  \u00b7  1.6s", OK),
                ("ollama", "granite4.1:3b", "offline \u2014 no GPU", TEXT_LOW)):
            row = ctk.CTkFrame(panel, fg_color=BG_INPUT, corner_radius=9,
                               border_width=1, border_color=BORDER)
            row.pack(fill="x", padx=14, pady=3)
            ctk.CTkLabel(row, text="\u2261", font=ctk.CTkFont(FONT, 13),
                         text_color=TEXT_LOW).pack(side="left", padx=(9, 6))
            ctk.CTkLabel(row, text=name, width=92, anchor="w",
                         font=ctk.CTkFont(FONT, 12, "bold"),
                         text_color=TEXT_HI).pack(side="left")
            ctk.CTkLabel(row, text=model, anchor="w",
                         font=ctk.CTkFont(MONO, 10),
                         text_color=TEXT_MID).pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(row, text=state, width=112, anchor="e",
                         font=ctk.CTkFont(FONT, 10),
                         text_color=color).pack(side="right", padx=10)
        ctk.CTkFrame(panel, height=12, fg_color="transparent").pack()


def main():
    root = ctk.CTk()
    app = Mockup(root)
    if any(a == "--shot" or a.startswith("--shot=") for a in sys.argv):
        variant = "main"
        for arg in sys.argv:
            if arg.startswith("--shot="):
                variant = arg.split("=", 1)[1]
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           f"_gui_mockup_{variant}.png")
        if variant == "settings":
            root.after(300, app.toggle_settings)
        elif variant == "log":
            root.after(300, app.toggle_log)
        elif variant == "expanded":
            root.after(300, app.expand_all_activity)

        def grab():
            try:
                from PIL import ImageGrab
                root.lift()
                root.attributes("-topmost", True)
                root.update_idletasks()
                root.update()
                img = ImageGrab.grab(all_screens=True)
                try:
                    import ctypes
                    scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
                except Exception:
                    scale = 1.0
                x, y = root.winfo_rootx(), root.winfo_rooty()
                w, h = root.winfo_width(), root.winfo_height()
                box = (int(x * scale), int(y * scale),
                       int((x + w) * scale), int((y + h) * scale))
                img.crop(box).save(out)
                print("saved", out, "scale", scale)
            except Exception as e:
                print("screenshot failed:", e)
            os._exit(0)

        root.after(1600, grab)
    root.mainloop()


if __name__ == "__main__":
    main()
