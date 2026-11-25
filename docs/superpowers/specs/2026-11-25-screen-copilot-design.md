# Screen Copilot — Approved Adaptive Plan

**Date:** 2026-11-25

**Status:** Approved design; implementation in progress

**Scope:** Kestrel's first screen-aware copilot slice only

**Builds on:** the single-main-screen GUI committed as `25fb74c`

## 1. Goal

Let the operator select one visible native Windows window, enter a short goal,
and approve one bounded adaptive plan. Kestrel then runs at most five
`Observe -> Plan -> Act -> Observe` cycles in that same window. The planner may
click a UIA control, type approved text into a UIA control, request a
target-window-only vision click, or return `done`.

The approval authorizes the goal, selected target, allowed action types, typing
rule, screenshot disclosure, and five-action limit once. It does not authorize
new targets, new action types, arbitrary typed text, elevated processes,
browsers, Kestrel itself, or work beyond the approved envelope.

## 2. In scope

1. List visible, non-elevated, non-browser native windows owned by other
   processes.
2. Select one window explicitly in the Copilot page.
3. Observe a bounded set of invokable and editable UIA controls without reading
   text-box values.
4. Send the operator's goal and safe control metadata to the configured text
   planner, with no general tools.
5. Require one inline **Approve Plan / Deny** decision in the same Kestrel main
   window.
6. Execute up to five adaptive actions: UIA click, UIA typing, or vision click.
7. Permit `done` as a planner action without executing input.
8. Capture only the selected window when a vision click is requested, ask the
   existing DeepSeek vision path for local X/Y coordinates, validate them, and
   perform at most one physical click.
9. Revalidate target, process, observation, control, screenshot bounds, and
   foreground-window identity immediately before each action.
10. Append privacy-preserving audit records before every execution attempt and
    after every outcome.

## 3. Out of scope permanently for this slice

| Capability | Reason |
|---|---|
| Browser windows and browser processes | Separate browser subsystem; prevents accidental duplication of its approval model. |
| Elevated targets | An elevated action requires a separate security design. |
| Kestrel's own process | Prevents self-clicking and recursive UI interaction. |
| Proactive or background observation | Observation begins only after explicit target selection and Run. |
| Reading text-box values, passwords, document contents, clipboard, or files | Observation contains metadata only; typed text must come from the approved goal. |
| Raw desktop tools exposed to the model | The planner returns a validated proposal, never a tool call. |
| Macros, queued plans, arbitrary hotkeys, multi-window plans | The approved envelope is one target and at most five actions. |
| Vision typing, drag/drop, scrolling, right-click, or double-click | Only a single left coordinate click is authorized. |
| Automatic retry after an uncertain action | Fail closed and report the uncertainty. |
| New approval bypasses | Out-of-envelope proposals stop; they are never silently accepted. |

Typing, target-window screenshots, coordinate clicks, adaptive planning, and one
plan-level approval are explicit approved upgrades to the original single-click
design.

## 4. Architecture

```text
CopilotView (same QMainWindow)
  -> CopilotWorker (QObject + daemon thread)
    -> list_copilot_targets() / run_copilot_task()
      -> ScreenCopilotService
        -> UIAAdapter
          -> pywinauto Desktop(backend="uia")
          -> Win32 process identity, bounds, elevation, and foreground checks
          -> ValuePattern typing or literal Unicode SendInput fallback
          -> target-HWND PrintWindow capture and physical left click
        -> Planner(prompt) -> existing DeepSeek text callable
        -> VisionLocator(png, query) -> existing DeepSeek image callable
        -> ApprovalHandler(payload) -> concurrent Future
        -> AuditWriter -> AGENT_MEMORY_DIR/copilot/audit.jsonl
```

The service owns policy and orchestration. The adapter owns Windows/UIA and
physical input. The GUI owns presentation and the single human decision. Tests
can replace the adapter, planner, vision locator, approval handler, and audit
destination without replacing the service's control flow.

## 5. Core contracts

Create `screen_copilot.py` with these stable interfaces:

