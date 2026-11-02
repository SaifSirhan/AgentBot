---
name: play_media
description: Play music, movies, or anime from a URL or search in mpv/VLC with no site ads.
---

# Playing media with stream_media

`stream_media` resolves anything yt-dlp supports into a direct stream and
plays it in a native window (mpv, then VLC). Use it instead of opening a site
in the browser when the user wants to actually watch or listen.

## When to use

- "play <song>", "put on <album track>" — music videos and audio.
- "watch <movie>", "play <show> episode <n>", "stream <anime>" — video.
- Any direct media link (`https://.../movie.m3u8`, `.mp4`).
- When the user complains about ads, popups, or wants a normal player window.

Use `play_on_youtube` instead when the user specifically wants the YouTube
Music web player left running in Opera. Use `stream_media` when the request is
to watch or listen in a real player.

## Calls

- `stream_media("one piece episode 1")` — searches and plays the best match.
- `stream_media("lofi hip hop radio")` — same for music.
- `stream_media("https://www.youtube.com/watch?v=...")` — exact video.
- `stream_media("https://site.example/path/master.m3u8")` — direct stream.

Nothing is downloaded; playback streams from the source.

## What happens

1. yt-dlp extracts the media: a URL is used as-is, anything else is resolved
   with `ytsearch1:`.
2. If the site offers a single URL that already has video and audio, that URL
   is handed to the player.
3. If it does not (YouTube now serves video and audio separately), mpv is
   given the page URL and its ytdl hook assembles both tracks.
4. With no mpv and no VLC, the stream is opened in the default browser. When
   the tracks are split, the page is opened instead, because a video-only URL
   plays silently.

## Reporting

- Quote the reply: "Playing in mpv: <title>" tells the user where it went.
- If the reply starts with ERROR, report it as a failure. Never claim
  something is playing when the tool returned an error.
- If the wrong video was picked, ask for the URL and call `stream_media` with
  it instead of searching again.

## Limits

- DRM services (Netflix, Disney+, Crunchyroll, Prime Video) cannot be played;
  yt-dlp has no key for them. Say so instead of retrying.
- Live streams work only when the site exposes a playable HLS stream.
- Search plays one result. For an exact episode or version, pass the URL.
- Pausing, skipping, and volume use `media_control` and `set_volume` once the
  player has focus.
