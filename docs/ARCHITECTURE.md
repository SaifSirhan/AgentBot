# Architecture

AgentBot is a Windows desktop assistant. A user request enters through the GUI
(or a messaging bridge), is planned by an LLM, executed through local tools, and
the result is summarised back to the user.

## Modules

| Module | Responsibility |
| --- | --- |
| `agent.py` | Core: the single DeepSeek client (text, vision, search/fetch), tool dispatch, and the multi-step agent turn loop. Also holds the tool-description prompt the model uses to choose tools. |
| `native_tools.py` | Adapter between the loop's text contracts and OpenAI-native function calling: builds the `tools` array, converts `tool_calls` back into action dicts, and replays `step_log` as `assistant`/`tool` messages. Owns no state. |
| `agent_gui_qt.py` | PySide6 desktop UI — chat view, sidebar, input pill, settings overlay, tray, hotkeys, mic. |
| `rag_tool.py` | Indexes local documents into ChromaDB with Sentence-Transformers embeddings; semantic search for retrieval-augmented answers. |
| `memory.py` | Persistent user facts recalled across conversations. |
| `telegram_user.py` | Telethon wrapper for a Telegram **user** account (send/receive as you). |
| `scheduler.py` | Reminders and recurring/scheduled tasks. |
| `voice_output.py` | Text-to-speech (Kokoro). |
| `voice.py` | Speech-to-text (faster-whisper). |
| `config.py` | Loads configuration from `%APPDATA%\AgentBot\config.json` and environment variables. |

Supporting tool modules include `file_tools.py`, `export_tools.py`, `screen.py`,
`weather_news.py`, `gmail_tool.py`, `whatsapp_tool.py`, and `air_quality.py`.

## LLM client

`agent.py` talks to **one** client: `deepseek-flash` at `api.deepseek.com`,
which serves text, vision (`image_url` content parts) and web search/fetch
(`/v1/tools/search`, `/v1/tools/fetch`). Errors are still classified
(auth/quota → dead; rate-limit/5xx → cooldown) so a known-bad window is not
re-tried on every turn, but there is no chain to fall over to — a failed call
returns an error the caller can see.

DeepSeek V4 Flash has explicit **thinking-mode control**: reasoning is disabled
for normal tool-calling turns (so a small `max_tokens` budget isn't consumed by
reasoning tokens), while `deep_research` uses a high-effort thinking path.

## Native tool calling

Each step is sent with a `tools` array of JSON-Schema functions built from the
turn's routed catalog (`native_tools.build_specs`). Migrated tools bring their
registry `input_schema`; the rest advertise the single `input` string the
legacy dispatcher already expects. The response's `tool_calls` become the same
action dicts the dispatcher has always taken, and the observation returns as a
`role: "tool"` message keyed to the call's id.

Two details that are easy to get wrong:

- `tool_choice` stays `"auto"`. `"required"` is refused while thinking mode is
  on, and forcing a call teaches the model to invent one to satisfy the API.
- The provider will echo tool names it was never given, so returned names are
  checked against `VALID_TOOLS` **and** this turn's allowlist before anything
  executes. A rejection is retried with the error attached; it is not treated
  as a transport failure.

`tool_calls` may arrive in parallel, which maps onto the existing read-only
batch path — writes are still refused there.

The transcript is rebuilt from `step_log` on every step rather than stored as
extra turn state, so there is one record of what ran. If the native call itself
fails, the step falls back to the JSON-object text protocol; that is the only
remaining use of it.

## Request flow

```
                     +---------------------------+
   user request ---> |  agent_gui_qt / Telegram  |
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
              |  _call_llm  ->  deepseek-flash (one client) |
              |  no failover: a failed call returns an error |
              +--------------------+---------------------+
                                   |
                    model returns either
                    (a) plain text  or  (b) native tool_calls
                        |                    |
                 reply to user        executed, then sent back
                                      as role:"tool" messages
                                      (native_tools.py adapts)
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