```python
@dataclass(frozen=True)
class WindowBounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    executable: str
    started_at: int
    elevated: bool


@dataclass(frozen=True)
class TargetWindow:
    hwnd: int
    title: str
    process: ProcessIdentity
    visible: bool
    bounds: WindowBounds


@dataclass(frozen=True)
class Control:
    element_id: int
    runtime_id: tuple[int, ...]
    automation_id: str
    name: str
    control_type: str
    enabled: bool
    visible: bool
    invokable: bool
    editable: bool


@dataclass(frozen=True)
class Observation:
    target: TargetWindow
    controls: tuple[Control, ...]


@dataclass(frozen=True)
class Screenshot:
    png: bytes
    bounds: WindowBounds
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class VisionPoint:
    x: int
    y: int


@dataclass(frozen=True)
class Proposal:
    action: str                 # "click", "type_text", "vision_click", or "done"
    element_id: int | None = None
    text: str = ""
    query: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PlanEnvelope:
    task_id: str
    target_hwnd: int
    goal: str
    max_steps: int
    allowed_actions: frozenset[str]
    vision_enabled: bool


@dataclass(frozen=True)
class CopilotResult:
    status: str
    summary: str
    task_id: str
    steps: int = 0
    changed: bool | None = None
    last_action: str = ""


class UIAAdapter(Protocol):
    def list_windows(self) -> list[TargetWindow]: ...
    def observe(self, hwnd: int) -> Observation: ...
    def invoke(
        self,
        expected: Observation,
        control: Control,
        should_stop: Callable[[], bool] | None = None,
    ) -> None: ...
    def set_text(
        self,
        expected: Observation,
        control: Control,
        text: str,
        should_stop: Callable[[], bool] | None = None,
    ) -> None: ...
    def capture(
        self,
        expected: Observation,
        should_stop: Callable[[], bool] | None = None,
    ) -> Screenshot: ...
    def vision_click(
        self,
        expected: Observation,
        screenshot: Screenshot,
        point: VisionPoint,
        should_stop: Callable[[], bool] | None = None,
    ) -> None: ...


Planner = Callable[[str], str]
VisionLocator = Callable[[bytes, str], str]
ApprovalHandler = Callable[[dict], bool | Future[bool]] | None
ProgressHandler = Callable[[dict], None] | None
```

`runtime_id` is valid only for same-process, same-run identity. It is combined
with HWND, PID, process start time, executable, title hash, control type,
automation ID hash, label hash, capabilities, window bounds, and the complete
observation digest.

## 6. Constants and limits

```python
MAX_CONTROLS = 300
MAX_SCANNED = 2000
MAX_LABEL_CHARS = 120
MAX_AUTOMATION_ID_CHARS = 160
MAX_GOAL_CHARS = 500
MAX_TYPE_TEXT_CHARS = 200
MAX_VISION_QUERY_CHARS = 120
MAX_REASON_CHARS = 200
MAX_STEPS = 5
APPROVAL_TIMEOUT_SECONDS = 60.0
CANCEL_POLL_SECONDS = 0.1
MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
MAX_SCREENSHOT_PIXELS = 8_000_000
```

Limits are enforced in code, not only in prompts. A proposal that exceeds a
limit is rejected before approval or execution.

## 7. Target policy

A target is eligible only when every condition below is true:

- The window is visible, not minimized, and has positive width and height.
- The PID is not Kestrel's PID.
- Win32 process identity can be read: PID, executable basename, creation time,
  and token elevation.
- `TokenElevation.TokenIsElevated` is exactly zero.
- The executable basename is not in the browser deny set:
  `chrome.exe`, `msedge.exe`, `firefox.exe`, `opera.exe`, `operagx.exe`,
  `brave.exe`, `vivaldi.exe`, `arc.exe`, or `iexplore.exe`.
- The window has a non-empty title.
- `GetWindowRect` returns a valid rectangle with positive dimensions that
  intersects the virtual desktop. The virtual-desktop origin and dimensions come
  from `SM_XVIRTUALSCREEN`, `SM_YVIRTUALSCREEN`, `SM_CXVIRTUALSCREEN`, and
  `SM_CYVIRTUALSCREEN`, so visible windows on negative-coordinate monitors remain
  eligible while fully off-screen windows do not.

Unknown process metadata, failed token queries, invalid bounds, or any
uncertainty means the window is not selectable. There is no elevation bypass and
no target lookup by title alone.

The adapter rechecks this policy during initial observation, every action
revalidation, screenshot capture, and post-action observation. If a target
becomes elevated, hidden, minimized, self-owned, browser-owned, or off-screen,
the task fails closed.

