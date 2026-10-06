import subprocess
import os
import glob
import json
import re
import requests
import webbrowser
import time
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------
# CONFIGURATION
# ---------------------------
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b"
MAX_STEPS_PER_TURN = 6
OLLAMA_TIMEOUT = 120
MAX_PAGE_CHARS = 6000

# ---------------------------
# GLOBAL MEMORY
# ---------------------------
last_search_query = ""
last_search_result = ""

KNOWN_WEB_APPS = {
    "youtube": "https://youtube.com",
    "canva": "https://canva.com",
    "notion": "https://notion.so",
    "figma": "https://figma.com",
    "spotify": "https://open.spotify.com",
    "gmail": "https://mail.google.com",
    "google docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "discord": "https://discord.com/app",
    "netflix": "https://netflix.com",
    "chatgpt": "https://chat.openai.com",
    "reddit": "https://reddit.com",
    "twitter": "https://twitter.com",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
}

# ---------------------------
# HELPERS
# ---------------------------
def get_downloads_path():
    return os.path.join(os.environ['USERPROFILE'], 'Downloads')

def get_home_path():
    return os.environ['USERPROFILE']

def normalize_url(url):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url

# ---------------------------
# RAW LLM CALL
# ---------------------------
def ask_llm_direct(prompt, max_tokens=None):
    try:
        payload = {"model": MODEL, "prompt": prompt, "stream": False}
        if max_tokens:
            payload["options"] = {"num_predict": max_tokens}
        response = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        if response.status_code == 200:
            return response.json().get('response', '').strip()
        return f"Error: HTTP {response.status_code}"
    except Exception as e:
        return f"Error: {e}"

# ---------------------------
# BASIC FILE TOOLS
# ---------------------------
def run_command(command):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        return result.stdout if result.stdout else result.stderr
    except Exception as e:
        return f"ERROR: {e}"

def make_folder(folder_name):
    try:
        folder_path = os.path.join(get_downloads_path(), folder_name) if not os.path.isabs(folder_name) else folder_name
        os.makedirs(folder_path, exist_ok=True)
        return f"Folder created: {folder_path}"
    except Exception as e:
        return f"ERROR: {e}"

def write_file(filename, content, folder=None):
    try:
        if folder:
            folder_path = os.path.join(get_downloads_path(), folder) if not os.path.isabs(folder) else folder
            file_path = os.path.join(folder_path, filename)
        else:
            file_path = os.path.join(get_downloads_path(), filename) if not os.path.isabs(filename) else filename
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"File written: {file_path}"
    except Exception as e:
        return f"ERROR: {e}"

def read_file(filename):
    try:
        file_path = os.path.join(get_downloads_path(), filename) if not os.path.isabs(filename) else filename
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"ERROR: {e}"

# ---------------------------
# NEW FILE TOOLS  ← THIS IS THE FIX
# ---------------------------
def list_folder(folder_path):
    """List contents of a folder (files + subfolders)."""
    try:
        if not os.path.isabs(folder_path):
            folder_path = os.path.join(get_downloads_path(), folder_path)
        if not os.path.exists(folder_path):
            return f"ERROR: Folder not found: {folder_path}"
        if not os.path.isdir(folder_path):
            return f"ERROR: Not a folder: {folder_path}"

        items = os.listdir(folder_path)
        folders = sorted([i for i in items if os.path.isdir(os.path.join(folder_path, i))])
        files = sorted([i for i in items if os.path.isfile(os.path.join(folder_path, i))])

        out = f"Contents of {folder_path}:"
        if folders:
            out += f"\n  Folders ({len(folders)}): " + ", ".join(folders[:30])
        if files:
            out += f"\n  Files ({len(files)}): " + ", ".join(files[:30])
        if not items:
            out += "\n  (empty folder)"
        return out
    except Exception as e:
        return f"ERROR: {e}"

