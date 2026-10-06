"""
Rich chat widgets for agent_gui.py — markdown text, code fences, activity
chips and crisp coloured icons. No new dependencies: tkinter + (optionally)
PIL for icon rendering, with an emoji fallback when PIL is missing.
"""
import re
import tkinter as tk
from tkinter import font as tkfont

try:
    from PIL import Image, ImageDraw
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

# --------------------------- palette ---------------------------
COLOR_BG           = "#0d0f14"
COLOR_BG_ALT       = "#141720"
COLOR_SIDEBAR      = "#111319"
COLOR_RAISED       = "#171a22"
COLOR_CODE_BG      = "#080a0e"
COLOR_INPUT_BG     = "#14171f"
COLOR_BORDER       = "#23262f"
COLOR_HOVER        = "#1c2029"
COLOR_SELECTED     = "#16241e"

COLOR_USER_PILL    = "#232838"
COLOR_TEXT_HI      = "#e8eaef"
COLOR_TEXT_MID     = "#9aa1af"
COLOR_TEXT_LOW     = "#5f6673"

# mint accent
COLOR_ACCENT       = "#3ecf8e"
COLOR_ACCENT_HOVER = "#2fb87c"
COLOR_ACCENT_DIM   = "#1f4d3a"
COLOR_DANGER       = "#dc2626"
COLOR_WARN         = "#eab308"
COLOR_SCHED_TEXT   = "#fbbf24"

FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"

_CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\*[^*]+\*)")


def split_fences(text):
    """Yield ('text', chunk) and ('code', lang, chunk) segments."""
    pos = 0
    for m in _CODE_FENCE_RE.finditer(text):
        if m.start() > pos:
            yield ("text", text[pos:m.start()])
        yield ("code", m.group(1) or "code", m.group(2))
        pos = m.end()
    if pos < len(text):
        yield ("text", text[pos:])


# --------------------------- markdown text ---------------------------
class MdText(tk.Text):
    """Read-only tk.Text rendering a small markdown subset via tags.

    Height is kept in LINES (tk.Text -height unit) and recomputed from
    display-line count whenever width or content changes.
    """

    def __init__(self, master, md="", bg=COLOR_BG, fg=COLOR_TEXT_HI, size=12):
        super().__init__(master, wrap="word", bd=0, highlightthickness=0,
                         padx=0, pady=0, cursor="arrow", bg=bg, fg=fg,
                         font=(FONT_UI, size), state="normal", width=92,
                         height=1)
        self._fg = fg
        self._size = size
        self._refits = 0
        self.tag_configure("h2", font=(FONT_UI, size + 3, "bold"), foreground=fg)
        self.tag_configure("bold", font=(FONT_UI, size, "bold"), foreground=fg)
        self.tag_configure("ital", font=(FONT_UI, size, "italic"),
                           foreground=COLOR_TEXT_MID)
        self.tag_configure("code", font=(FONT_MONO, size - 1),
                           foreground="#8fd3ff", background="#161b24")
        self.tag_configure("li", foreground=fg, lmargin1=14, lmargin2=26)
        self.tag_configure("p", foreground=fg)
        self.set_markdown(md)

    def set_markdown(self, md):
        self.configure(state="normal")
        self.delete("1.0", "end")
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
        self.configure(state="disabled")
        self.fit_height()

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

    def display_lines(self):
        try:
            res = self.count("1.0", "end", "displaylines")
            return int(res[0]) if isinstance(res, (tuple, list)) else int(res)
        except Exception:
            return int(self.index("end-1c").split(".")[0])

    def fit_height(self):
        mapped = self.winfo_ismapped()
        if mapped:
            self.update_idletasks()
        self.configure(height=max(1, self.display_lines()))
        if mapped:
            self.update_idletasks()
        self._refits += 1
        if not mapped and self._refits < 6:
            self.after(200, self.fit_height)

    def reflow(self, chars):
        self.configure(width=max(20, chars))
        self.fit_height()


# --------------------------- code fence ---------------------------
class CodeBlock(tk.Frame):
    def __init__(self, master, lang, code, on_copy=None):
        super().__init__(master, bg=COLOR_CODE_BG, highlightthickness=1,
                         highlightbackground=COLOR_BORDER)
        bar = tk.Frame(self, bg="#10141b")
        bar.pack(fill="x")
        tk.Label(bar, text=lang, bg="#10141b", fg=COLOR_TEXT_LOW,
                 font=(FONT_MONO, 9)).pack(side="left", padx=8, pady=3)
        if on_copy:
            copy_lbl = tk.Label(bar, text="Copy", bg="#10141b", fg=COLOR_ACCENT,
                                cursor="hand2", font=(FONT_UI, 9, "bold"))
            copy_lbl.pack(side="right", padx=8)
            copy_lbl.bind("<Button-1>", lambda e: on_copy(code))
        self.body = tk.Text(self, wrap="none", bd=0, highlightthickness=0,
                            padx=10, pady=8, bg=COLOR_CODE_BG, fg="#d6deeb",
                            font=(FONT_MONO, 11), cursor="arrow", state="normal")
        self.body.insert("end", code.rstrip("\n"))
        self.body.configure(state="disabled")
        xscroll = tk.Scrollbar(self, orient="horizontal",
                               command=self.body.xview)
        self.body.configure(xscrollcommand=xscroll.set)
        self.body.pack(fill="x")
        xscroll.pack(fill="x")
        self.fit_height()

    def fit_height(self):
        n = int(self.body.index("end-1c").split(".")[0])
        self.body.configure(height=max(1, min(n, 24)))

    def reflow(self, chars):
        self.body.configure(width=max(20, chars - 2))
        self.fit_height()


