import logging
import math
import threading
import time

import psutil

try:
    from winotify import Notification
except ImportError:
    Notification = None


logger = logging.getLogger(__name__)


def start_watch(condition, target, message):
    if not isinstance(condition, str) or condition.strip().lower() not in {"battery", "cpu", "ram"}:
        return "ERROR: condition must be battery, cpu, or ram."
    condition = condition.strip().lower()
    try:
        if isinstance(target, bool):
            raise ValueError()
        target = float(target)
    except (TypeError, ValueError):
        return "ERROR: target must be a percentage between 0 and 100."
    if not math.isfinite(target) or not 0 <= target <= 100:
        return "ERROR: target must be a percentage between 0 and 100."
    if not isinstance(message, str) or not message.strip():
        return "ERROR: a notification message is required."
    message = message.strip()
    if Notification is None:
        return "ERROR: winotify is not installed; system notifications are unavailable."
    if condition == "battery":
        try:
            if psutil.sensors_battery() is None:
                return "ERROR: No battery detected; cannot start a battery watch."
        except Exception as exc:
            return f"ERROR: Cannot read battery status: {exc}"

    def watch():
        try:
            while True:
                if condition == "battery":
                    battery = psutil.sensors_battery()
                    if battery is None:
                        raise RuntimeError("Battery is no longer available.")
                    percent = battery.percent
                elif condition == "cpu":
                    percent = psutil.cpu_percent(interval=1)
                else:
                    percent = psutil.virtual_memory().percent
                if percent >= target:
                    Notification(
                        app_id="Agent", title="System monitor", msg=message, duration="short",
                    ).show()
                    break
                time.sleep(30)
        except Exception:
            logger.exception("System watch failed for %s at %g%%", condition, target)

    thread = threading.Thread(target=watch, daemon=True, name=f"watch-{condition}")
    try:
        thread.start()
    except RuntimeError as exc:
        return f"ERROR: Cannot start system watcher: {exc}"
    return (f"Watching {condition} for {target:g}% or higher every 30 seconds. "
            "One notification will be sent while AgentBot is running.")
