# AgentBot

A personal Windows AI assistant with a tool-calling agent loop, multi-provider
LLM failover, local-document RAG, voice input/output, and messaging integrations
— wrapped in a CustomTkinter desktop GUI.

![Screenshot](assets/screenshot.png)

## What it does

AgentBot turns natural-language requests into actions on your PC. An LLM plans
each turn and calls local tools — finding and reading files, searching the web,
controlling system volume and windows, capturing the screen, scheduling
reminders, and sending messages — then summarises the result. It indexes your
documents for retrieval-augmented answers, remembers persistent facts across
sessions, and fails over automatically between several LLM providers so a single
outage or rate limit never stops it.

## Features

- **Multi-provider LLM failover** — routes through a priority chain
  (Groq, DeepSeek, Gemini, OpenRouter, Mistral, Hugging Face, Meta, Cohere,
  Ollama) with per-provider cooldown/dead-marking on errors.
- **Tool-calling agent loop** — the model selects and chains tools per turn.
- **Local RAG** — ChromaDB + Sentence-Transformers index and query your documents.
- **Document & file tools** — find files, read/summarise, `.docx`/`.xlsx`/`.pdf`
  parsing, and OCR via Tesseract.
- **Voice** — speech-to-text (faster-whisper) and text-to-speech (Kokoro).
- **Messaging integrations** — Telegram (Telethon), Gmail, WhatsApp.
- **Web tools** — search, fetch, and clean-read pages; weather & news feeds.
- **System control** — volume (pycaw), window management (pygetwindow),
  screen capture, notifications (winotify).
- **Scheduler** — recurring reminders and scheduled tasks.
- **Desktop GUI** — CustomTkinter chat UI with Markdown/code rendering,
  collapsible activity chips, an in-window settings overlay, and a system tray icon.
- **Persistent memory** — remembers user facts between conversations.

## Requirements

- **OS:** Windows 10/11 (several tools are Windows-specific: `pycaw`,
  `pygetwindow`, `winotify`, `pystray`, `winsound`).
- **Python:** 3.11 (developed on 3.11.9 via `pyenv-win`).
- **System binaries:** [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
  for the OCR tool; a Chromium/Chrome browser + matching driver for Selenium.
- **API keys** for whichever LLM providers and integrations you want to use.

## Installation

```bash
# 1. Clone
git clone https://github.com/<your-username>/AgentBot.git
cd AgentBot

# 2. Create and activate a virtual environment (optional but recommended)
python -m venv .venv
.venv\Scripts\activate        # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

## Configuration

AgentBot reads configuration from **`%APPDATA%\AgentBot\config.json`** and from
environment variables. Secrets should be provided as environment variables so
they are never written to disk.

| Variable | Purpose |
| --- | --- |
| `GROQ_API_KEY` | Groq provider |
| `DEEPSEEK_API_KEY` | DeepSeek provider |
| `GEMINI_API_KEY` | Google Gemini |
| `OPENROUTER_API_KEY` | OpenRouter |
| `MISTRAL_API_KEY` | Mistral |
| `HUGGINGFACE_API_KEY` | Hugging Face inference |
| `COHERE_API_KEY` | Cohere |
| `META_AI_API_KEY` | Meta AI |
| `OLLAMA_URL` / `OLLAMA_MODEL` | Local Ollama endpoint and model |
| `AGENT_BRAIN_PRIORITY` | Comma-separated provider order (overrides the built-in default) |

Non-secret settings (model names, brain priority, Telegram IDs, etc.) live in
`config.json`. See [`docs/SETUP.md`](docs/SETUP.md) for a full walkthrough.

> **Security note:** `config.json`, `credentials.json`, `*.session`, and other
> secret files are excluded via `.gitignore`. Never commit real keys.

## Usage

```bash
python agent_gui.py
```

Type a request in the chat box and press Enter. Type `/help` for the list of
slash commands. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how a
request flows through the system.

## Project layout

| File | Role |
| --- | --- |
| `agent.py` | LLM routing, tool dispatch, and the agent turn loop |
| `agent_gui.py` | CustomTkinter desktop UI |
| `gui_widgets.py` | Shared rich chat widgets (Markdown, code blocks, icons) |
| `rag_tool.py` | Document indexing and semantic search |
| `memory.py` | Persistent user facts |
| `telegram_user.py` | Telethon user-account bridge |
| `scheduler.py` | Reminders and scheduled tasks |
| `voice_output.py` | Text-to-speech |
| `voice.py` | Speech-to-text |

## License

MIT — see [LICENSE](LICENSE).
