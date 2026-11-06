# Desktop Pet Animation — Design

Date: 2026-11-03
Status: Approved design, pending implementation plan
Scope: P1–P4 of the pet roadmap. P5 (generated motion via Kling AI) is explicitly out of scope.

## 1. Problem

`PetWidget` today draws exactly one PNG per state. The pet bobs, blinks by
swapping to a whole different sprite, and wanders on a timer, but it never
animates: a walking pet is a still image sliding sideways.

We have 22 poses to work from across three reference sheets, and the owner wants
a pet that walks, flies, blinks mid-stride, sleeps when left alone, and comes to
the front of the chat when the cursor approaches.

### Constraints established earlier in this work, still binding

- The pet must never block text input. `WA_TransparentForMouseEvents` stays on.
- The HUD's existing tick is the only animation clock. No new `QTimer`.
- True-alpha compositing only: `CompositionMode_SourceOver`, never `Source`, no
  opaque clear. The pet may not darken the chat canvas.
- The pet sits behind the message body by default.

## 2. Why not the obvious alternatives

**`QMovie` / animated GIF.** Adds a second timer, gives up control of the
compositing mode, and makes "blink mid-stride" impossible because a blink has to
*substitute* a frame rather than interrupt playback. Rejected.

**Slicing the supplied sheets.** Measured: all four attached images are
`mode=RGB` with alpha range 255–255 and 0% transparent pixels — the transparency
checkerboard is painted into the pixels. A naive colour key masks 64–74% of the
frame correctly but destroys the near-white cores of the glow lines. Rejected in
favour of generating fresh poses from the clean PNGs we already own.

**Third-party skills.** `TamerinTECH/claude-skill-klingai-animation` produces
*CSS* sprite animations via Kling AI video generation plus `ffmpeg`; there is no
`ffmpeg` on this machine and no Kling key in `.env`. The `openai` game-studio
skills are nine web/JavaScript skills. Neither targets PySide6. Borrowed: the
method (generate motion, extract frames, sequence them). Not installed.

## 3. State model

`PetWidget` currently stores one drawn name. It is replaced by three orthogonal
values:

| Field | Meaning |
|---|---|
| `pose` | `idle` / `walking` / `flying` / `thinking` / `reading` / `speaking` / `sleeping` / `looking` |
| `frame_index` | Which frame of that pose's cycle is showing |
| `blinking` | Whether the current frame is being replaced by its blink twin |

The drawn file is `frames/<pose>/<pose>_<frame_index>.png`, or
`frames/<pose>/<pose>_1_blink.png` while `blinking` is true.

**Blink semantics, settled explicitly.** A blink freezes `frame_index` and shows
the pose's single blink twin for `PET_BLINK_DURATION` (0.18 s). It does *not*
substitute the exact current frame. At 0.18 s the freeze is not perceptible, and
this keeps the asset count at one twin per pose instead of one per frame. A
walking pet therefore blinks mid-stride.

`blinking` is a flag, not a pose — the old model had it as a state name and that
goes away. Consequence: `PET_STATES` loses `blinking`, `set_pet_state("blinking")`
becomes an unknown name that falls back to `idle`, and the existing
`test_the_pet_state_set_matches_the_committed_sprites` assertion changes because
`blinking.png` is no longer a shipped pose. `sleeping` is a pose with no twin
because its eyes are already shut.

**Amended after the v2 art review.** `thinking` and `reading` ship twins with a
punched-out hole (in the arm over the laptop, and beside the book). Until that
art is redrawn, `PET_BLINK_SKIP` holds their eyes open instead of flashing the
hole every few seconds. Every other awake pose still blinks. Removing the two
names from that tuple restores blinking the moment sound twins land.

The public API keeps its current shape and names, so the tool-based mapping
committed in `8963165` still works unchanged — with one exception, described in
§6: the mic-sleeping timeout stops mapping to `sleeping`. `walking` and `flying`
are never requested from outside; the pet chooses them.

## 4. Assets

```
assets/pet/
  frames/
    idle/       idle_1.png  idle_1_blink.png
    walking/    walking_1.png … walking_4.png  walking_1_blink.png
    flying/     flying_1.png … flying_3.png   flying_1_blink.png
    thinking/   thinking_1.png thinking_2.png thinking_1_blink.png
    reading/    reading_1.png reading_2.png reading_1_blink.png
    speaking/   speaking_1.png speaking_2.png speaking_1_blink.png
    sleeping/   sleeping_1.png
    looking/    looking_1.png … looking_4.png
```