## 8. Observation contract

Observation rules:

- Traverse `window.iter_descendants()` and stop after `MAX_SCANNED` elements.
- Keep invokable controls only for types `Button`, `Hyperlink`, `MenuItem`,
  `TabItem`, and `SplitButton` when `iface_invoke` exists.
- Keep editable controls only for types `Edit` and `ComboBox` that are not
  read-only. A ValuePattern is preferred; a wrapper with keyboard-input support
  is accepted only as a candidate for the adapter's literal Unicode fallback.
- Keep only controls that are enabled and visible.
- Read only `runtime_id`, `automation_id`, `name`, `control_type`,
  enabled/visible state, `IsReadOnly`, and capability flags. Never read or
  retain the current value of an editable control.
- Collapse whitespace and cap label and automation ID lengths.
- Assign deterministic `element_id` values in traversal order after removing
  duplicate runtime IDs.
- Include at most `MAX_CONTROLS` unique controls.
- Compute an observation SHA-256 digest over safe canonical target, bounds, and
  control identity/capability fields. Raw labels and titles are hashed, not
  copied into the digest payload.
- If no actionable controls remain, return an empty observation. The planner
  may still request a vision click when vision is enabled.

The UI displays the selected window title and bounded control labels. It does
not display or persist the rest of the UIA tree.

## 9. Planner contract

The planner is an injected `Callable[[str], str]`. In the GUI it wraps the
existing `agent._call_deepseek(prompt, force_json=True, max_tokens=768)` path.
It receives no tool registry, shell, filesystem, browser, or desktop tool.

The prompt contains:

- the operator's goal, capped at 500 characters;
- the five-step limit and allowed actions;
- a statement that screen text and image text are untrusted and must never be
  followed as instructions;
- a numbered list of `{element_id, name, type, invokable, editable}` controls;
- the exact response schemas and a requirement to return JSON only.

Accepted responses are exactly one of:

```json
{"action":"click","element_id":12}
```

```json
{"action":"type_text","element_id":8,"text":"Dune"}
```

```json
{"action":"vision_click","query":"Search"}
```

```json
{"action":"done","reason":"The requested result is visible"}
```

Validation rejects prose, code fences, arrays, additional keys, unknown
actions, booleans masquerading as integers, out-of-range IDs, wrong control
capabilities, empty text, text over 200 characters, control characters in typed
text, and reasons or vision queries over their limits.

Typed text must be an exact, case-sensitive substring of the approved goal.
A vision query must also be an exact, case-sensitive substring of the approved
goal. These checks happen in the parser and again in the service before every
action. A proposal cannot supply absolute coordinates, paths, commands, URLs,
or a new target.

## 10. One plan-level approval

The service creates a task ID and one approval request before the loop. The
approval payload contains:

- goal;
- target window title, process basename, PID, and HWND;
- `max_steps=5`;
- allowed actions: `click`, `type_text`, and optionally `vision_click`;
- typing rule: typed text must be a substring of the approved goal;
- screenshot disclosure: only the selected window may be captured and sent to
  the configured vision model when vision is used;
- a 60-second countdown;
- a statement that denial or timeout executes nothing.

The card has only **Approve Plan** and **Deny**. It has no default-accept option
and does not accept Enter as approval. Its timer resolves the future to
`False` after 60 seconds. The service independently enforces the same timeout,
so a broken or missing card cannot turn timeout into approval.

Approval creates an immutable `PlanEnvelope`. Every later proposal is checked
against it. A new target, disallowed action type, non-goal typed text,
non-goal vision query, changed step cap, or attempt to enable vision after it
was denied stops the task with `out_of_envelope`.

The following resolve to no execution:

- Deny;
- timeout;
- Copilot Stop;
- Escape while the Copilot page is active;
- global kill switch `Ctrl+Alt+Shift+Esc`;
- approval handler missing or failing;
- planner or validation failure;
- policy failure;
- audit failure before an execution attempt;
- target, control, bounds, screenshot, or foreground drift.

Only one copilot task may be active. Starting another task returns a busy
error. Starting a new chat cancels an active copilot task and returns to the
chat page so a plan cannot be approved off-screen.

Cancellation during a planner or vision network request cannot recall an
already-sent request. It prevents the next action; the worker returns after the
bounded provider call completes. Cancellation during approval polling is
checked every 100 ms.

