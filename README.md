# Vecna — Slack AI Agent

> **Smart Entity Disambiguator & Knowledge Resolver** — a context-aware Slack assistant powered by Google Gemini and Slack's Real-Time Search API.

Vecna answers natural-language questions about your Slack workspace. It searches messages, files, channels, and users through Slack's `assistant.search.context` endpoint, uses Gemini to plan queries and synthesize grounded answers, and interactively disambiguates ambiguous people or channel names within the same thread.

---

## Features

- **Natural-language workspace search** — ask questions like *"Where did we leave off on the database migration?"* and get a sourced answer from real Slack conversations.
- **Entity disambiguation** — if a query references an ambiguous name (e.g. *"What did Jason say about the launch?"*), the bot searches for matching users or channels and asks you to pick the right one, then continues with the refined search automatically.
- **Gemini-powered query planning** — before every search, Gemini analyses the intent (`disambiguate_user`, `disambiguate_channel`, `resolve_knowledge`, or `clarify`) and crafts an optimised search query.
- **Grounded answers with sources** — Gemini synthesises answers only from the returned search results and includes source permalinks.
- **Thread-local session state** — disambiguation state is tracked per channel/thread so concurrent conversations don't interfere.
- **Socket Mode** — no public HTTP endpoint required; the app connects to Slack over a persistent WebSocket.

---

## Architecture

```
app.py                          # Slack Bolt event handlers (entry point)
│
├── agent/
│   └── resolver.py             # KnowledgeResolver — orchestration layer
│                               #   SessionStore, ResolverSession,
│                               #   PendingDisambiguation, ResolverResult
│
└── services/
    ├── gemini_agent.py         # GeminiAgent — query planner & answer synthesizer
    └── slack_search.py         # SlackSearchService — assistant.search.context wrapper
```

### Request flow

```
User @mentions Vecna
        │
        ▼
app.py: handle_app_mention()
        │
        ▼
KnowledgeResolver.handle_mention()
        │
        ├─ GeminiAgent.analyze_query()        → intent + optimised search_query
        │
        ├─ intent == "disambiguate_user/channel"
        │       │
        │       ├─ SlackSearchService.search_context()  (users or channels only)
        │       │
        │       ├─ 1 candidate  → auto-select, jump to resolve_knowledge
        │       └─ N candidates → ask user to pick (session saved)
        │                │
        │                ▼  (user replies in-thread)
        │           KnowledgeResolver.handle_thread_reply()
        │                │
        │                └─ GeminiAgent.resolve_with_selected_entity()
        │
        └─ intent == "resolve_knowledge"
                │
                ├─ SlackSearchService.search_knowledge()  (messages + files + channels)
                │
                └─ GeminiAgent.resolve_answer()           → synthesized answer + sources
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Uses `str \| None` union syntax |
| Slack workspace | Admin access required to install the app |
| Slack app with Real-Time Search | Must be enrolled in the RTS API program |
| Google Gemini API key | [Get one at Google AI Studio](https://aistudio.google.com/apikey) |

---

## Slack App Setup

### 1. Create the app from the manifest

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**.
2. Paste the contents of [`manifest.json`](manifest.json).
3. Install the app to your workspace.

The manifest configures:

| Setting | Value |
|---|---|
| Bot display name | Vecna Search Agent |
| Socket Mode | Enabled |
| OAuth scopes | `app_mentions:read`, `assistant:write`, `channels:history`, `chat:write`, `groups:history`, `im:history`, `mpim:history`, `reactions:read`, `reactions:write`, `search:read.public`, `search:read.files`, `search:read.users` |
| Events | `app_context_changed`, `app_home_opened`, `message.im` |

### 2. Collect your tokens

| Token | Where to find it |
|---|---|
| `SLACK_BOT_TOKEN` | **OAuth & Permissions** → *Bot User OAuth Token* (`xoxb-…`) |
| `SLACK_APP_TOKEN` | **Basic Information** → *App-Level Tokens* → generate one with `connections:write` scope (`xapp-…`) |

---

## Local Development

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd Slack_AI_Agent

python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Dependencies:

| Package | Purpose |
|---|---|
| `slack-bolt` | Slack app framework + Socket Mode handler |
| `google-genai` | Google Gemini API client |
| `python-dotenv` | Loads `.env` into `os.environ` at startup |

### 3. Configure environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

`.env.example`:

```env
# Slack tokens
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token