def search_files(query, search_path=None):
    """Search for files/folders by name under Downloads (or a given path)."""
    try:
        if search_path is None:
            search_path = get_downloads_path()
        elif not os.path.isabs(search_path):
            search_path = os.path.join(get_downloads_path(), search_path)

        if not os.path.exists(search_path):
            return f"ERROR: Search path not found: {search_path}"

        query_lower = query.lower()
        matches = []

        for root, dirs, files in os.walk(search_path):
            # Skip hidden and cache folders
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            for name in dirs + files:
                if query_lower in name.lower():
                    matches.append(os.path.join(root, name))
            if len(matches) > 50:
                break

        if not matches:
            return f"No files/folders matching '{query}' under {search_path}"

        out = f"Found {len(matches)} matches:\n" + "\n".join(matches[:20])
        if len(matches) > 20:
            out += f"\n... and {len(matches) - 20} more"
        return out
    except Exception as e:
        return f"ERROR: {e}"

def open_file(file_path):
    """Open a file with the default application."""
    try:
        if not os.path.isabs(file_path):
            file_path = os.path.join(get_downloads_path(), file_path)
        if not os.path.exists(file_path):
            return f"ERROR: File not found: {file_path}"
        os.startfile(file_path)
        return f"Opened file: {file_path}"
    except Exception as e:
        return f"ERROR: {e}"

def open_folder(folder_path):
    """Open a folder in Windows Explorer."""
    try:
        if not os.path.isabs(folder_path):
            folder_path = os.path.join(get_downloads_path(), folder_path)
        if not os.path.exists(folder_path):
            return f"ERROR: Folder not found: {folder_path}"
        subprocess.Popen(f'explorer "{folder_path}"', shell=True)
        return f"Opened folder in Explorer: {folder_path}"
    except Exception as e:
        return f"ERROR: {e}"

# ---------------------------
# SEARCH TOOLS
# ---------------------------
def search_web(query):
    global last_search_query, last_search_result
    try:
        last_search_query = query
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for r in soup.select(".result__a")[:5]:
            title = r.get_text(strip=True)
            href = r.get("href", "")
            if title and href:
                results.append(f"{title} -> {href}")

        if not results:
            result = f"No results found for '{query}'."
        else:
            result = "Real search results (use these EXACT URLs):\n" + "\n".join(results)

        last_search_result = result
        return result
    except Exception as e:
        return f"ERROR: Search error: {e}"

def show_last_result():
    global last_search_result
    if last_search_result:
        return last_search_result
    return "No previous search result found. Please search first."

def open_search_in_browser(query):
    return open_url_in_browser(f"https://duckduckgo.com/?q={query}", "chrome")

# ---------------------------
# WEBPAGE SUMMARIZER
# ---------------------------
def read_and_summarize(url):
    global last_search_result
    try:
        url = normalize_url(url)
        print(f"Fetching: {url}")

        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=15
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "form", "noscript", "iframe", "svg"]):
            tag.decompose()

        main = soup.find("article") or soup.find("main") or soup.body or soup
        text = main.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        text = "\n".join(lines)

        if len(text) < 200:
            return f"ERROR: Could not extract text from {url} (page may be JS-only)."

        text = text[:MAX_PAGE_CHARS]

        prompt = f"""Summarize the following webpage in 4-6 bullet points. Then add a one-line TL;DR.

Be factual. Do not invent information.

--- PAGE from {url} ---
{text}
--- END ---

Summary:"""

        print(f"Summarizing via {MODEL}...")
        summary = ask_llm_direct(prompt)
        output = f"Summary of {url}:\n{summary}"
        last_search_result = output
        return output

    except requests.exceptions.HTTPError as e:
        return f"ERROR: HTTP {e.response.status_code}"
    except requests.exceptions.Timeout:
        return f"ERROR: Page timed out."
    except Exception as e:
        return f"ERROR: {e}"

# ---------------------------
# APP / WEBSITE TOOLS
# ---------------------------
def open_website(url):
    try:
        url = normalize_url(url)
        driver = get_driver()
        if driver is None:
            webbrowser.open(url)
            return f"Opened: {url} (default browser)"
        driver.get(url)
        return f"Opened: {url} (Opera)"
    except Exception as e:
        return f"ERROR: {e}"