## 11. Adaptive loop

`ScreenCopilotService.run(goal, hwnd, ...)` executes this order:

1. Check cancellation and kill switch. Reject an empty goal and cap the
   planner copy at `MAX_GOAL_CHARS`.
2. Observe the target and enforce policy.
3. Append a safe `inspection` audit record.
4. Build and present one approval envelope.
5. Resolve approval; return without action when denied, timed out, or
   cancelled.
6. Freeze the approved `PlanEnvelope`.
7. For up to five plan-act cycles:
   - observe the same target;
   - build the planner prompt from the approved goal and current observation;
   - call the planner and check cancellation;
   - parse and validate the proposal against the observation and envelope;
   - if `done`, append `planner_done` and return `completed`;
   - if `click`, `type_text`, or `vision_click`, execute Section 12;
   - observe again after the action and report progress;
   - stop immediately on any policy, audit, drift, cancellation, or provider
     failure.
8. If five actions complete without `done`, return `max_steps_reached` and do
   not perform a sixth action.

`done` is a planner assertion, not proof of an external effect. The summary
must say that the planner marked the goal complete. `max_steps_reached` must
not be presented as success.

## 12. Action execution and revalidation

Every action follows the same mandatory order:

1. Re-observe the selected HWND.
2. Require the same process identity, target policy, window bounds, and
   observation digest that the planner saw.
3. For UIA actions, require the selected control's runtime ID, safe identity
   fields, and capability flags to match.
4. Validate the proposal again against the immutable envelope.
5. Append and fsync an `execution_attempt` audit record.
6. Call the adapter action with `should_stop`.
7. The adapter repeats target/control revalidation and checks `should_stop`
   immediately before the single UIA call or physical click. The keyboard
   fallback and vision click also re-read target identity and foreground root
   after final activation, immediately before input.
8. Catch drift or cancellation before claiming an action occurred. Cancellation
   raised at an observation or post-provider boundary is normalized to
   `cancelled_before_action` and receives a `step_outcome` record.
9. Observe again and append a safe `step_outcome` record. A late audit failure
   after any earlier or uncertain action is reported as
   `audit_failed_after_execution`, never as proof that nothing ran.

There is no retry, alternate lookup by title, or second action. If revalidation
fails after the audit record, the outcome is `drift_blocked` or
`cancelled_before_action`; the audit record represents an authorized attempt
that was not performed.

### UIA click

- The control must be invokable.
- The adapter calls `UIAWrapper.invoke()` exactly once.
- A raised exception returns `click_failed`; the action outcome may be unknown.

### UIA typing

- The control must be editable and not read-only.
- The typed text must be a non-empty exact substring of the approved goal.
- Prefer `element.iface_value.SetValue(text)`.
- If ValuePattern is unavailable, the adapter focuses the same UIA control,
  activates and verifies the approved target root, revalidates HWND/PID/process
  identity and bounds, checks `should_stop()`, then sends the UTF-16 code units
  once through Win32 `SendInput` with `KEYEVENTF_UNICODE` and key-up events.
  This fallback inserts at the current caret and does not clear existing text;
  it never uses pywinauto `type_keys`, clipboard paste, or arbitrary hotkeys.
  Strings such as `%`, `+`, `^`, `~`, and `{ENTER}` therefore remain literal
  characters rather than keyboard commands.
- A raised exception returns `type_failed`; Kestrel does not claim the text was
  applied and does not retry.

### Vision click

Vision is used only when `vision_click` is in the approved envelope and a
vision locator is configured.

1. The service asks the adapter to capture only the approved target window.
2. The adapter revalidates the target and observation, activates it, verifies
   the foreground root HWND, and uses Win32 `PrintWindow(hwnd, hdc,
   PW_RENDERFULLCONTENT)` into a compatible DIB. It does not use a desktop
   screen grab or pywinauto `capture_as_image`, either of which can include
   overlapping foreign windows.
3. The bitmap dimensions must equal the revalidated `WindowBounds`. Capture
   must become PNG of at most 16 MiB and at most 8,000,000 pixels. GDI resources
   are released in `finally`. The screenshot object holds bytes only in memory
   for the current vision step.
4. The service appends a safe `vision_capture` record containing dimensions,
   bounds, and screenshot hash, never the image.
