# Architecture

AgentBot is a Windows desktop assistant. A user request enters through the GUI
(or a messaging bridge), is planned by an LLM, executed through local tools, and
the result is summarised back to the user.

## Modules

| Module | Responsibility |
| --- | --- |
| `agent.py` | Core: LLM provider routing + failover, tool dispatch, and the multi-step agent turn loop. Also holds the tool-description prompt the model uses to choose tools. |
| `agent_gui.py` | CustomTkinter desktop UI — chat view, sidebar, input pill, settings overlay, tray, hotkeys, mic. |
| `gui_widgets.py` | Shared rich widgets: Markdown text, code blocks, activity chips, PIL-rendered icons. |
| `rag_tool.py` | Indexes local documents into ChromaDB with Sentence-Transformers embeddings; semantic search for retrieval-augmented answers. |
| `memory.py` | Persistent user facts recalled across conversations. |
| `telegram_user.py` | Telethon wrapper for a Telegram **user** account (send/receive as you). |
| `scheduler.py` | Reminders and recurring/scheduled tasks. |
| `voice_output.py` | Text-to-speech (Kokoro). |
| `voice.py` | Speech-to-text (faster-whisper). |
| `config.py` | Loads configuration from `%APPDATA%\AgentBot\config.json` and environment variables. |

Supporting tool modules include `file_tools.py`, `export_tools.py`, `screen.py`,
`weather_news.py`, `gmail_tool.py`, `whatsapp_tool.py`, `air_quality.py`, and
`tray.py`.

## LLM routing & failover

`agent.py` keeps a **priority chain** of providers (Groq, DeepSeek, Gemini,
OpenRouter, Mistral, Hugging Face, Meta, Cohere, Ollama). For each call it walks
the chain and uses the first provider that returns a non-error response.
Providers that error are classified (auth/quota → marked dead; rate-limit/5xx →
temporary cooldown) so the chain self-heals without retrying a known-bad
provider on every turn.

DeepSeek V4 Flash has explicit **thinking-mode control**: reasoning is disabled
for normal tool-calling turns (so a small `max_tokens` budget isn't consumed by
reasoning tokens), while `deep_research` uses a high-effort thinking path.

## Request flow

```
                     +---------------------------+
   user request ---> |  agent_gui.py / Telegram  |
   (typed, voice,    +-------------+-------------+
    or scheduled)                  |
                                   v
                     +---------------------------+
                     |   agent.py: run_agent_turn |
                     |   (multi-step loop,        |
                     |    MAX_STEPS_PER_TURN)     |
                     +-------------+-------------+
                                   |
                     plan (LLM)    v
              +--------------------+--------------------+
              |  _call_llm  ->  BRAIN_PRIORITY failover   |
              |  groq -> deepseek -> gemini -> ...        |
              +--------------------+---------------------+
                                   |
                    model returns either
                    (a) plain text  or  (b) {tool, input}
                                   |
                                   v
                     +---------------------------+
                     |   tool dispatch           |
                     |  find_files / read /      |
                     |  search_web / rag /       |
                     |  scheduler / system / ... |
                     +-------------+-------------+
                                   |
                        tool result fed back as context
                                   |
                                   v
                     (loop until a final text answer)
                                   |
                                   v
                     +---------------------------+
                     |  summary shown in GUI /    |
                     |  sent via Telegram / TTS   |
                     +---------------------------+
```

### Turn loop detail

1. The GUI (or bridge) passes the user text plus recent conversation context to
   `run_agent_turn`.
2. `agent.py` builds a prompt containing the tool descriptions, known facts, the
   current time, and the conversation context, then calls `_call_llm`.
3. The model replies with **either** a plain-text answer **or** a single JSON
   tool call `{"tool": ..., "input": ...}`.
4. If it's a tool call, the tool runs; its output is appended to context and the
   loop repeats (bounded by `MAX_STEPS_PER_TURN`).
5. When the model returns plain text, that final answer is displayed, optionally
   spoken via TTS, and stored in memory/history.