# Gemini API key
GEMINI_API_KEY=your-gemini-api-key

# Optional: override the default model (default: gemini-2.5-flash)
# GEMINI_MODEL=gemini-2.5-flash
```

`app.py` calls `load_dotenv()` at startup, so the `.env` file is loaded automatically — no manual exporting needed.

### 4. Start the bot

```bash
python app.py
```

You should see Slack Bolt output confirming the Socket Mode connection. Mention the bot in any channel to start querying.

---

## Usage

### Basic knowledge query

```
@Vecna Where did we leave off on the database migration?
```

Vecna searches messages, files, and channels for relevant conversations and replies with a synthesized answer and source links.

### User disambiguation

```
@Vecna What did Jason say about the Q3 roadmap?
```

If multiple people named Jason exist in the workspace, Vecna replies with a numbered list and asks you to pick one. Reply with the number or name in the same thread:

```
2
```

Vecna then automatically searches with the selected user and responds with the answer.

### Channel disambiguation

```
@Vecna What's the latest in the infra channel?
```

If multiple channels match *infra*, Vecna presents options and waits for your selection.

### Clarification prompt

If the query is too vague, Vecna asks for more context rather than returning unhelpful results.

---

## Code Reference

### [`app.py`](app.py)

Entry point. Registers two Slack event handlers:

| Handler | Event | Description |
|---|---|---|
| `handle_app_mention` | `app_mention` | Triggered when the bot is @mentioned; starts a new resolver session |
| `handle_thread_reply` | `message` | Listens for in-thread replies; continues disambiguation flows |

### [`agent/resolver.py`](agent/resolver.py)

Core orchestration. Key classes:

| Class | Description |
|---|---|
| `KnowledgeResolver` | Main orchestrator — routes intents, calls search, drives disambiguation |
| `SessionStore` | In-memory dict keyed by `channel_id:thread_ts` |
| `ResolverSession` | Per-thread state machine (`idle` / `awaiting_disambiguation`) |
| `PendingDisambiguation` | Holds candidates and original query during a disambiguation round |
| `ResolverResult` | Return value containing `text`, optional Slack `blocks`, and `thread_ts` |

### [`services/gemini_agent.py`](services/gemini_agent.py)

Gemini integration. Three public methods:

| Method | Description |
|---|---|
| `analyze_query(user_query)` | Returns a planning JSON with `intent`, `search_query`, `content_types` |
| `format_disambiguation(...)` | Generates the numbered candidate list message |
| `resolve_answer(user_query, search_results)` | Synthesizes a grounded answer with source citations |
| `resolve_with_selected_entity(...)` | Builds a refined search query after entity selection |

Default model: `gemini-2.5-flash`. Override with the `GEMINI_MODEL` environment variable.

### [`services/slack_search.py`](services/slack_search.py)

Wrapper around Slack's `assistant.search.context` API. Key methods:

| Method | Description |
|---|---|
| `search_context(...)` | Full-featured call to `assistant.search.context` |
| `search_users(...)` | Convenience wrapper — `content_types=["users"]` only |
| `search_knowledge(...)` | Convenience wrapper — messages, files, channels with context messages |

Raises `SlackSearchError` on API failures, with human-readable messages for known error codes (`invalid_action_token`, `missing_scope`, `feature_not_enabled`, `rate_limited`).

---

## Configuration

| Environment variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | — | Bot user OAuth token (`xoxb-…`) |
| `SLACK_APP_TOKEN` | ✅ | — | App-level token for Socket Mode (`xapp-…`) |
| `GEMINI_API_KEY` | ✅ | — | Google Gemini API key |
| `GEMINI_MODEL` | ❌ | `gemini-2.5-flash` | Gemini model name |

---

## License

[MIT](LICENSE)