5. The service sends the PNG bytes and bounded query to the vision locator.
   The default locator writes a temporary PNG with restrictive permissions,
   calls the existing `agent.describe_image()` path, rejects empty/non-string
   provider errors as `vision_locator_failed`, and deletes the temporary file in
   `finally`.
6. The response must be strict JSON:

```json
{"x":123,"y":45}
```

or:

```json
{"action":"not_found","reason":"The requested control is not visible"}
```

7. Coordinates are local pixels from the screenshot's top-left. Both must be
   integers, not booleans, with `0 <= x < width` and `0 <= y < height`.
8. The adapter re-observes the target and requires the same process identity,
   policy, observation, and window bounds. It captures the window again and
   requires the same screenshot hash, dimensions, and bounds. After final
   activation it re-reads the target identity and bounds and verifies the
   foreground root HWND, checks `should_stop()`, and performs exactly one
   physical left click at the freshly validated `bounds.left + x`,
   `bounds.top + y`.
9. Any capture, model, parse, bounds, hash, foreground, or input failure blocks
   the click and returns a fixed failure status.

The temporary PNG is deleted after the model call even when the call fails.
Screenshots are never written to audit, history, chat, task workspace, or the
result. A screenshot hash and dimensions are sufficient for audit.

## 13. Verification and progress

After every action, the loop performs a fresh UIA observation. A changed digest
sets `changed=True`; an unchanged digest sets `changed=False`; an unreadable
target sets `changed=None`. These values describe visible UIA state only.

An unchanged UIA tree is not proof that the application did nothing. An
external or visual effect may not be represented in UIA. The final summary must
preserve that distinction.

The service emits safe progress events such as `observing`, `planning`,
`typing`, `clicking`, `vision_capture`, `vision_locating`, and `done`. Events
contain step number, action type, and status only, never raw screen text,
typed text, model responses, or screenshots.

## 14. Audit contract

Audit path:

```text
%AGENT_MEMORY_DIR%/copilot/audit.jsonl
```

Use append-only JSONL with `O_APPEND`, a per-writer lock, flush, and `fsync`.
The writer recursively drops raw-data keys (`goal`, `title`, `name`, `label`,
`automation_id`, `raw_title`, `raw_label`, `response`, `model_response`,
`exception`, `error_text`, `uia_text`, `value`, `text`, `query`, `png`,
`screenshot`, `image`, and `bytes`) as defence in depth. Each record has
`schema=1`, UTC timestamp, task ID, event, and event-specific safe fields.

Events:

| Event | Required safe fields |
|---|---|
| `inspection` | HWND, PID, process basename/start time, title hash, bounds, observation hash, control count |
| `approval_requested` | request ID, envelope hash, max steps, allowed actions, vision enabled |
| `decision` | request ID, `allow`, `deny`, `timeout`, `cancel`, or `error` |
| `step_proposal` | step, observation hash, action, safe control identity or text/query hash |
| `execution_attempt` | step, observation hash, safe control identity or screenshot hash, local coordinates |
| `vision_capture` | step, screenshot hash, dimensions, bounds |
| `vision_locator` | step, query hash, status, local coordinates when valid |
| `step_outcome` | step, action, status, changed, fixed error code |
| `planner_done` | step count, final observation hash |
| `outcome` | status, steps, changed, last action, fixed error code |

Safe control identity contains runtime ID hash, automation ID hash, label hash,
control type, and capability flags. Raw titles, labels, goals, typed text,
vision queries, UIA values, model responses, screenshots, and exception strings
are never written. Process basename, PID, HWND, timestamps, bounds, hashes,
local coordinates, statuses, and fixed error codes are permitted.

An audit failure before `execution_attempt` blocks the action. An audit failure
after an action returns `audit_failed_after_execution` and explicitly reports
that the action may have occurred while its outcome record could not be saved.

## 15. GUI integration

Modify `agent_gui_qt.py` without creating another top-level window:

- Add a seventh `NavRail` destination: **Copilot**.
- Replace the chat-only conversation slot with a `QStackedWidget` containing
  `ChatView` and a new `CopilotView`.
- Keep `OwlStage`, `TaskWorkspace`, `StateBar`, title bar, and the 920 px
  conversation column unchanged.
- Hide the normal composer while Copilot is active; show it on return to
  chat.
