"""Tool metadata for prompt_router: rule groups, keywords, descriptions."""
from __future__ import annotations

from functools import lru_cache

import prompt_router

EXCLUDED = {"chat", "done"}

FILE_TOOLS = frozenset({
    "read_file", "write_file", "patch_file", "make_folder",
    "find_and_open_folder", "find_files", "move_files", "list_symbols",
    "repo_map", "execute_python", "edit_document", "inspect_docx",
    "edit_docx_safe", "read_word_doc", "set_word_language", "scan_file",
    "quarantine_file", "list_quarantine", "watch_folder", "stop_watching",
    "deep_research",
})

OS_TOOLS = frozenset({
    "open_app", "open_website", "list_windows", "focus_window",
    "close_window", "save_screenshot", "screenshot_background",
    "analyze_screenshot", "click_coordinates", "type_text",
    "get_pixel_color", "save_macro", "play_macro", "set_volume",
    "media_control", "system_status", "lock_pc", "sleep_pc", "shutdown_pc",
    "restart_pc", "read_screen", "describe_screen", "clipboard_read",
    "clipboard_write", "delegate_to_qoder",
})

# The five tools the Stremio skill actually needs before it can see and click.
STREMIO_TOOLS = frozenset({
    "play_macro", "open_app", "screenshot_background",
    "analyze_screenshot", "click_coordinates",
})

# Fallback when a request matches no intent or keyword: desktop control plus
# file/Python access, so the turn is never left without a way to act.
FALLBACK_TOOLS = (
    "open_app", "type_text", "execute_python", "read_file", "write_file",
)

DESCRIPTIONS = {
    "search_web": "Search the live web and return titles, URLs, and summaries.",
    "serp_search": "Structured Google/News/Images/Scholar search via SerpApi.",
    "read_and_summarize": "Fetch a URL and summarize its contents.",
    "google_search_and_open": "Google search and open the top result.",
    "youtube_search": "Search YouTube and open the top result.",
    "get_video_links": "Return YouTube result links without opening them.",
    "play_on_youtube": "Play a song on YouTube Music.",
    "queue_on_ytm": "Queue a song on YouTube Music through the browser UI.",
    "close_music_tab": "Close the YouTube Music tab in Opera via CDP.",
    "stream_media": "Play YouTube/music/direct video URLs in mpv or VLC.",
    "weather": "Current weather for a place.",
    "air_quality": "Current air quality index for a place.",
    "news": "Current headlines by topic.",
    "current_time": "Real current date and time.",
    "rag_search": "Search the user's indexed documents by keywords.",
    "read_file": "Read a text file at an exact full path.",
    "write_file": "Create or fully overwrite a file.",
    "patch_file": "Surgical search/replace edit in an existing file.",
    "make_folder": "Create a folder.",
    "find_files": "Find files by name pattern.",
    "move_files": "Preview and then move matching files.",
    "edit_document": "Edit DOCX, XLSX, PPTX, or PDF via plain-language instruction.",
    "inspect_docx": "Map a DOCX's paragraphs, tables, and cells before editing.",
    "edit_docx_safe": "Locked, atomic Word edit; use for every .docx change.",
    "read_word_doc": "Read all text from a Word file via COM.",
    "execute_python": "Run a Python script in a fresh process.",
    "open_app": "Launch a desktop app or known web app.",
    "open_website": "Open a URL, or search a name and open it.",
    "list_windows": "List open windows.",
    "focus_window": "Focus a window by partial title.",
    "close_window": "Close a window by partial title.",
    "screenshot_background": "Hide the GUI, focus an app, and capture it.",
    "analyze_screenshot": "Capture the screen and answer a vision question.",
    "click_coordinates": "Click x,y in a fresh screenshot.",
    "type_text": "Type into the focused window.",
    "delegate_to_qoder": "Send a complex website/app/localhost-server build to Qoder IDE via clipboard and Ctrl+L chat, then let the turn inspect the terminal. Input: prompt and goal.",
    "save_macro": "Save a reusable click/type/hotkey macro.",
    "play_macro": "Run a saved macro by name.",
    "set_volume": "Set, mute, or unmute system volume.",
    "media_control": "Play/pause, next, previous, or stop media.",
    "check_battery_health": "Battery health percentage, design and full-charge capacities via powercfg; no input or file path needed.",
    "watch_system_condition": "Notify once when battery charge, CPU or RAM reaches a threshold in the background. Input: condition|target_percent|message.",
    "lock_pc": "Lock Windows.",
    "sleep_pc": "Put Windows to sleep.",
    "shutdown_pc": "Shut down Windows (requires confirmation).",
    "restart_pc": "Restart Windows (requires confirmation).",
    "send_telegram_message": "Notify the user through their own Telegram bot.",
    "telegram_user_send": "Send a Telegram message as the user.",
    "telegram_user_delete": "Preview or delete the user's Telegram messages.",
    "telegram_user_edit": "Edit a recent Telegram message as the user.",
    "send_image_telegram": "Send an image on Telegram as the user.",
    "send_gif": "Send a GIF to a named contact as the user.",
    "send_maps_url": "Build and send one Google Maps link.",
    "send_maps_list": "Build and send several Google Maps links.",
    "gmail_unread": "List unread email.",
    "gmail_read": "Read email by UID.",
    "gmail_search": "Search email by keyword.",
    "set_reminder": "Set a one-shot reminder from a duration.",
    "set_reminder_at": "Set a one-shot reminder at HH:MM.",
    "schedule_task": "Create or manage a recurring scheduled job.",
    "list_reminders": "List pending reminders and scheduled jobs.",
    "remember": "Save a lasting personal fact.",
    "forget": "Remove a lasting personal fact.",
    "generate_image": "Generate an AI image from a prompt.",
    "describe_image": "Describe an image with the vision model.",
    "scan_file": "Hash and scan a file for malicious indicators.",
    "scan_process": "Inspect a running process.",
    "quarantine_file": "Move a suspicious file to quarantine.",
    "browse_web": "Run a restricted, allowlisted browser task.",
    "load_skill": "Read the full manual for a skill or tool input format.",
    "skill_manage": "Create, update, list, read, or delete a reusable skill.",
}

