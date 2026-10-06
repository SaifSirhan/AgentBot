"""
System tray icon for the agent. This is what actually lets scheduled tasks
run unattended - without it, closing the window ends the whole process, so
a "check the news at 8am" task could only ever fire while you happened to
have the window open.

The tray loop is started with pystray's run_detached() integration from the
Tk main thread. on_show and on_quit run outside Tk's main thread, so
agent_gui.py wraps both in root.after(0, ...) before touching any widget.

Requires: pip install pystray
"""

def _make_icon_image():
    """
    A simple generated icon (a solid circle) instead of depending on a
    bundled .ico file existing on disk somewhere. Matches the GUI's blue
    accent color so it's recognizable at a glance in a crowded tray.
    """
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=(37, 99, 235, 255))
    return img


def start_tray(on_show, on_quit):
    try:
        import pystray
        print("[tray] pystray imported OK")
    except Exception as e:
        print(f"[tray] pystray import FAILED: {e}")
        return None

    def on_show_clicked(icon, item):
        on_show()

    def on_quit_clicked(icon, item):
        icon.stop()
        on_quit()

    try:
        menu = pystray.Menu(
            pystray.MenuItem("Show", on_show_clicked, default=True),
            pystray.MenuItem("Quit", on_quit_clicked),
        )

        icon = pystray.Icon("agent", _make_icon_image(), "Agent", menu)
        print("[tray] Icon object created")

        icon.run_detached()
        print("[tray] detached icon loop started")
        return icon
    except Exception as e:
        print(f"[tray] FAILED to create icon: {e}")
        import traceback
        traceback.print_exc()
        return None
