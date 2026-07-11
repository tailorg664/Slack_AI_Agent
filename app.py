import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App
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


def _strip_bot_mention(text: str) -> str:
    """Remove @bot mention markup from message text."""
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def _reply(say, result, *, thread_ts: str | None = None):
    kwargs: dict = {"text": result.text, "thread_ts": thread_ts or result.thread_ts}
    if result.blocks:
        kwargs["blocks"] = result.blocks
    say(**kwargs)


@app.event("app_mention")
def handle_app_mention(event, say, logger):
    action_token = event.get("action_token")
    if not action_token:
        say(
            text=(
                "I need an *action_token* from Slack to search on your behalf. "
                "Make sure Real-Time Search scopes are installed and mention me in a "
                "channel, group, or DM."
            ),
            thread_ts=event.get("ts"),
        )
        return

    user_query = _strip_bot_mention(event.get("text", ""))
    if not user_query:
        say(
            text=(
                "Hi! I'm *Smart Entity Disambiguator & Knowledge Resolver*. "
                "Ask me something about your workspace — e.g. "
                "_\"Where did we leave off on the database migration?\"_"
            ),
            thread_ts=event.get("ts"),
        )
        return

    logger.info("Processing mention query=%r channel=%s", user_query, event["channel"])

    try:
        result = resolver.handle_mention(
            user_query=user_query,
            action_token=action_token,
            channel_id=event["channel"],
            thread_ts=event["ts"],
            user_id=event["user"],
        )
        _reply(say, result, thread_ts=event["ts"])
    except Exception:
        logger.exception("Failed to handle mention")
        say(
            text="Something went wrong while searching. Please try again.",
            thread_ts=event.get("ts"),
        )


@app.event("message")
def handle_thread_reply(event, say, logger):
    """Handle follow-up replies during entity disambiguation."""
    if event.get("bot_id") or event.get("subtype"):
        return

    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    session = sessions.get(event["channel"], thread_ts)
    if not session or session.phase != "awaiting_disambiguation":
        return

    logger.info(
        "Processing disambiguation reply=%r channel=%s thread=%s",
        event.get("text"),
        event["channel"],
        thread_ts,
    )

    try:
        result = resolver.handle_thread_reply(
            reply_text=event.get("text", ""),
            channel_id=event["channel"],
            thread_ts=thread_ts,
            user_id=event["user"],
        )
        if result:
            _reply(say, result, thread_ts=thread_ts)
    except Exception:
        logger.exception("Failed to handle thread reply")
        say(
            text="Something went wrong. Please mention me again to restart.",
            thread_ts=thread_ts,
        )


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