def open_app(app_name):
    key = app_name.lower().strip()
    if key in KNOWN_WEB_APPS:
        return open_website(KNOWN_WEB_APPS[key])
    try:
        if 'bluetooth' in key:
            subprocess.Popen('start ms-settings:bluetooth', shell=True)
            return "Opened Bluetooth settings."
        elif 'wifi' in key or 'wi-fi' in key:
            subprocess.Popen('start ms-settings:network-wifi', shell=True)
            return "Opened Wi-Fi settings."
        elif 'setting' in key:
            subprocess.Popen('start ms-settings:', shell=True)
            return "Opened Settings."

        app_paths = {
            'telegram': r'"C:\Users\USER\AppData\Roaming\Telegram Desktop\Telegram.exe"',
            'chrome': r'"C:\Program Files\Google\Chrome\Application\chrome.exe"',
            'notepad': 'notepad.exe',
            'calculator': 'calc.exe',
            'explorer': 'explorer.exe',
            'cmd': 'cmd.exe',
            'powershell': 'powershell.exe',
        }
        for k, path in app_paths.items():
            if k in key:
                subprocess.Popen(path, shell=True)
                return f"Opened {app_name}."

        check = subprocess.run(f'where {app_name}', shell=True, capture_output=True, text=True)
        if check.returncode == 0 and check.stdout.strip():
            subprocess.Popen(f'start "" "{app_name}"', shell=True)
            return f"Opened {app_name}."

        return f"ERROR: '{app_name}' is not installed. Try open_website or search_files."
    except Exception as e:
        return f"ERROR opening {app_name}: {e}"

# ---------------------------
# OPERA + SELENIUM
# ---------------------------
def find_opera_path():
    candidates = [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Opera GX', 'opera.exe'),
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'Opera GX', 'opera.exe'),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'Opera GX', 'opera.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Opera', 'opera.exe'),
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'Opera', 'opera.exe'),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    for folder in ('Opera GX', 'Opera'):
        pattern = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', folder, '**', 'opera.exe')
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    return None

OPERA_PATH = find_opera_path()
_driver = None

def get_driver():
    global _driver
    if _driver is not None:
        try:
            _ = _driver.title
            return _driver
        except Exception:
            _driver = None
    _driver = setup_chrome_driver()
    return _driver

OPERA_CHROMIUM_VERSION = "151.0.7922.170"

def get_chromedriver_service():
    for version in [OPERA_CHROMIUM_VERSION, OPERA_CHROMIUM_VERSION.split('.')[0], None]:
        try:
            path = ChromeDriverManager(driver_version=version).install() if version \
                else ChromeDriverManager().install()
            if version:
                print(f"chromedriver matched: {version}")
            return Service(path)
        except Exception:
            continue
    raise RuntimeError("Could not obtain chromedriver")

def setup_chrome_driver():
    try:
        options = Options()
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--start-maximized")
        if OPERA_PATH:
            options.binary_location = OPERA_PATH
            print(f"Using Opera: {OPERA_PATH}")
        service = get_chromedriver_service()
        return webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"Driver error: {e}")
        return None

def google_search_and_open(query):
    try:
        driver = get_driver()
        if driver is None:
            return "ERROR: Could not start browser."
        driver.get("https://www.google.com")
        time.sleep(2)
        search_box = driver.find_element(By.NAME, "q")
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)
        time.sleep(3)
        try:
            driver.find_element(By.CSS_SELECTOR, "h3").click()
            time.sleep(2)
            return f"Opened Google search for '{query}' and clicked first link."
        except Exception:
            return f"Opened Google search for '{query}' but could not click."
    except Exception as e:
        return f"ERROR: {e}"

