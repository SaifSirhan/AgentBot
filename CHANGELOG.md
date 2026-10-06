# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- DeepSeek V4 Flash (`deepseek-flash`) as an LLM provider, with explicit
  thinking-mode control: reasoning disabled for normal tool-calling turns and a
  dedicated high-effort thinking path used by `deep_research`.
- Re-added `deepseek` to the default brain priority chain (second, after Groq).
- Full GUI rehaul: DeepSeek-style chat layout with a collapsible sidebar,
  sparse header, bubble-less assistant Markdown, code blocks with copy buttons,
  per-turn collapsible activity chips, and an in-window settings overlay.
- Shared rich-widget module (`gui_widgets.py`) for Markdown, code blocks,
  activity chips, and high-resolution PIL-rendered icons.
- Memory-only reset in Settings (clears saved conversation history while keeping
  the on-screen chat).
- `/map` and `/symbols` slash commands for code navigation.
- Project documentation: README, LICENSE (MIT), CONTRIBUTING, CHANGELOG,
  `requirements.txt`, GitHub issue/PR templates, and `docs/` guides.

### Changed
- Provider model IDs corrected and the failover chain reordered; dead providers
  (cerebras, cloudflare) removed from the default priority.
- Settings dialog converted from a separate top-level window to an in-window
  overlay.

### Fixed
- Missing-comma bug in the RAG path configuration.
- Duplicate config load and hardcoded Ollama URL/model overriding user config.

[Unreleased]: https://github.com/your-username/AgentBot/commits/main