25 files. Frame lists are discovered by globbing `<pose>_<n>.png` in sorted
numeric order; blink twins are excluded from the cycle.

Timing is a code table, mirroring how `ReactorHud._STYLES` already works, so
there is no new configuration surface:

| Pose | Frames | fps | Blink twin |
|---|---|---|---|
| idle | 1 | — | yes |
| walking | 4 | 6 | yes |
| flying | 3 | 8 | yes |
| thinking | 2 | 2 | yes |
| reading | 2 | 2 | yes |
| speaking | 2 | 8 | yes |
| sleeping | 1 | — | no |
| looking | 4 | 2 | no |

**Amended after the v2 art review.** Speaking now runs at 8 fps rather than 6:
its two frames are a mouth, and 8 fps makes the pulse beat four times a second,
roughly the syllable rate of the TTS it trails. `looking` is the idle-variation
cycle (§6) and ships no twin — it is already a "looking around" animation.

**Amended at approval.** `idle_2` was cut. A two-frame idle at 1.5 fps is a
1.5 s hard swap between near-identical drawings, which reads as a stutter rather
than as breathing; the bob already carries the breathing. Idle is one pose plus
its blink twin. `flying_1_blink` was kept as generated even though it matches
pose 2's wing angle rather than pose 1's: at 8 fps the 0.18 s blink is inside one
flap, and regenerating it produced a standing wing-raise, which is worse.

**Migration.** The seven existing flat PNGs in `assets/pet/` become generation
references only. Once `frames/` is approved, the loader reads `frames/`
exclusively and the flat files are deleted in the same commit, so there is never
a window with two sources of truth. `blinking.png` is discarded outright: it is a
head-only closeup at a different scale from every full-body pose and cannot be
swapped in or overlaid without looking broken.

## 5. Auto-alignment

**Amended during batch 1.** Generated frames arrive at different scales relative
to their own canvas — the idle sheet fills 79% of its height, the walk sheet 57%.
Scaling each frame to fit the canvas would make the bird grow and shrink between
frames, which is the exact wobble alignment exists to remove. The normaliser is
therefore the **head**, not the bounding box: legs and wings change height
legitimately between poses, the head does not.

Measured on batch 1 after head-width normalisation: mean head width idle 108.7 px
against walk 109.5 px, a 0.8% difference, with every frame's feet landing on rows
284–285.

**Amended during batch 2.** Batch 1 was in fact built with a per-sheet
*bounding-box* fit, and that head agreement was a property of two similar sheets,
not of the method. With props in the frame — a laptop, a book, sound waves — and
with horizontal poses, bbox fit put `reading_1` at 303 px on a 286 px canvas and
clipped the crest. Normalising each frame by its own head was worse: a raised
wing sits in the head band, so `flying_3` measured 121 against its siblings'
145–217 and came out 40% too big. The rule is now **one scale per sheet** — the
generator drew every pose of a cycle at one scale inside one image, so a
per-frame scale would reintroduce the wobble — and the head *median* of a sheet
is what matches that sheet to the others. One global target of 86.7 px head
width, the largest that lets all 21 frames fit the canvas, re-cuts batch 1 about
15% smaller than it shipped so every pose agrees. Feet still land on rows
284–285.

The build step is:

1. Matte the key field to alpha (flood fill from the edges, feathered — the
   method proven on the JPEG sources, where it left 0.01–0.46% fringe).
2. Remove magenta residue. Measured: the generator's ground line and any pink
   fringe have **green as their minimum channel** (G 5–28 against R,B 51–219),
   which cyan and navy art never does. That one rule clears residue across the
   whole frame without touching the sprite, and it is what made column slicing
   possible at all.
3. Trim to the bounding box of pixels with alpha > 8.
4. Scale by the shared head-width normaliser and paste onto a common
   **300×300** canvas, horizontally centred, with the bbox bottom edge resting on
   a fixed baseline at y = 286. Feet-anchored, so legs and wings move while the
   bird stays planted.
5. Clamp every visible pixel to the chat canvas colour as a per-channel floor.
   The pet composites `SourceOver` at 0.85 opacity, so a pixel darker than
   `#0E1116` pulls the background down at *any* alpha — the "adds light, never
   darkens" rule can only hold if no such pixel exists. The generated feather
   outlines broke it on 0.07–1.8% of each frame's visible pixels, some fully
   black. Clamping costs nothing visible, because those pixels sit on a canvas
   that is already that dark.
6. Record the baseline offset in the frame table.