def youtube_search(query):
    try:
        driver = get_driver()
        if driver is None:
            return "ERROR: Could not start browser."
        driver.get("https://www.youtube.com")
        time.sleep(2)
        search_box = driver.find_element(By.NAME, "search_query")
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)
        time.sleep(3)
        videos = driver.find_elements(By.CSS_SELECTOR, "ytd-video-renderer")
        if videos:
            title = videos[0].find_element(By.CSS_SELECTOR, "#video-title").get_attribute("title")
            videos[0].find_element(By.CSS_SELECTOR, "#video-title").click()
            time.sleep(2)
            return f"Opened YouTube, searched '{query}', clicked: '{title}'."
        return f"No videos found for '{query}'."
    except Exception as e:
        return f"ERROR: {e}"

def get_video_links(query):
    try:
        driver = get_driver()
        if driver is None:
            return "ERROR: Could not start browser."
        driver.get("https://www.youtube.com")
        time.sleep(2)
        search_box = driver.find_element(By.NAME, "search_query")
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)
        time.sleep(3)
        videos = driver.find_elements(By.CSS_SELECTOR, "ytd-video-renderer")
        results = []
        for video in videos[:5]:
            try:
                title = video.find_element(By.CSS_SELECTOR, "#video-title").get_attribute("title")
                link = video.find_element(By.CSS_SELECTOR, "#video-title").get_attribute("href")
                results.append(f"{title} - {link}")
            except Exception:
                pass
        return "Videos for " + query + ":\n" + "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"

def open_url_in_browser(url, browser="chrome"):
    url = normalize_url(url)
    if browser.lower() == "chrome":
        try:
            subprocess.Popen(f'start chrome "{url}"', shell=True)
            return f"Opened {url} in Chrome."
        except Exception as e:
            return f"ERROR: {e}"
    else:
        webbrowser.open(url)
        return f"Opened {url} in default browser."

