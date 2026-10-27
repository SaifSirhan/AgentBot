---
name: stremio
description: "Watch movies and TV shows by driving the Stremio app with OS Control: screenshot, search, click the poster, then the first Torrentio stream link."
---

# Stremio

## Overview

Stremio is the desktop media center on this machine. Torrentio is its usual
stream add-on. The agent has no API for Stremio, so playback is driven visually:
screenshot, click, type, screenshot again. Never click blind and never report
playback you did not see on screen.

## Procedure

1. **Check whether Stremio is running.** Use the `list_windows` tool; if any
   title contains "Stremio", it is open. Otherwise launch it with
   `open_app("Stremio")` and wait a few seconds for the home screen to load.
   Focus it before acting with `focus_window("Stremio")`, or pass the window
   title to the click: `"x,y | Stremio"`.

2. **Screenshot first.** Call `screenshot_background()` to hide AgentBot, then
   read the image. Coordinates are relative to the primary monitor and change
   with window size, so take a fresh screenshot before each click.

3. **Search.** Click the search bar at the top, then type the exact title and
   submit in one call: `type_text("Dune Part Two{enter}")`. A trailing
   `{enter}` presses Enter after typing.

4. **Open the result.** Screenshot again. Find the movie poster in the results
   grid and click it. On a series, open the show first and then the episode.

5. **Start the stream.** Screenshot again. The stream list appears below the
   poster; click the first streaming link — usually the Torrentio entry with
   the most seeds. If Stremio asks which player to use, accept the default.

6. **Verify.** Take one final screenshot. Only say it is playing when a player
   window or the video itself is visible. Otherwise report exactly what is on
   screen (buffering, "no streams", an error dialog) and try one alternative
   link.

## Rules

- One screenshot per interaction step; re-measure after any window change.
- Clicking the wrong subject (a sequel, a remake, a trailer) is the most common
  failure. Match the poster title and year to what the user asked for.
- Never claim a movie is playing without a screenshot that shows it.
- If Torrentio returns no streams, say so plainly and offer the next add-on in
  the list instead of improvising another site.