`PET_HEIGHT` (190) then scales the common canvas down at draw time, so every
pose occupies the same footprint and the corner anchor does not shift when the
pose changes.

Alignment is deterministic and runs offline at build time, never at draw time.
Horizontal mirroring for left-facing movement is a draw-time negative x scale,
which needs no extra art.

## 6. Behaviour

Three layers, all driven by `advance(dt)` from the existing tick.

**Playback.** `frame_index = floor(pose_elapsed * fps) % frame_count`.

**Blink.** Unchanged schedule: 4–7 s random gap (`PET_BLINK_GAP`), 0.18 s
duration, fires in any awake pose whose twin is not in `PET_BLINK_SKIP`.

**Idle variation.** After `PET_IDLE_VARIATION_AFTER` (120 s) without activity,
and only while the requested state is `idle`, the pet freezes what it is doing
(standing, pausing, or mid-walk once it is back on the ground) and plays the
`looking` cycle for `PET_IDLE_VARIATION_DURATION` (4–7 s), then puts the pose it
came from back. The next one is `PET_IDLE_VARIATION_GAP` (60–150 s) later.
Roaming, sleep, a blink or any activity cancels it. It is never played while
flying.

**Floating Zzz.** While the drawn pose is `sleeping`, paint draws three Z strokes
above the head. `pet_zzz_progress` gives each glyph its fraction through a
`PET_ZZZ_PERIOD` (2.8 s) rise; they are staggered a third of a period apart,
travel `PET_ZZZ_RISE` (46 px), fade in and out on `sin(pi p)`, and drift
sideways a little. They are strokes rather than text because a font lookup
renders as tofu when the family is missing, and the strokes cost nothing. No
asset change: `sleeping` stays one frame.

**Roaming.** Replaces the current `PET_WANDER_AFTER` / `PET_WANDER_DURATION`
pair. After `PET_ROAM_AFTER` (45 s) without activity the pet enters roaming: it
picks a target x within the viewport, walks toward it at `PET_ROAM_SPEED`
(40 px/s) facing its direction of travel, pauses 2–5 s, picks another target.
`PET_ROAM_FLY_CHANCE` (0.2) of the time it flies an arc instead, using
`flying` frames and a sine path. Roaming stops immediately on activity.

**Sleep.** `PET_SLEEP_AFTER` = 1800 s idle by day, `PET_SLEEP_AFTER_NIGHT` =
600 s between `PET_NIGHT_START` 22:00 and `PET_NIGHT_END` 07:00 local. Any
activity resets the clock and wakes the pet.

### Decision that changes existing behaviour

`sleeping` is currently triggered by the 60-second "Mic sleeping" continuous-
listening timeout. Under this design that is far too eager: the pet would sleep
after a minute of quiet while the 30-minute rule would never fire.

**Resolution:** the mic-sleeping timeout maps to `idle` (awake, waiting,
roaming). Only the activity-idle threshold produces the `sleeping` pose. This
revises the mapping approved in `a141865` and is the one behavioural change here
that removes a trigger rather than adding one.

### Activity definition

An activity timestamp is updated by:

- sending or receiving a message, a voice transcript, or a tool turn;
- any keypress or mouse click inside the application window. One shared
  application-level filter forwards these; Qt only delivers an event to filters
  installed on the object that received it, and a filter per window made the
  full test suite quadratic in the windows it builds (measured: 168 s against
  83 s once shared);
- the cursor entering the pet's proximity radius.

Scrolling without clicking does not count.

## 7. Cursor proximity and Z-order

`WA_TransparentForMouseEvents` means the pet receives no mouse events, so
proximity comes from polling `QCursor.pos()` inside `advance(dt)` and mapping it
into the pet's rect. No global event filter for this, and no new timer.

Within `PET_PROXIMITY_PX` (120) of the sprite rect the pet raises to the front of
the message body, unconditionally — the owner accepted that it can then sit over
a code block. After `PET_PROXIMITY_RELEASE_S` (1.5 s) outside the radius it drops
back behind the body.

`ChatView.resizeEvent` calls `self._body.raise_()`, which would silently demote
the pet. The pet therefore re-asserts its own order whenever its desired layer
changes and after any viewport resize.

## 8. Error handling

- Missing pose directory, missing frame, or unreadable PNG: fall back to
  `idle_1`, then to drawing nothing. Never raise.
- A pose whose frame list is empty renders the fallback rather than a blank
  cycle.
- The blink twin is optional per pose; absent twin means no blink for that pose,
  not a fallback swap.
