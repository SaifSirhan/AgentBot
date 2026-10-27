---
name: stremio
description: "Watch movies and TV shows on the Desktop Stremio app with OS Control: search, click the exact poster, then the Torrentio 1080p link."
---

# Stremio

## Scope

`stream_media` covers trailers, songs, and specific YouTube URLs. Full movies
and TV episodes belong here, in Stremio.

## Checklist

1. **Focus Stremio. Take a screenshot.** Use `list_windows`; if no title
   contains "Stremio", launch it with `open_app("Stremio")`. Then call
   `screenshot_background("Stremio")` to focus the app and capture the screen.

2. **Click the search bar. Type the requested show/movie name. Press Enter.
   Wait 3 seconds.** Use `analyze_screenshot("Stremio|Where is the search
   bar?")` to get the X/Y, then `click_coordinates("x,y | Stremio")`, then
   `type_text("<title>{enter}")`. Wait with
   `execute_python("import time; time.sleep(3)")`.

3. **Take a screenshot. Click the poster that exactly matches the title. Wait
   3 seconds.** Use `analyze_screenshot("Stremio|Where is the poster whose
   title exactly matches <title>?")` to get the X/Y, then
   `click_coordinates("x,y | Stremio")`. Wait 3 seconds.

4. **Take a screenshot. Find the Torrentio link that says '1080p'. Click it.**
   Use `analyze_screenshot("Stremio|Where is the Torrentio link that says
   1080p?")` to get the X/Y, then `click_coordinates("x,y | Stremio")`.

- DO NOT click any other link.
- DO NOT ask for permission.
- STOP after clicking the 1080p link.
- If a screenshot description is truncated, do not stop; continue to the next
  `analyze_screenshot` call, which returns only coordinates.
- If there is no Torrentio 1080p link, say so plainly and stop. Do not click
  another stream.

## Setup

For instant playback, Stremio should be set to play in an external player
(mpv) in its settings. This is a one-time setting; do not stop the checklist
for it.
