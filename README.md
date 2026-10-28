# AgentBot

A highly capable, autonomous personal AI assistant for Windows. AgentBot turns natural-language requests into actions on your PC — controlling applications, editing documents, managing files, and navigating the web — wrapped in a custom PySide6 brutalist terminal interface.

<img width="1716" height="898" alt="image" src="https://github.com/user-attachments/assets/69e783c9-b905-4f45-9d5e-f105080e458c" />


## What it does

AgentBot is not just a wrapper around a chatbot; it is a true computer-use agent. It plans multi-step workflows, executes them via OS Control (mouse and keyboard automation), and verifies its own success. It indexes your documents for retrieval-augmented answers, remembers persistent facts, learns from its own mistakes, and fails over automatically between eight different LLM providers.

## Smart Architecture (The "Agent" Brain)

- **Autonomous ReAct Loop:** Unlike standard bots that give up after one step, AgentBot uses a Reason → Act → Observe → Verify loop. It keeps working until the job is done.
- **Dynamic Prompt Router:** Token budgets are strictly managed. The agent dynamically injects only the top 5 relevant tools and instructions based on the user's intent (e.g., "watch a movie" loads the Stremio/OS tools, while "edit a file" loads Python/File tools).
- **Verification Engine (Anti-Hallucination):** A strict, code-level gate intercepts the agent's final reply. If the agent claims it "sent a message" or "saved a file" but has no successful tool execution in its logs, the message is blocked and the agent is forced to try again.
- **Parameterized Macro Engine:** AgentBot learns muscle memory. When it successfully completes a complex UI task via OS Control, it can silently record those clicks and keystrokes into a reusable JSON macro, replacing them with variables (e.g., `{{TITLE}}`) for instant execution next time without burning Vision API tokens.
- **Experiential Learning (ExpeL):** A background process reviews the day's execution traces nightly, extracts lessons from failures (e.g., "Wait 3 seconds for Stremio to load"), and automatically updates the agent's core rules.

## Features

- **True OS Control:** Uses `pyautogui`, `mss`, and Vision-Language Models to take screenshots, identify UI elements, and physically move the mouse and click coordinates.
- **Native Office Automation:** Safely reads and edits `.docx` and `.xlsx` files using atomic saving and lock-detection to prevent data loss.
- **Multi-provider LLM failover** — Routes through a priority chain (DeepSeek, Groq, Gemini, OpenRouter, etc.) with per-provider cooldown/dead-marking on errors.
- **Hybrid Memory** — Combines ChromaDB semantic search with SQLite FTS5 keyword search for blazing-fast, accurate RAG.
- **Skills System (Progressive Disclosure):** Keeps the base prompt under 1.5k tokens by moving massive tool manuals into loadable `SKILL.md` files.
- **Voice** — Wake-word activation (openWakeWord), speech-to-text (faster-whisper), and text-to-speech (Kokoro).
- **Terminal UI** — A blazing-fast, pitch-black PySide6 interface with neon-mint aesthetics, inline ghost-text autocomplete, and zero bloat.

## Requirements

- **OS:** Windows 10/11 (several tools are Windows-specific).
- **Python:** 3.11 (developed on 3.11.9 via `pyenv-win`).
- **System binaries:** [MPV](https://mpv.io/) (for media playback), [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki), and a Chromium/Chrome browser.
- **API keys** for whichever LLM providers you want to use.

## Installation

```bash
# 1. Clone
git clone https://github.com/SaifSirhan/AgentBot.git
cd AgentBot

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows

# 3. Install dependencies
pip install -r requirements.txt
Configuration
AgentBot reads configuration from %APPDATA%\AgentBot\config.json and from environment variables. Secrets should be provided as environment variables so they are never written to disk.

Variable	Purpose
DEEPSEEK_API_KEY	DeepSeek provider (Primary)
GROQ_API_KEY	Groq provider (Fallback)
GEMINI_API_KEY	Google Gemini
AGENT_BRAIN_PRIORITY	Comma-separated provider order
Usage
bash
# Launch the PySide6 Terminal GUI
python agent_gui_qt.py
Type a request in the terminal prompt and press Enter. Type /help for the list of slash commands.

Tech Stack
GUI: PySide6 (Qt)

AI/Agentic Architecture: OpenAI SDK (DeepSeek), ReAct Loop, ExpeL

Automation: PyAutoGUI, Playwright, PyGetWindow, MSS

Memory/RAG: ChromaDB, SQLite, Sentence-Transformers

Audio: faster-whisper, Kokoro, openWakeWord

Project layout
File	Role
agent.py	LLM routing, tool dispatch, and the agent turn loop
agent_gui_qt.py	PySide6 Terminal UI
verification_engine.py	Anti-hallucination gate
macro_engine.py	Self-learning macro generator and runner
prompt_router.py	Dynamic token-saving prompt router
rag_tool.py	Document indexing and hybrid search
skills/	Progressive Disclosure tool manuals
License
MIT — see LICENSE.
