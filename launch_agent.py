"""
AgentBot launcher — picks the CORRECT Python.

Config: agentbot_launcher.json next to this EXE:
    { "python": "C:\\path\\to\\python.exe",
      "script": "C:\\Users\\USER\\AgentBot\\agent_gui.py" }

If config is missing, auto-detects, but SKIPS known-bad Python installs
(Laragon, Microsoft Store stubs, WindowsApps).
"""

import os, sys, json, subprocess, traceback, tempfile, datetime, ctypes

HERE        = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                              else os.path.abspath(__file__))
CONFIG      = os.path.join(HERE, "agentbot_launcher.json")
LOG         = os.path.join(tempfile.gettempdir(), "agentbot_launcher.log")
CHILD_LOG   = os.path.join(tempfile.gettempdir(), "agentbot_child.log")
APP_ID      = "AgentBot.Local.1"

DEFAULT_SCRIPT = r"C:\Users\USER\AgentBot\agent_gui.py"

BAD_MARKERS = ("\\laragon\\", "\\WindowsApps\\", "\\Microsoft\\WindowsApps\\")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def load_config():
    if not os.path.exists(CONFIG):
        return {}
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log("config parse failed: " + traceback.format_exc())
        return {}


def find_python():
    cfg = load_config()
    if cfg.get("python") and os.path.exists(cfg["python"]):
        log(f"python from config: {cfg['python']}")
        return cfg["python"]

    # 1) venv inside the AgentBot folder
    agent_dir = os.path.dirname(cfg.get("script", DEFAULT_SCRIPT))
    for venv in (".venv", "venv", "env"):
        p = os.path.join(agent_dir, venv, "Scripts", "python.exe")
        if os.path.exists(p):
            log(f"python from venv: {p}")
            return p

    # 2) PATH — but reject known-bad locations
    import shutil
    for exe in ("python.exe", "pythonw.exe"):
        p = shutil.which(exe)
        if p and not any(bad in p for bad in BAD_MARKERS):
            log(f"python from PATH: {p}")
            return p
        elif p:
            log(f"rejected PATH python (bad marker): {p}")

    # 3) Common install dirs, skipping Laragon
    for base in (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
        r"C:\Python313", r"C:\Python312", r"C:\Python311", r"C:\Python310",
    ):
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            if any(bad in root for bad in BAD_MARKERS):
                continue
            if "python.exe" in files:
                p = os.path.join(root, "python.exe")
                log(f"python from scan: {p}")
                return p

    return None


def main():
    log("=" * 60)
    log(f"frozen={getattr(sys,'frozen',False)}  exe={sys.executable}")

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        log("AUMID failed: " + traceback.format_exc())

    cfg    = load_config()
    script = cfg.get("script", DEFAULT_SCRIPT)
    python = find_python()

    log(f"script={script}  exists={os.path.exists(script)}")
    log(f"python={python}")

    if not python:
        _err("No usable Python found.\n\n"
             f"Create {CONFIG} with:\n"
             '{\n  "python": "C:\\\\path\\\\to\\\\python.exe",\n'
             f'  "script": "{DEFAULT_SCRIPT}"\n}}')
        return
    if not os.path.exists(script):
        _err(f"Script not found:\n{script}")
        return

    # Fresh child log each run so old crashes don't confuse us
    try:
        open(CHILD_LOG, "w", encoding="utf-8").close()
    except Exception:
        pass

    child_out = open(CHILD_LOG, "a", encoding="utf-8", buffering=1)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            [python, script],
            cwd=os.path.dirname(script),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=child_out,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
        )
        log(f"spawned pid={proc.pid} (child log: {CHILD_LOG})")
    except Exception:
        tb = traceback.format_exc()
        log("EXCEPTION:\n" + tb)
        _err(tb)


def _err(msg):
    try:
        import tkinter as tk, tkinter.messagebox as mb
        r = tk.Tk(); r.withdraw()
        mb.showerror("AgentBot Launcher", msg)
        r.destroy()
    except Exception:
        log("dialog failed: " + traceback.format_exc())


if __name__ == "__main__":
    main()