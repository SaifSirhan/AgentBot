# Setup

A step-by-step guide to get AgentBot running from a fresh clone on Windows.

## 1. Install Python 3.11

AgentBot is developed on **Python 3.11.9**. Download it from
[python.org](https://www.python.org/downloads/) or install it via `pyenv-win`
(see next step). Make sure Python is on your `PATH`.

## 2. (Recommended) pyenv-win

If you want to manage multiple Python versions:

```powershell
# Install pyenv-win (PowerShell), then restart your shell
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1" -OutFile "./install-pyenv-win.ps1"; &"./install-pyenv-win.ps1"

# Install and select Python 3.11.9
pyenv install 3.11.9
pyenv global 3.11.9
python --version    # -> Python 3.11.9
```

> **Tip:** run AgentBot from a normal PowerShell window where the `pyenv` shims
> resolve correctly. Some IDE-integrated terminals pick the wrong interpreter and
> fail to import `PySide6`.

## 3. Clone and install dependencies

```powershell
git clone https://github.com/SaifSirhan/AgentBot.git
cd AgentBot
pip install -r requirements.txt
```

### System binaries

- **Tesseract OCR** — required by the OCR tool. Install from
  [UB-Mannheim/tesseract](https://github.com/UB-Mannheim/tesseract/wiki) and make
  sure `tesseract` is on your `PATH`.
- **Chrome/Chromium** — required by the Selenium-based web tools. `webdriver-manager`
  fetches a matching driver automatically.

## 4. Configure the API key

The secret is read from an **environment variable** so it never touches disk:

```powershell
# Per-session
$env:DEEPSEEK_API_KEY  = "your-deepseek-key"
```

To persist it, set it as a **User environment variable** in
*Settings → System → About → Advanced system settings → Environment Variables*,
or with `setx`:

```powershell
setx DEEPSEEK_API_KEY "your-deepseek-key"
```

Non-secret settings live in **`%APPDATA%\AgentBot\config.json`** (created on
first run); the GUI's Settings → API Keys field writes the same value.

`DEEPSEEK_API_KEY` is the only model credential AgentBot uses. It serves text,
vision and web search/fetch, so there is no provider order to configure and no
fallback key to keep valid — if this call fails, the turn reports an error.

## 5. First run

```powershell
python agent_gui_qt.py
```

- The chat window opens. Type a request and press **Enter**.
- Type `/help` to see available slash commands.
- The system tray icon lets you show/hide the window.
- On first launch AgentBot may index documents for RAG — this can take a while
  and is logged to the activity log.

### Smoke check without the GUI

```powershell
python -c "import agent; print(agent.list_brains())"
```

This prints the client's status. Cooldown and dead-marking still apply, so a
rate-limited or auth-failed window is reported instead of re-tried every turn.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: PySide6` | Wrong Python interpreter. Use the pyenv 3.11.9 shell and `pip install -r requirements.txt`. |
| `DeepSeek is unavailable` | No valid `DEEPSEEK_API_KEY`, or the client is cooling down after errors. Check the env var and `list_brains()`. |
| OCR fails | Tesseract not installed or not on `PATH`. |
| Selenium fails | No Chrome/Chromium installed, or driver mismatch. |

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how requests flow through the system.