EXTRA_KEYWORDS = {
    "read_file": ("file", "read", "open", "edit"),
    "write_file": ("file", "write", "save", "create", "edit"),
    "patch_file": ("file", "patch", "edit", "change"),
    "edit_docx_safe": ("file", "word", "docx", "document", "edit"),
    "edit_document": ("file", "document", "edit", "docx", "xlsx", "pptx", "pdf"),
    "inspect_docx": ("docx", "word", "document"),
    "read_word_doc": ("word", "docx", "document", "read"),
    "find_files": ("find", "search"),
    "move_files": ("move", "rename"),
    "make_folder": ("folder", "create", "directory"),
    "execute_python": ("python", "script", "run", "code", "docker",
                       "terminal", "server"),
    "open_app": ("open", "app", "launch", "application", "docker",
                 "terminal", "server", "code"),
    "open_website": ("open", "website", "site", "url", "browser"),
    "screenshot_background": ("screenshot", "screen", "capture", "desktop"),
    "analyze_screenshot": ("screenshot", "screen", "look", "vision", "desktop"),
    "click_coordinates": ("click", "desktop", "screen"),
    "type_text": ("type", "write", "keyboard", "desktop", "terminal"),
    "delegate_to_qoder": ("qoder", "delegate", "build", "website", "web",
                          "app", "localhost", "server", "complex", "manager"),
    "play_macro": ("macro", "play", "movie", "film", "episode", "stremio"),
    "save_macro": ("macro", "save", "record"),
    "search_web": ("search", "web", "google", "look", "up"),
    "rag_search": ("search", "documents", "notes", "files", "rag"),
    "weather": ("weather", "forecast", "temperature"),
    "air_quality": ("aqi", "air", "quality", "pollution"),
    "news": ("news", "headlines", "today"),
    "current_time": ("time", "date", "today", "now"),
    "send_telegram_message": ("telegram", "message", "send", "notify", "text"),
    "telegram_user_send": ("telegram", "send", "message", "user", "text"),
    "telegram_user_delete": ("telegram", "delete", "remove", "message"),
    "telegram_user_edit": ("telegram", "edit", "message"),
    "set_reminder": ("reminder", "remind", "timer", "alarm"),
    "set_reminder_at": ("reminder", "remind", "alarm", "time"),
    "schedule_task": ("schedule", "recurring", "daily", "weekly", "cron"),
    "remember": ("remember", "save", "memory", "fact"),
    "forget": ("forget", "delete", "memory", "fact"),
    "generate_image": ("image", "picture", "generate", "draw", "create"),
    "describe_image": ("image", "picture", "describe", "look"),
    "stream_media": ("youtube", "music", "video", "stream", "play"),
    "play_on_youtube": ("youtube", "music", "song", "play"),
    "queue_on_ytm": ("queue", "add", "music", "youtube", "song"),
    "close_music_tab": ("close", "music", "youtube", "tab", "opera"),
    "media_control": ("play", "pause", "skip", "next", "previous", "prev",
                      "stop", "music", "media", "track", "song"),
    "load_skill": ("skill", "manual", "instructions", "format", "tool"),
    "watch_system_condition": ("battery", "cpu", "ram", "notify", "notified", "alert", "monitor", "watch"),
    "run_command": ("run", "command", "terminal", "shell", "docker",
                    "server", "code"),
}


@lru_cache(maxsize=1)
def build_tools(valid_tools: frozenset[str]) -> tuple[prompt_router.Tool, ...]:
    tools = []
    for name in sorted(valid_tools - EXCLUDED):
        groups = []
        if name in STREMIO_TOOLS:
            groups = ["stremio", "os"]
        elif name in FILE_TOOLS:
            groups = ["python"]
        elif name in OS_TOOLS:
            groups = ["os"]
        tokens = {part for part in name.split("_") if part and part != "safe"}
        tokens.update(EXTRA_KEYWORDS.get(name, ()))
        tools.append(prompt_router.Tool(
            name=name,
            description=DESCRIPTIONS.get(name, name.replace("_", " ")),
            rule_groups=tuple(groups),
            keywords=tuple(sorted(tokens)),
        ))
    return tuple(tools)