- Frame discovery is cached per widget, so a missing file costs one `stat` for
  the life of the widget, matching the current sprite cache.

## 9. Testing

**Pure and injectable.** Frame selection, blink substitution, alignment maths,
roaming steps and the sleep schedule are functions that take `dt`, an injected
`rng`, and an injected clock — no wall time, no real timer. This follows the
pattern already in `PetBehaviourTests`.

**Permanent asset guard.** A test asserts that every file named by the frame
table exists and is genuinely RGBA. This is the class of defect that appeared
twice in this work — JPEG sources with no alpha channel, then painted
checkerboards — and it should fail loudly forever rather than be caught by eye.

**Offscreen Qt.** Retains the existing assertions: no pixel darker than the
canvas floor, `WA_TransparentForMouseEvents` still set, still exactly one
`QTimer` in the HUD, and the pet still adds no second timer.

**Layer order.** A test that raising on proximity and dropping after the release
delay survives a viewport resize.

## 10. Assistant sprite control

The assistant drives the pet through one tool, `sprite_control`, so "fly around"
or "get bigger" actually moves the bird instead of being described.

| Command | Effect |
|---|---|
| `fly_to(x, y)` | ease in/out to that top-left; duration from distance at 420 px/s, clamped to 0.35–1.8 s |
| `fly_around()` | four viewport waypoints (`PET_TOUR`), one eased leg each, then back to the anchor |
| `pop_out()` / `pop_in()` | scale up from `PET_POP_SCALE` while fading in over 0.45 s; the reverse over 0.35 s |
| `set_scale(factor)` | live resize, clamped to `PET_SCALE_RANGE`; reloads the frame cache |
| `set_position(anchor)` | one of the four corner presets; resets the offset |

**No new timer.** Every command only writes a target; `advance(dt)` on the HUD's
existing tick walks the ease and repaints. Nothing sleeps on the GUI thread.

**Threading.** Tools run on a worker and the pet is a QWidget, so the tool calls
`sprite_bus.sprite_controller.play(...)`; the GUI registers a `QObject` relay and
a `Signal(str, dict)` hops the call onto the GUI thread (AutoConnection: direct
on the GUI thread, queued from a worker). With no window open the tool answers
"the desktop pet is not on screen" rather than pretending.

**Position is one number.** Flight and roaming both write `_roam_x/_roam_y`, the
offset from the anchor, so a pet that lands somewhere stays there;
`set_position` is the only thing that zeroes it.

**Size.** `PET_SCALE` is the shipped default and `set_scale` is the live
override; it is not persisted.

## 11. Generation and approval workflow

**Amended during batch 1.** The original plan was one `ImageGen` call per frame.
Batch 1 proved that wrong twice: separately-called frames came back at four
near-identical standing poses (a walk cycle that does not walk) and at
inconsistent scale, because the model has no shared frame of reference between
calls. The method is now **one sheet per cycle** — all poses of a cycle in a
single image, then sliced. Same image means same scale, same style, and the model
can see the poses it is relating. This is also how the supplied reference sheets
were built.

1. Generate one sheet per pose cycle, image-to-image from the clean reference
   PNGs, poses left to right on a flat magenta field, feet on a common line.
2. Matte, clear residue, slice by column projection with seam splitting at
   opacity troughs, normalise by head width, align on the feet baseline.
3. Compose a **contact sheet per batch**, composited over `COLOR_BG` so it is
   judged against the real dark canvas, plus a zoomed head strip to prove the
   blink twin actually closed the eyes.
4. Show the contact sheet. **Nothing is committed until it is approved.**
5. Batch 1 is `idle` (3) + `walking` (4) = 7 of 22 frames.

Two facts about the generator that the pipeline must accommodate, both measured:

- It does **not** honour a requested hex background. It produced a different flat
  pink per image — `(245,8,137)` to `(253,3,217)` — so the key colour is sampled
  per image from the border ring rather than assumed.
- Its PNGs are `mode=RGBA` with a **nearly opaque alpha channel** (extrema
  101–239 up to 255). That is not a cut-out. Existing alpha is only trusted when
  it actually contains transparent pixels.

Known risk, still true: poses can drift between sheets. Head-width normalisation
measures the drift and the batch is rejected if it exceeds a few percent.

## 12. Out of scope

- P5: Kling AI video generation for true in-between motion. Needs an `ffmpeg`
  install and a paid API key.
- User-facing configuration for fps, thresholds or night hours. Constants first;
  a manifest or settings keys only if tuning is actually needed.
- Sound, particle trails, and any change to the reactor HUD's own layers.