- `CopilotView` contains Refresh windows, a target selector, a short goal
  field, Run plan, Stop, progress/status text, and one approval area.
- Show the **Approve Plan** card inside `CopilotView`, not in a `QDialog`.
- Use the existing queued-signal worker pattern. All widget mutation happens
  on the GUI thread.
- Route copilot stages through `TaskWorkspace.set_activity()` and HUD state
  through the existing `_sync_hud_state()` path.
- Extend Stop, Escape, new-chat, and shutdown handling to cancel the copilot.
- Register `screen_copilot.cancel_current_task` with the existing kill-switch
  callback API during OS-control setup.

Privacy copy on the page must state that safe UIA labels and the approved goal
are sent to the configured text model; when vision is approved and used, only
the selected window image is sent to the configured vision model and the
temporary screenshot is deleted after the call. Text-box values are never read
or sent.

## 16. Error reporting

Use short fixed statuses and human-readable summaries:

- `no_targets`: No eligible native windows were found.
- `policy_blocked`: Target policy failed.
- `planner_failed`: The model call failed.
- `invalid_proposal`: The model response violated the schema.
- `out_of_envelope`: A proposal exceeded the approved goal, target, actions, or
  step limit.
- `denied`, `timeout`, `cancelled`: No action was executed.
- `busy`: Another copilot task is active.
- `drift_blocked`: Target, control, bounds, screenshot, or foreground identity
  changed before execution.
- `cancelled_before_action`: Cancellation won the final race check.
- `click_failed`: UIA invocation raised; outcome unknown.
- `type_failed`: UIA typing raised; outcome unknown.
- `vision_capture_failed`: The approved window could not be captured safely.
- `vision_locator_failed`: The vision model call failed.
- `vision_not_found`: The vision model did not find the requested control.
- `vision_invalid`: The vision response or coordinates were invalid.
- `vision_click_failed`: Physical click raised; outcome unknown.
- `audit_failed`: Audit failed before execution.
- `audit_failed_after_execution`: Action may have occurred; outcome audit failed.
- `max_steps_reached`: Five actions completed without `done`.
- `completed`: The planner returned `done`; this is not independent proof of an
  external effect.

No error message includes raw UIA text, typed text, a vision query, a model
response, or an exception string.

## 17. Testing and acceptance

### Unit tests

`tests/test_screen_copilot.py` covers:

- target policy accepts a safe target and rejects self, hidden, minimized,
  invalid bounds, elevated, and each denied browser process;
- observation digest changes for title, process start time, bounds, control
  label, automation ID, runtime ID, type, capability, enabled, visible, or
  control-set changes;
- observation never reads or retains an editable control's current value;
- planner prompt contains no tool schemas and marks screen text untrusted;
- strict proposal validation accepts all four schemas and rejects malformed,
  wrong-capability, non-goal text, non-goal query, and extra-key forms;
- vision response validation rejects booleans, floats, negatives, and
  out-of-bounds coordinates;
- audit output contains hashes, dimensions, bounds, and fixed metadata but none
  of the raw title, label, goal, typed text, query, model response, or image;
- one approval is requested once and five actions are the hard maximum;
- deny, timeout, missing handler, approval error, and cancellation execute zero
  times;
- `done` completes without an action;
- target, control, bounds, screenshot, and foreground drift execute zero times;
- typed text outside the approved goal is rejected before action;
- audit failure before execution blocks the action;
- outcome audit failure after action is reported honestly;
- capture failure, locator failure, `not_found`, invalid coordinates, and
  physical-click failure each block or report honestly without retry;
- changed, unchanged, and unreadable post-action observations remain distinct;
- cancellation and kill switch are checked at every planning and action
  boundary, including after a vision request and before physical input;
- literal keyboard fallback sends Unicode code units rather than key syntax;
- target-HWND capture does not call desktop capture;
- off-screen windows are rejected while negative-coordinate desktop windows
  remain eligible;
- target movement or foreground loss after final activation blocks input;
- provider error strings, boundary cancellation, and late audit failures retain
  their fixed, honest statuses.

### Offscreen GUI tests

`tests/test_gui_copilot.py` covers:

