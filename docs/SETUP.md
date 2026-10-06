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
> fail to import `customtkinter`.

## 3. Clone and install dependencies

```powershell
git clone https://github.com/<your-username>/AgentBot.git
cd AgentBot
pip install -r requirements.txt
```

### System binaries

- **Tesseract OCR** — required by the OCR tool. Install from
  [UB-Mannheim/tesseract](https://github.com/UB-Mannheim/tesseract/wiki) and make
  sure `tesseract` is on your `PATH`.
- **Chrome/Chromium** — required by the Selenium-based web tools. `webdriver-manager`
  fetches a matching driver automatically.

## 4. Configure API keys

Secrets are read from **environment variables** so they never touch disk. Set the
ones you need (PowerShell):

```powershell
# Per-session
$env:GROQ_API_KEY      = "your-groq-key"
$env:DEEPSEEK_API_KEY  = "your-deepseek-key"
$env:GEMINI_API_KEY    = "your-gemini-key"
# ... OPENROUTER_API_KEY, MISTRAL_API_KEY, HUGGINGFACE_API_KEY,
#     COHERE_API_KEY, META_AI_API_KEY as desired
```

To persist them, set them as **User environment variables** in
*Settings → System → About → Advanced system settings → Environment Variables*,
or with `setx`:

```powershell
setx DEEPSEEK_API_KEY "your-deepseek-key"
```

Non-secret settings live in **`%APPDATA%\AgentBot\config.json`** (created on
first run). You can optionally set the provider order there:

```json
{
  "AGENT_BRAIN_PRIORITY": "groq,deepseek,gemini,openrouter,mistral,huggingface,meta,cohere,ollama"
}
```

At least **one** provider key must be valid for the agent to answer. Local
**Ollama** works with no cloud key if you run `ollama serve` and set
`OLLAMA_URL` / `OLLAMA_MODEL`.

## 5. First run

```powershell
python agent_gui.py
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

This prints each provider's status in priority order.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: customtkinter` | Wrong Python interpreter. Use the pyenv 3.11.9 shell and `pip install -r requirements.txt`. |
| `All brains unavailable` | No valid API key set, or every provider is cooling/dead. Check env vars and `list_brains()`. |
| OCR fails | Tesseract not installed or not on `PATH`. |
| Selenium fails | No Chrome/Chromium installed, or driver mismatch. |

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how requests flow through the system.
