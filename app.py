import logging
import os

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App, Assistant
from slack_bolt.adapter.socket_mode import SocketModeHandler

from agent.resolver import KnowledgeResolver, SessionStore
from services.gemini_agent import GeminiAgent
from services.slack_search import SlackSearchService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

sessions = SessionStore()
search_service = SlackSearchService(app.client)
gemini_agent = GeminiAgent()
resolver = KnowledgeResolver(search_service, gemini_agent, sessions)

# ---------------------------------------------------------------------------
# Slack AI Assistant middleware
# Handles assistant_thread_started, assistant_thread_context_changed, and
# user messages inside assistant DM threads — the only events that carry a
# valid action_token for assistant.search.context (Real-Time Search).
# ---------------------------------------------------------------------------
assistant = Assistant()


@assistant.thread_started
def handle_thread_started(say, set_suggested_prompts, payload, logger):
    """
    Fired when a user opens a new assistant DM thread.
    Set the welcome message and suggested prompts.
    """
    logger.info(
        "assistant_thread_started: channel=%s thread_ts=%s",
        payload.get("assistant_thread", {}).get("channel_id"),
        payload.get("assistant_thread", {}).get("thread_ts"),
    )
    set_suggested_prompts(
        prompts=[
            {
                "title": "Find a conversation",
                "message": "Where did we leave off discussing the database migration?",
            },
            {
                "title": "Look up a person",
                "message": "Who is Jason on the analytics team?",
            },
        ]
    )
    say(
        text=(
            "Hi! I'm *Vecna*, your workspace search agent. "
            "Ask me anything about your Slack conversations — e.g. "
            "_\"Where did we leave off on the database migration?\"_"
        )
    )


@assistant.thread_context_changed
def handle_thread_context_changed(save_thread_context, payload, logger):
    """
    Fired when the user navigates to a different channel while the assistant
    thread is open. Persist the new context so we can pass context_channel_id
    to assistant.search.context.
    """
    context = payload.get("assistant_thread", {}).get("context", {})
    logger.info("assistant_thread_context_changed: new context=%s", context)
    save_thread_context(context)


@assistant.user_message
def handle_user_message(payload, get_thread_context, say, set_status, logger):
    """
    Fired for every user message inside an assistant DM thread.
    The payload carries a valid action_token for Real-Time Search.
    """
    action_token = payload.get("action_token")
    user_query = payload.get("text", "").strip()
    channel_id = payload.get("channel")
    thread_ts = payload.get("thread_ts")
    user_id = payload.get("user")

    # Recover the channel the user was viewing when they typed the query.
    thread_context = get_thread_context()
    context_channel_id = (thread_context or {}).get("channel_id") or channel_id

    logger.info(
        "user_message: user=%s channel=%s thread_ts=%s has_action_token=%s context_channel=%s query=%r",
        user_id,
        channel_id,
        thread_ts,
        bool(action_token),
        context_channel_id,
        user_query,
    )

    if not action_token:
        say(
            text=(
                "I need an *action_token* from Slack to search on your behalf. "
                "Make sure the app has the `assistant:write` scope and is enrolled "
                "in Real-Time Search."
            )
        )
        return

    if not user_query:
        return

    # --- Continue a pending disambiguation flow if one is active ---
    session = sessions.get(channel_id, thread_ts)
    if session and session.phase == "awaiting_disambiguation":
        try:
            result = resolver.handle_thread_reply(
                reply_text=user_query,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
            )
            if result:
                kwargs = {"text": result.text}
                if result.blocks:
                    kwargs["blocks"] = result.blocks
                say(**kwargs)
        except Exception:
            logger.exception("Failed to handle disambiguation reply")
            say(text="Something went wrong. Please try again.")
        return

    # --- Fresh query ---
    set_status("Searching your workspace…")

    try:
        result = resolver.handle_mention(
            user_query=user_query,
            action_token=action_token,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
        )
        kwargs = {"text": result.text}
        if result.blocks:
            kwargs["blocks"] = result.blocks
        say(**kwargs)
    except Exception:
        logger.exception("Failed to handle user message")
        say(text="Something went wrong while searching. Please try again.")


# Register the Assistant middleware with the app.
app.use(assistant)


# ---------------------------------------------------------------------------
# Boilerplate event acks — suppress unhandled-event warnings
# ---------------------------------------------------------------------------
@app.event("app_home_opened")
def handle_app_home_opened(event, logger):
    logger.debug("app_home_opened user=%s", event.get("user"))


@app.event("app_context_changed")
def handle_app_context_changed(event, logger):
    logger.debug("app_context_changed received")


@app.event("message")
def handle_other_messages(event, logger):
    """
    Catch-all for message.im events that are NOT inside an assistant thread
    (e.g. bot messages, subtypes, or DMs sent before an assistant thread
    is opened). The Assistant middleware handles the real assistant-thread
    messages before this runs; this just prevents 404 unhandled-request errors.
    """
    logger.debug(
        "ignored message event subtype=%s bot_id=%s",
        event.get("subtype"),
        event.get("bot_id"),
    )


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