# ---------------------------
# TOOL DESCRIPTIONS
# ---------------------------
TOOL_DESCRIPTIONS = """
You are a highly intelligent AI assistant named Agent. You have extensive knowledge and can answer most questions directly.

## CRITICAL RULES

1. ANSWER FIRST, SEARCH LAST — Use "chat" if you know the answer. Use "search_web" only for current info.

2. BE CONCISE.

3. TAKE INITIATIVE — For vague requests, use "chat" to ask.

4. RETRY ON FAILURE — If the last result starts with "ERROR:", do NOT repeat the same call. Try a DIFFERENT tool.

5. STOP WHEN DONE — Respond with "done" when the task is complete.

6. NEVER INVENT A URL — Only use URLs from search_web results or the user.

7. FINISH THE WHOLE REQUEST — Do all actions. Chat goes LAST.

8. CHAT ENDS THE TURN — Use it last.

9. *** FINDING FILES / FOLDERS ON DISK ***
   When the user mentions a file or folder by NAME (e.g. "LI Documents", "my report"):
   - DO NOT use run_command with `cd` or `dir` — that fails with spaces and is confusing.
   - DO NOT guess an app name (open_app) or a website (open_website).
   - USE "search_files" with the name as input. It searches under Downloads and returns full paths.
   - THEN use "open_folder" or "open_file" with the exact path returned.
   
   Example flow:
      User: "open the LI Documents folder"
      Step 1 -> {"tool":"search_files","input":"LI Documents"}
      Step 2 (real path returned) -> {"tool":"open_folder","input":"C:\\\\Users\\\\USER\\\\Downloads\\\\LI Documents"}
      Step 3 -> {"tool":"done","input":"Opened LI Documents folder."}

10. PATH SPACES — Paths with spaces like "LI Documents" must be kept as ONE string. Never split on the space. The tools handle spaces correctly — just pass the full name.

11. SUMMARIZE FLOW — "summarize" / "read and summarize":
    - If user gave URL → "read_and_summarize" with that URL.
    - If only a topic → "search_web" first, then "read_and_summarize" on one of the returned URLs.
    - Do NOT use "open_website" for summarizing.

## Available Tools

| Tool | Purpose | Example Input |
|------|---------|---------------|
| chat | Answer directly | "AI stands for..." |
| search_web | Search online | "AI news 2025" |
| show_last_result | Show last search | "" |
| read_and_summarize | Fetch + summarize a URL | "https://example.com/article" |
| search_files | Find files/folders by name on disk | "LI Documents" |
| list_folder | List contents of a folder | "C:\\\\Users\\\\USER\\\\Downloads" |
| open_file | Open a file in its default app | "C:\\\\path\\\\doc.docx" |
| open_folder | Open a folder in Explorer | "C:\\\\path\\\\folder" |
| open_search_in_browser | Open search in browser | "meaning of AI" |
| google_search_and_open | Google search + click first | "haze Malaysia news" |
| youtube_search | YouTube search + click | "cute cats" |
| get_video_links | Get YouTube links | "cooking recipes" |
| open_app | Open app or known web app | "chrome", "canva" |
| open_website | Open a URL in browser | "youtube.com" |
| open_url_in_browser | Open in specific browser | "https://x.com|chrome" |
| run_command | Run system command | "dir" |
| make_folder | Create a folder | "test_folder" |
| write_file | Write a file | "note.txt|Hello" |
| read_file | Read a file | "note.txt" |
| done | Finish task | "Done" |

## Examples

- "open the LI Documents folder":
   step 1 -> {"tool":"search_files","input":"LI Documents"}
   step 2 -> {"tool":"open_folder","input":"C:\\\\Users\\\\USER\\\\Downloads\\\\LI Documents"}
   step 3 -> {"tool":"done","input":"Opened LI Documents folder."}

- "list what's in my Downloads folder":
   {"tool":"list_folder","input":"C:\\\\Users\\\\USER\\\\Downloads"}

- "open my resume":
   step 1 -> {"tool":"search_files","input":"resume"}
   step 2 (real path returned) -> {"tool":"open_file","input":"C:\\\\Users\\\\USER\\\\Downloads\\\\resume.pdf"}

- "find and summarize a news article about AI":
   step 1 -> {"tool":"search_web","input":"AI news 2025"}
   step 2 -> {"tool":"read_and_summarize","input":"https://techcrunch.com/category/artificial-intelligence/"}
   step 3 -> {"tool":"done","input":"Summarized an AI article."}

- "open canva" -> {"tool":"open_app","input":"canva"}

Your response MUST be a single JSON object with exactly two keys: "tool" and "input". Nothing else.
"""

VALID_TOOLS = {
    "chat", "search_web", "show_last_result", "open_search_in_browser",
    "read_and_summarize", "search_files", "list_folder", "open_file", "open_folder",
    "youtube_search", "get_video_links", "open_app", "open_website",
    "run_command", "make_folder", "write_file", "read_file",
    "google_search_and_open", "open_url_in_browser",
    "done",
}

