"""
Proactive/scheduled tasks for the agent.

Three kinds of scheduled items:
  1. Daily tasks  - (time "HH:MM", request) pairs, repeat every day.
  2. Reminders    - one-shot, fire once then delete themselves.
  3. Cron jobs    - recurring jobs described by a 5-field cron expression.

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
CRON_FILE = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'cron_jobs.json')


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
# Recurring cron jobs
# ---------------------------
def load_cron_jobs():
    if not os.path.exists(CRON_FILE):
        return []
    try:
        with open(CRON_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('jobs', [])
    except (json.JSONDecodeError, OSError):
        return []


def save_cron_jobs(jobs):
    os.makedirs(os.path.dirname(CRON_FILE), exist_ok=True)
    with open(CRON_FILE, 'w', encoding='utf-8') as f:
        json.dump({'jobs': jobs}, f, indent=2)


def add_cron_job(cron_expr, request, label=""):
    """Add a recurring job. Returns (ok, error_or_index)."""
    if cron_match(cron_expr, datetime.now()) is None and not validate_cron(cron_expr):
        return False, f"Invalid cron expression '{cron_expr}'."
    jobs = load_cron_jobs()
    for j in jobs:
        if j["cron"] == cron_expr and j["request"] == request:
            return False, "An identical job already exists."
    jobs.append({
        "cron": cron_expr,
        "request": request,
        "label": label or request[:40],
        "last_fired_minute": None,
        "created_at": time.time(),
    })
    save_cron_jobs(jobs)
    return True, len(jobs) - 1


def remove_cron_job(index):
    jobs = load_cron_jobs()
    if 0 <= index < len(jobs):
        jobs.pop(index)
        save_cron_jobs(jobs)
        return True
    return False


def _field_matches(field, value, lo, hi):
    """Match one cron field against an int. Supports *, */n, a-b, a-b/n, lists."""
    for part in field.split(','):
        part = part.strip()
        if not part:
            continue
        step = 1
        if '/' in part:
            part, step_s = part.split('/', 1)
            try:
                step = int(step_s)
            except ValueError:
                return False
            if step <= 0:
                return False
        if part == '*':
            if (value - lo) % step == 0:
                return True
            continue
        if '-' in part:
            a_s, b_s = part.split('-', 1)
            try:
                a, b = int(a_s), int(b_s)
            except ValueError:
                return False
            if a <= value <= b and (value - a) % step == 0:
                return True
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            return False
    return False


def validate_cron(cron_expr):
    """True if the expression is a syntactically usable 5-field cron."""
    if not cron_expr:
        return False
    fields = cron_expr.split()
    if len(fields) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for f, (lo, hi) in zip(fields, ranges):
        # _field_matches returns bool; use a probe value to catch malformed fields
        if not isinstance(_field_matches(f, lo, lo, hi), bool):
            return False
        for ch in f:
            if ch not in "0123456789*,-/":
                return False
    return True


def cron_match(cron_expr, when=None):
    """Return True if the 5-field cron expression matches `when` (a datetime).

    Fields: minute hour day-of-month month day-of-week (0=Sunday).
    Day-of-month and day-of-week are ANDed (simpler than Vixie's OR rule).
    Returns None if the expression is invalid.
    """
    if not validate_cron(cron_expr):
        return None
    when = when or datetime.now()
    minute, hour, dom, month, dow = cron_expr.split()
    # Python weekday(): Monday=0..Sunday=6 -> convert to 0=Sunday
    py_dow = (when.weekday() + 1) % 7
    return (
        _field_matches(minute, when.minute, 0, 59)
        and _field_matches(hour, when.hour, 0, 23)
        and _field_matches(dom, when.day, 1, 31)
        and _field_matches(month, when.month, 1, 12)
        and _field_matches(dow, py_dow, 0, 6)
    )


def describe_cron(cron_expr):
    """Human-readable summary of a cron expression, or None if invalid."""
    if not validate_cron(cron_expr):
        return None
    minute, hour, dom, month, dow = cron_expr.split()
    if minute.startswith('*/') and hour == '*' and dom == '*' and month == '*' and dow == '*':
        return f"every {minute[2:]} minutes"
    if minute == '*' and hour == '*' and dom == '*' and month == '*' and dow == '*':
        return "every minute"
    if minute.isdigit() and hour == '*' and dom == '*' and month == '*' and dow == '*':
        return f"hourly at :{int(minute):02d}"
    if minute.isdigit() and hour.isdigit() and dom == '*' and month == '*' and dow == '*':
        return f"daily at {int(hour):02d}:{int(minute):02d}"
    if minute.isdigit() and hour.isdigit() and dom == '*' and month == '*' and dow != '*':
        names = "Sun Mon Tue Wed Thu Fri Sat".split()
        try:
            day = names[int(dow)] if dow.isdigit() else dow
        except (ValueError, IndexError):
            day = dow
        return f"weekly on {day} at {int(hour):02d}:{int(minute):02d}"
    return f"cron '{cron_expr}'"


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

            # --- recurring cron jobs ---
            jobs = load_cron_jobs()
            if jobs:
                stamp = datetime.now()
                minute_key = stamp.strftime("%Y-%m-%dT%H:%M")
                changed = False
                for j in jobs:
                    if j.get("last_fired_minute") == minute_key:
                        continue
                    if cron_match(j["cron"], stamp):
                        j["last_fired_minute"] = minute_key
                        changed = True
                        print(f"[Scheduler] Cron firing: {j['cron']} -> {j['request'][:60]}")
                        safe_trigger(j["request"])
                if changed:
                    save_cron_jobs(jobs)

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