"""Main-chat adapter around the bounded Screen Copilot service."""
from __future__ import annotations

import os
from typing import Sequence

import screen_copilot

SCREEN_COPILOT_TOOL = "screen_copilot"
MAX_TARGET_CHARS = 200


def resolve_target(query: str, targets: Sequence[screen_copilot.TargetWindow]):
    """Resolve a natural-language target without guessing between windows."""
    folded = str(query or "").strip().casefold()
    if not folded:
        return None, ()

    def process_names(target):
        executable = str(target.process.executable or "")
        basename = os.path.basename(executable).casefold()
        return tuple(dict.fromkeys((
            executable.casefold(), basename, os.path.splitext(basename)[0],
        )))

    exact_titles = tuple(
        target for target in targets
        if str(target.title or "").strip().casefold() == folded)
    if exact_titles:
        return (exact_titles[0], ()) if len(exact_titles) == 1 else (None, exact_titles)

    exact_processes = tuple(
        target for target in targets if folded in process_names(target))
    if exact_processes:
        return ((exact_processes[0], ()) if len(exact_processes) == 1
                else (None, exact_processes))

    partial = tuple(
        target for target in targets
        if folded in str(target.title or "").casefold()
        or any(folded in name for name in process_names(target)))
    if len(partial) == 1:
        return partial[0], ()
    return None, partial


def run_copilot_capability(args, *, planner, approval_handler, stop_event,
                           progress_handler=None) -> str:
    """Resolve one approved target and run the existing bounded service."""
    if not callable(approval_handler):
        return "ERROR: Screen Copilot requires interactive approval and is unavailable."
    if stop_event is not None and stop_event.is_set():
        return "ERROR: Screen Copilot cancelled."
    if not callable(planner):
        return "ERROR: Screen Copilot planner is unavailable."
    if not isinstance(args, dict):
        return "ERROR: Screen Copilot needs a target and goal object."
    query = str(args.get("target", "") or "").strip()
    goal = str(args.get("goal", "") or "").strip()
    if not query or len(query) > MAX_TARGET_CHARS:
        return f"ERROR: Screen Copilot target must be 1-{MAX_TARGET_CHARS} characters."
    if not goal or len(goal) > screen_copilot.MAX_GOAL_CHARS:
        return ("ERROR: Screen Copilot goal must be 1-"
                f"{screen_copilot.MAX_GOAL_CHARS} characters.")

    try:
        targets = screen_copilot.list_copilot_targets(stop_event=stop_event)
    except Exception:
        return "ERROR: Screen Copilot could not inspect eligible windows."
    selected, candidates = resolve_target(query, targets)
    if selected is None:
        if candidates:
            choices = "; ".join(
                f"{index}. {_target_label(target)}"
                for index, target in enumerate(candidates[:5], 1))
            return ("ERROR: Screen Copilot target is ambiguous. Ask the user to "
                    f"choose one of: {choices}. Then call screen_copilot with "
                    "the exact window title. No actions ran.")
        return ("ERROR: No eligible Screen Copilot window matches that target. "
                "Open the app first or ask for the exact window title. No actions ran.")

    def safe_progress(event):
        if progress_handler is not None:
            progress_handler(format_progress(event))

    try:
        result = screen_copilot.run_copilot_task(
            goal=goal,
            hwnd=selected.hwnd,
            planner=planner,
            approval_handler=approval_handler,
            vision_locator=screen_copilot.deepseek_vision_locator,
            stop_event=stop_event,
            progress_handler=safe_progress,
        )
    except screen_copilot.CopilotBusy:
        return "ERROR: Another Screen Copilot task is already active."
    except screen_copilot.CopilotCancelled:
        return "ERROR: Screen Copilot cancelled."
    except Exception:
        return "ERROR: Screen Copilot task failed."
    return format_result(result)


def _target_label(target) -> str:
    process = os.path.basename(str(target.process.executable or ""))
    title = str(target.title or "Untitled window")
    return f"{title} — {process}"


def format_progress(event) -> str:
    """Translate service events into fixed, privacy-safe status text."""
    if not isinstance(event, dict):
        return "Copilot: Working…"
    name = event.get("event")
    step = event.get("step")
    valid_step = type(step) is int and 1 <= step <= screen_copilot.MAX_STEPS
    if name == "observing" and event.get("status") == "ready":
        return "Copilot: Inspecting the selected window…"
    if name == "planning" and event.get("status") == "started" and valid_step:
        return f"Copilot: Planning step {step}…"
    if name == "action" and event.get("status") == "started" and valid_step:
        return f"Copilot: Performing step {step}…"
    if name == "step_outcome" and event.get("status") == "executed" and valid_step:
        return f"Copilot: Step {step} finished; checking the window…"
    return "Copilot: Working…"


def format_result(result: screen_copilot.CopilotResult) -> str:
    """Return a safe tool observation without internal ids or raw screen data."""
    status = str(getattr(result, "status", "failed") or "failed")
    summary = str(getattr(result, "summary", "") or "").strip()
    steps = getattr(result, "steps", 0)
    if type(steps) is not int or steps < 0:
        steps = 0
    if status != "completed":
        detail = f" {summary}" if summary else ""
        no_actions = (
            " No actions ran." if steps == 0 and status in {
                "denied", "timeout", "cancelled", "target_policy",
            } else ""
        )
        return (f"ERROR: Screen Copilot ended with status '{status}'.{detail}"
                f"{no_actions} This is a terminal result. Do not retry unless "
                "the user asks.")

    changed = getattr(result, "changed", None)
    change_text = {
        True: "yes",
        False: "no",
        None: "unknown",
    }.get(changed, "unknown")
    lines = [
        f"Screen Copilot status: {status}",
        f"Summary: {summary or 'The approved plan finished.'}",
        f"Actions completed: {steps}",
        f"Observed UI change: {change_text}",
    ]
    last_action = str(getattr(result, "last_action", "") or "")
    if last_action:
        lines.append(f"Last action: {last_action}")
    lines.append(
        "Completion is the planner's done signal, not independent verification "
        "of an external effect.")
    return "\n".join(lines)
