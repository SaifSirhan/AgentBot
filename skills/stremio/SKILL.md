---
name: stremio
description: "Watch movies and TV shows by driving the Desktop Stremio app with OS Control: screenshot, search, click the poster, then the first Torrentio stream link."
---

# Stremio

## Scope

`stream_media` covers trailers, songs, and specific YouTube URLs. Full movies
and TV episodes belong here, in Stremio.

If the user asks to watch a movie or TV show, you MUST use OS Control on the
Desktop Stremio app. Focus the Stremio window, take a screenshot, click the
search bar, type the title, click the poster, and click the first stream.
Do not use stream_media for this.

## Overview

Stremio is the desktop media center on this machine. Torrentio is its usual
stream add-on. The agent has no API for Stremio, so playback is driven visually:
screenshot, click, type, screenshot again. Never click blind and never report
playback you did not see on screen.

## Procedure

1. **Focus Stremio.** Use `list_windows`; if no title contains "Stremio",
   launch it with `open_app("Stremio")` and wait a few seconds. If it is
   minimized, restore it with `focus_window("Stremio")`.

2. **Screenshot the app, not the desktop.** Call
   `screenshot_background("Stremio")`. It hides AgentBot, restores/focuses
   the Stremio window, captures the screen, and returns a vision description
   with pixel coordinates. Read the X/Y of the element you need from that
   description. Coordinates change with window size, so take a fresh
   screenshot before each click.

3. **Search.** To click the search bar, you must first take a screenshot, use
   your vision to locate the text "Search", output the X/Y coordinates, and
   use click_coordinates to click it. Do not ask the user for coordinates.
   Then type the exact title and submit in one call:
   `type_text("Dune Part Two{enter}")`. A trailing `{enter}` presses Enter
   after typing.

4. **Open the result.** Screenshot again with `screenshot_background("Stremio")`.
   Find the movie poster in the results grid, read its X/Y, and click it. On a
   series, open the show first and then the episode.

5. **Start the stream.** Screenshot again. Before clicking a stream, remind
   the user to ensure Stremio is set to play in an external player (mpv) in its
   settings so the video plays instantly without buffering in the Stremio
   window. Say this once per session, then continue without waiting. The
   stream list appears below the poster; click the first streaming link —
   usually the Torrentio entry with the most seeds. If Stremio asks which
   player to use, accept the default.

6. **Verify.** Take one final screenshot. Only say it is playing when a player
   window or the video itself is visible. Otherwise report exactly what is on
   screen (buffering, "no streams", an error dialog) and try one alternative
   link.

## Rules

- Use the Desktop Stremio app only.
- You have a vision model. Use `analyze_screenshot("Stremio|Where is the
  search bar?")` to read the screen. Never tell the user you only received a
  file path.
- One screenshot per interaction step; re-measure after any window change.
- Clicking the wrong subject (a sequel, a remake, a trailer) is the most common
  failure. Match the poster title and year to what the user asked for.
- Never claim a movie is playing without a screenshot that shows it.
- If Torrentio returns no streams, say so plainly and offer the next add-on in
  the list instead of improvising another site.