- one `QMainWindow`, no `QDialog`, no splitter, owl and task area retained;
- Copilot navigation switches the stack and hides the composer;
- returning to chat restores the composer;
- the approval card shows one plan envelope and defaults to deny;
- timeout resolves false once;
- Approve Plan resolves true once and removes the card;
- there is no per-action approval callback;
- Stop and Escape cancel through the worker's stop event;
- starting a new chat cancels and leaves the Copilot page.

### Live Windows test

A self-targeted WinForms fixture, following `tests/test_uia_control.py`,
provides one button, one search box, and one result label. The test proves:

- the production adapter lists and observes the fixture;
- a two-action approved plan types `Dune` into the search box and clicks the
  result button;
- the label proves both actions occurred exactly once;
- a denied plan changes nothing;
- the audit file contains no raw fixture title, button label, goal, or typed
  text;
- the vision adapter captures only the fixture bounds and validates local
  coordinates; physical vision clicking is tested with a stateful fake unless
  a safe self-targeted fixture proves foreground and bounds revalidation.

Skip live classes only when Windows or pywinauto is unavailable. Do not
install or upgrade packages as part of this phase.

### Regression gates

Use pyenv Python 3.11.9, not the stale `.venv`:

```bash
python -m unittest tests.test_screen_copilot -v
python -m unittest tests.test_gui_copilot -v
python -m unittest tests.test_uia_control -v
python -m unittest tests.test_gui_single_screen -v
python -m unittest discover -s tests -t tests -p "test_*.py"
```

The live native GUI smoke must also show the Copilot page, one Approve Plan
card, progress state, and result state in the single Kestrel window. A test-only
screenshot of Kestrel's own window is permitted as verification evidence.
Compilation and unit tests alone are not acceptance.

## 18. File impact and isolation

Expected production files:

- Create `screen_copilot.py`.
- Modify `agent_gui_qt.py`.

Expected test files:

- Create `tests/test_screen_copilot.py`.
- Create `tests/test_gui_copilot.py`.
- Create `tests/gui_copilot_smoke.py`.
- Extend existing GUI regression expectations only where the seventh
  destination intentionally changes them.

Do not modify the concurrent sandbox/verification work in `agent.py`,
`config.py`, `tool_registry.py`, `verification_adapter.py`,
`autonomous_loop.py`, `office_editor.py`, `prompt_catalog.py`,
`python_sandbox.py`, or their tests. The copilot may call existing DeepSeek
helpers through injected closures but must not change them.

## 19. Known limits

- UIA support varies by application. Unsupported controls are omitted rather
  than approximated.
- Keyboard typing fallback inserts at the current caret and does not clear
  existing text. ValuePattern is preferred because it replaces the value.
- Vision can locate only what is visible in the selected window. It cannot type,
  scroll, or prove an external effect.
- A changed screenshot hash is required immediately before a vision click.
  Animations or live content can therefore block a click safely.
- Runtime IDs are not stable across application restarts; every task starts
  with a fresh observation.
- The planner sees safe UIA labels and the approved goal. Those strings can
  contain sensitive application text, so the page discloses model transmission
  and the feature remains operator-initiated.
- This is an approved adaptive plan, not unrestricted desktop autonomy.

## 20. References

- Approved phase scope: `C:\Users\USER\.qoder\projects\C--Users-USER-AgentBot\memory\agentbot-phased-roadmap.md`.
- Existing approval pattern: `browser_agent.py` and `BrowserApprovalCard` in
  `agent_gui_qt.py`.
- Existing live UIA fixture: `tests/test_uia_control.py`.
- Existing DeepSeek vision path: `agent.describe_image()` and
  `agent._call_deepseek()`.
- pywinauto 0.6.9 API verified against installed source and official docs:
  `Desktop.window(handle=...)`, `iter_descendants()`, `runtime_id`,
  `is_enabled()`, `is_visible()`, `iface_invoke`, and `iface_value.SetValue()`.
- Windows APIs required for policy, capture, and input: `OpenProcess`,
  `QueryFullProcessImageNameW`, `GetProcessTimes`, `OpenProcessToken`,
  `GetTokenInformation(TokenElevation)`, `GetWindowRect`, `IsIconic`,
  `GetSystemMetrics`, `GetForegroundWindow`, `GetAncestor`,
  `SetForegroundWindow`, `GetWindowThreadProcessId`, `PrintWindow`,
  `SendInput`, and `CloseHandle`.
