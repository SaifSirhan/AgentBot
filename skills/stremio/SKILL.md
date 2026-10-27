---
name: stremio
description: "Watch movies and TV shows by driving the Stremio app with OS Control: screenshot, search, click the poster, then the first Torrentio stream link."
---

# Stremio

## Scope

`stream_media` covers trailers, songs, and specific YouTube URLs. Full movies
and TV episodes belong here, in Stremio.

If the user asks to watch a full movie or TV show, use the desktop route below
to open Stremio, search for the title, and click the first Torrentio stream.
Do not use stream_media or a browser stream for this.

## Overview

Stremio is the desktop media center on this machine. Torrentio is its usual
stream add-on. The agent has no API for Stremio, so playback is driven visually:
screenshot, click, type, screenshot again. `web.stremio.com` can navigate faster
through the DOM, but it cannot play the desktop app's Torrentio streams. Never
click blind and never report playback you did not see on screen.

## Choosing a route

- **Desktop route (playback):** OS Control with vision. This is the route that
  uses the Torrentio add-on installed in the desktop app and can hand the
  stream to mpv.
- **Browser route (navigation only, when usable):** `browse_web` on
  `https://web.stremio.com` reads the DOM and clicks by visible label (for
  example the episode "Cherry") without screenshot or vision calls. Prefer it
  for finding a title or episode only when the browser profile is signed in and
  the Addons page lists Torrentio. AgentBot starts a fresh Chromium profile per
  task, so the default is an anonymous guest with only the built-in add-ons
  (Cinemeta, YouTube, WatchHub, Public Domain Movies, OpenSubtitles, Local
  Files). An HTTPS page also cannot reach the local streaming server
  (`http://127.0.0.1:11470 ... ERR_FAILED`). If Torrentio is missing, the
  profile is anonymous, the Streaming URL shows an error, or `browse_web`
  reports the origin is not allowlisted, stop using the browser route and
  switch to the desktop route; do not retry it.
- **Never mix them:** use the browser route to find or navigate, but play
  through the desktop route. Do not claim a movie is playing from web metadata.

## Procedure

1. **Check whether Stremio is running.** Use the `list_windows` tool; if any
   title contains "Stremio", it is open. Otherwise launch it with
   `open_app("Stremio")` and wait a few seconds for the home screen to load.
   If it is minimized, focus it with `focus_window("Stremio")`, or pass the
   window title to the screenshot and click calls.

2. **Screenshot the app, not the desktop.** Call
   `screenshot_background("Stremio")`. It hides AgentBot, restores/focuses
   the Stremio window, captures the screen, and returns a vision description
   with pixel coordinates. Read the X/Y of the element you need from that
   description. Coordinates are relative to the primary monitor and change
   with window size, so take a fresh screenshot before each click.

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

- The browser route is not a playback route: a fresh profile is an anonymous
  guest without Torrentio, and the local streaming server is unreachable from
  an HTTPS page. Do not retry `browse_web` for streams; switch to OS Control.
- You have a vision model. Use `analyze_screenshot("Stremio|Where is the
  search bar?")` to read the screen. Never tell the user you only received a
  file path.
- One screenshot per interaction step; re-measure after any window change.
- Clicking the wrong subject (a sequel, a remake, a trailer) is the most common
  failure. Match the poster title and year to what the user asked for.
- Never claim a movie is playing without a screenshot that shows it.
- If Torrentio returns no streams, say so plainly and offer the next add-on in
  the list instead of improvising another site.