# ---------------------------
# AI PLANNING
# ---------------------------
def ask_ai_for_plan(user_request, context, correction=None):
    correction_block = f"\n\nYour previous response was invalid: {correction}\nRespond with ONLY a corrected JSON object." if correction else ""
    prompt = f"""
{TOOL_DESCRIPTIONS}

Context (recent conversation + tool results, most recent last):
{context}

User: {user_request}{correction_block}

Your response (a single JSON object, nothing else):
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False, "format": "json"},
            timeout=OLLAMA_TIMEOUT
        )
        if response.status_code == 200:
            return response.json()['response']
        return f"Error: {response.status_code}"
    except Exception as e:
        return f"Error: {e}"

def parse_action(plan_text):
    try:
        parsed = json.loads(plan_text.strip())
    except json.JSONDecodeError as e:
        return None, f"not valid JSON ({e})"

    if isinstance(parsed, list):
        if not parsed:
            return None, "empty array"
        parsed = parsed[0]

    if not isinstance(parsed, dict):
        return None, "not a JSON object"

    if 'action' in parsed and 'tool' not in parsed:
        parsed['tool'] = parsed.pop('action')

    tool = parsed.get('tool')
    if tool not in VALID_TOOLS:
        return None, f"'{tool}' is not valid. Valid: {', '.join(sorted(VALID_TOOLS))}"

    parsed.setdefault('input', '')
    return parsed, None

def get_valid_action(user_request, context, max_retries=2):
    correction = None
    for attempt in range(max_retries + 1):
        plan_text = ask_ai_for_plan(user_request, context, correction=correction)
        print(f"\nRaw model output (attempt {attempt + 1}):", plan_text[:300])
        action, error = parse_action(plan_text)
        if action is not None:
            return action, None
        print(f"Invalid response: {error}")
        correction = error
    return None, f"Model failed after {max_retries + 1} attempts."

# ---------------------------
# TOOL EXECUTION
# ---------------------------
def execute_tool(action):
    tool = action.get('tool')
    inp = action.get('input', '')
    folder = action.get('folder', None)

    if tool == 'chat':
        return f"AI: {inp}"
    elif tool == 'google_search_and_open':
        return google_search_and_open(inp)
    elif tool == 'read_and_summarize':
        return read_and_summarize(inp)
    elif tool == 'search_files':
        return search_files(inp)
    elif tool == 'list_folder':
        return list_folder(inp)
    elif tool == 'open_file':
        return open_file(inp)
    elif tool == 'open_folder':
        return open_folder(inp)
    elif tool == 'make_folder':
        return make_folder(inp)
    elif tool == 'write_file':
        if '|' in inp:
            filename, content = inp.split('|', 1)
        else:
            filename, content = inp, ""
        return write_file(filename, content, folder)
    elif tool == 'read_file':
        return read_file(inp)
    elif tool == 'run_command':
        return run_command(inp)
    elif tool == 'search_web':
        return search_web(inp)
    elif tool == 'show_last_result':
        return show_last_result()
    elif tool == 'open_search_in_browser':
        return open_search_in_browser(inp)
    elif tool == 'open_website':
        return open_website(inp)
    elif tool == 'open_app':
        return open_app(inp)
    elif tool == 'youtube_search':
        return youtube_search(inp)
    elif tool == 'get_video_links':
        return get_video_links(inp)
    elif tool == 'open_url_in_browser':
        if '|' in inp:
            url, browser = inp.split('|', 1)
        else:
            url, browser = inp, "chrome"
        return open_url_in_browser(url, browser)
    else:
        return f"ERROR: Unknown tool '{tool}'"

# ---------------------------
# MAIN LOOP
# ---------------------------
def run_agent_turn(user_input, conversation_history):
    step_log = []
    last_signature = None

    for step in range(MAX_STEPS_PER_TURN):
        context = "\n".join(conversation_history[-10:] + step_log)

        if step == 0:
            next_input = user_input
        else:
            next_input = (
                f"Original request: '{user_input}'. Progress is above. "
                f'If everything is done, respond with {{"tool":"done","input":"<summary>"}}. '
                f"Otherwise do the next action."
            )

        action, error = get_valid_action(next_input, context)
        if action is None:
            print(f"\nGiving up: {error}")
            step_log.append(f"System: {error}")
            break

        tool = action.get('tool')
        inp = action.get('input', '')

        if tool == 'done':
            print(f"\nDone: {inp}")
            break

        signature = (tool, inp)
        if signature == last_signature:
            print("\nRepeated action detected - stopping.")
            step_log.append("System: Repeated action - stopped.")
            break
        last_signature = signature

        output = execute_tool(action)
        print(f"\n{output}")
        step_log.append(f"Action: {tool}({inp}) -> Result: {output}")

        if tool == 'chat':
            break

    conversation_history.extend(step_log)
    return step_log

def run_agent():
    print("Agent ready.")
    conversation_history = []

    while True:
        try:
            user_input = input("\nYou: ")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            break

        if user_input.lower() in ['quit', 'exit']:
            break

        conversation_history.append(f"User: {user_input}")
        run_agent_turn(user_input, conversation_history)
        print("\n" + "-" * 50)

if __name__ == "__main__":
    run_agent()