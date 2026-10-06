"""
Proactive/scheduled tasks for the agent.

Two kinds of scheduled items:
  1. Daily tasks  - (time "HH:MM", request) pairs, repeat every day.
  2. Reminders    - one-shot, fire once then delete themselves.

Requires: pip install schedule
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
import schedule

TASKS_FILE = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'scheduled_tasks.json')
REMINDERS_FILE = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'reminders.json')


# ---------------------------
# Daily tasks
# ---------------------------
def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return []
    try:
        with open(TASKS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('tasks', [])
    except (json.JSONDecodeError, OSError):
        return []


def save_tasks(tasks):
    os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
    with open(TASKS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'tasks': tasks}, f, indent=2)


def add_task(time_str, request):
    tasks = load_tasks()
    tasks.append({"time": time_str, "request": request})
    save_tasks(tasks)


def remove_task(index):
    tasks = load_tasks()
    if 0 <= index < len(tasks):
        tasks.pop(index)
        save_tasks(tasks)
        return True
    return False


def next_run_for(time_str):
    """Return the datetime of the next occurrence of 'HH:MM' from now."""
    now = datetime.now()
    try:
        hh, mm = map(int, time_str.split(":"))
    except Exception:
        return None
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------
# One-shot reminders
# ---------------------------
def load_reminders():
    if not os.path.exists(REMINDERS_FILE):
        return []
    try:
        with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('reminders', [])
    except (json.JSONDecodeError, OSError):
        return []


def save_reminders(reminders):
    os.makedirs(os.path.dirname(REMINDERS_FILE), exist_ok=True)
    with open(REMINDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'reminders': reminders}, f, indent=2)


def add_reminder(seconds_from_now, text):
    """Schedule a one-shot reminder N seconds from now. Returns the fire timestamp."""
    fire_at = time.time() + seconds_from_now
    reminders = load_reminders()
    reminders.append({"fire_at": fire_at, "text": text})
    save_reminders(reminders)
    return fire_at


def parse_duration(duration_str):
    """Convert '30s', '5min', '2hr', '1day' to seconds. Returns None if invalid."""
    if not duration_str:
        return None
    s = duration_str.strip().lower()
    try:
        if s.endswith("min"):
            return int(float(s[:-3].strip())) * 60
        if s.endswith("sec"):
            return int(float(s[:-3].strip()))
        if s.endswith("hr"):
            return int(float(s[:-2].strip())) * 3600
        if s.endswith("day"):
            return int(float(s[:-3].strip())) * 86400
        # single-letter forms
        if s.endswith("s"):
            return int(float(s[:-1].strip()))
        if s.endswith("m"):
            return int(float(s[:-1].strip())) * 60
        if s.endswith("h"):
            return int(float(s[:-1].strip())) * 3600
        if s.endswith("d"):
            return int(float(s[:-1].strip())) * 86400
    except (ValueError, AttributeError):
        pass
    return None


# ---------------------------
# Scheduler thread
# ---------------------------
def start_scheduler(on_trigger):
    """
    Background thread that checks clock-based tasks and one-shot reminders.
    on_trigger is fired on its OWN daemon thread so a slow agent turn
    (Ollama, Selenium) never blocks the scheduler loop itself.
    """
    def safe_trigger(msg):
        # Fire the callback on a separate thread so the main loop
        # can keep checking for other due reminders.
        try:
            threading.Thread(target=on_trigger, args=(msg,), daemon=True).start()
        except Exception as e:
            print(f"[Scheduler] Trigger error: {e}")

    def loop():
        last_loaded = None
        interval = 5                     # seconds between checks
        next_tick = time.time() + interval
        print(f"[Scheduler] Started. Tasks file: {TASKS_FILE}")

        while True:
            # --- daily tasks ---
            tasks = load_tasks()
            if tasks != last_loaded:
                schedule.clear()
                if tasks:
                    print(f"\n[Scheduler] {len(tasks)} daily task(s) registered:")
                    for task in tasks:
                        schedule.every().day.at(task["time"]).do(safe_trigger, task["request"])
                        nxt = next_run_for(task["time"])
                        print(f"  • {task['time']} -> {task['request'][:60]} (next: {nxt})")
                else:
                    print("\n[Scheduler] No daily tasks.")
                last_loaded = tasks

            schedule.run_pending()

            # --- one-shot reminders ---
            now = time.time()
            reminders = load_reminders()
            due = [r for r in reminders if r["fire_at"] <= now]
            if due:
                remaining = [r for r in reminders if r["fire_at"] > now]
                save_reminders(remaining)
                for r in due:
                    lateness = now - r["fire_at"]
                    print(f"[Scheduler] Reminder firing (lateness {lateness:.1f}s): {r['text']}")
                    safe_trigger(f"REMINDER: {r['text']}")

            # Self-correcting sleep — prevents the 5-second interval
            # from stretching to 6, 7, 8 seconds over time.
            sleep_for = next_tick - time.time()
            if sleep_for < 0:
                sleep_for = 0            # we're behind — catch up immediately
            time.sleep(sleep_for)
            next_tick += interval
            if next_tick < time.time():
                # if we fell way behind (e.g. system slept), reset the clock
                next_tick = time.time() + interval

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    print(f"Tasks file: {TASKS_FILE}")
    print(f"Reminders file: {REMINDERS_FILE}")
    tasks = load_tasks()
    if tasks:
        for i, t in enumerate(tasks):
            nxt = next_run_for(t["time"])
            nxt_str = nxt.strftime("%Y-%m-%d %H:%M") if nxt else "?"
            print(f"  [{i}] {t['time']} -> {t['request']}")
            print(f"       next run: {nxt_str}")
    else:
        print("No scheduled tasks yet.")

    reminders = load_reminders()
    if reminders:
        print(f"\nPending reminders ({len(reminders)}):")
        for r in reminders:
            when = datetime.fromtimestamp(r["fire_at"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  • {when} -> {r['text']}")
    else:
        print("No pending reminders.")