# --------------------------- activity chip ---------------------------
class ActivityChip(tk.Frame):
    """Collapsed per-turn tool summary; click to expand the raw step log."""

    def __init__(self, master, summary, detail):
        super().__init__(master, bg=COLOR_BG)
        self._open = False
        self._detail = detail

        self.chip = tk.Frame(self, bg=COLOR_RAISED, cursor="hand2",
                             highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.chip.pack(anchor="w")
        self.arrow = tk.Label(self.chip, text="\u25b8", bg=COLOR_RAISED,
                              fg=COLOR_TEXT_LOW, font=(FONT_UI, 10))
        self.arrow.pack(side="left", padx=(9, 0))
        self.label = tk.Label(self.chip, text=summary, bg=COLOR_RAISED,
                              fg=COLOR_TEXT_MID, font=(FONT_UI, 11))
        self.label.pack(side="left", padx=6, pady=5)

        self.body = tk.Text(self, wrap="none", bd=0, highlightthickness=0,
                            bg=COLOR_RAISED, fg=COLOR_TEXT_LOW,
                            font=(FONT_MONO, 10), cursor="arrow")
        self.body.insert("end", detail)
        self.body.configure(state="disabled")

        for w in (self.chip, self.arrow, self.label):
            w.bind("<Button-1>", self.toggle)

    def toggle(self, _event=None):
        self._open = not self._open
        if self._open:
            self.arrow.configure(text="\u25be")
            self.body.configure(height=max(1, len(self._detail.split("\n"))))
            self.body.pack(fill="x", padx=(2, 0), pady=(5, 0))
        else:
            self.arrow.configure(text="\u25b8")
            self.body.pack_forget()


# --------------------------- icons ---------------------------
def _supersample(size):
    return Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))


def _icon_clip(size, color):
    img = _supersample(size)
    d = ImageDraw.Draw(img)
    s = size * 4
    w = max(2, int(s * 0.09))
    # paperclip: outer U + inner U, tilted
    box_out = (s * 0.26, s * 0.10, s * 0.74, s * 0.78)
    box_in = (s * 0.40, s * 0.24, s * 0.60, s * 0.64)
    d.rounded_rectangle(box_out, radius=int(s * 0.24), outline=color, width=w)
    d.rectangle((box_out[0] - w, s * 0.40, box_out[0] + w, s * 0.62),
                fill=(0, 0, 0, 0))
    d.rounded_rectangle(box_in, radius=int(s * 0.10), outline=color, width=w)
    d.rectangle((box_in[2] - w, s * 0.44, box_in[2] + w, s * 0.66),
                fill=(0, 0, 0, 0))
    img = img.rotate(-38, resample=Image.BICUBIC, expand=False,
                     center=(s / 2, s / 2))
    return img.resize((size, size), Image.LANCZOS)


def _icon_mic(size, color):
    img = _supersample(size)
    d = ImageDraw.Draw(img)
    s = size * 4
    w = max(2, int(s * 0.09))
    d.rounded_rectangle((s * 0.38, s * 0.10, s * 0.62, s * 0.56),
                        radius=int(s * 0.12), outline=color, width=w)
    d.arc((s * 0.26, s * 0.30, s * 0.74, s * 0.78), start=0, end=180,
          fill=color, width=w)
    d.line((s * 0.50, s * 0.78, s * 0.50, s * 0.90), fill=color, width=w)
    d.line((s * 0.34, s * 0.90, s * 0.66, s * 0.90), fill=color, width=w)
    return img.resize((size, size), Image.LANCZOS)


def _icon_send(size, color):
    img = _supersample(size)
    d = ImageDraw.Draw(img)
    s = size * 4
    w = max(2, int(s * 0.10))
    d.line((s * 0.50, s * 0.80, s * 0.50, s * 0.24), fill=color, width=w)
    d.line((s * 0.28, s * 0.46, s * 0.50, s * 0.22), fill=color, width=w)
    d.line((s * 0.72, s * 0.46, s * 0.50, s * 0.22), fill=color, width=w)
    return img.resize((size, size), Image.LANCZOS)


def make_icon(name, size=20, color=COLOR_ACCENT):
    """Return a CTkImage or None when PIL is unavailable."""
    if not _HAS_PIL:
        return None
    import customtkinter as ctk
    draw = {"clip": _icon_clip, "mic": _icon_mic, "send": _icon_send}[name]
    img = draw(size, color)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
