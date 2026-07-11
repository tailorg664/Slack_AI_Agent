"""Smart Entity Disambiguator & Knowledge Resolver — core agent orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from services.gemini_agent import GeminiAgent
from services.slack_search import SlackSearchError, SlackSearchService

EntityType = Literal["user", "channel"]
ResolverPhase = Literal["idle", "awaiting_disambiguation"]


@dataclass
class PendingDisambiguation:
    """Tracks an in-progress entity disambiguation within a thread."""

    original_query: str
    entity_type: EntityType
    entity_name: str
    candidates: list[dict[str, Any]]
    action_token: str
    context_channel_id: str | None = None


@dataclass
class ResolverSession:
    thread_ts: str
    channel_id: str
    user_id: str
    phase: ResolverPhase = "idle"
    pending: PendingDisambiguation | None = None


@dataclass
class ResolverResult:
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    thread_ts: str | None = None


class SessionStore:
    """In-memory session store keyed by channel:thread."""

    def __init__(self) -> None:
        self._sessions: dict[str, ResolverSession] = {}

    @staticmethod
    def _key(channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def get(self, channel_id: str, thread_ts: str) -> ResolverSession | None:
        return self._sessions.get(self._key(channel_id, thread_ts))

    def put(self, session: ResolverSession) -> None:
        self._sessions[self._key(session.channel_id, session.thread_ts)] = session

    def clear(self, channel_id: str, thread_ts: str) -> None:
        self._sessions.pop(self._key(channel_id, thread_ts), None)


class KnowledgeResolver:
    """
    Orchestrates Gemini planning + Slack Real-Time Search to:
      1. Disambiguate ambiguous entities (users, channels)
      2. Resolve knowledge from workspace conversations
    """

    DISAMBIGUATION_THRESHOLD = 1  # >1 candidate triggers disambiguation

    def __init__(
        self,
        search: SlackSearchService,
        gemini: GeminiAgent,
        sessions: SessionStore | None = None,
    ):
        self.search = search
        self.gemini = gemini
        self.sessions = sessions or SessionStore()

    def handle_mention(
        self,
        *,
        user_query: str,
        action_token: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> ResolverResult:
        """Entry point when the bot is @mentioned."""
        self.sessions.clear(channel_id, thread_ts)
        return self._process_query(
            user_query=user_query,
            action_token=action_token,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
        )

    def handle_thread_reply(
        self,
        *,
        reply_text: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> ResolverResult | None:
        """Continue a disambiguation flow when the user replies in-thread."""
        session = self.sessions.get(channel_id, thread_ts)
        if not session or session.phase != "awaiting_disambiguation" or not session.pending:
            return None

        pending = session.pending
        selected = self._match_candidate(reply_text, pending.candidates, pending.entity_type)
        if not selected:
            return ResolverResult(
                text=(
                    "I couldn't match your reply to one of the options. "
                    "Please reply with the number (1, 2, …) or the full name."
                ),
                thread_ts=thread_ts,
            )

        refined_query = self.gemini.resolve_with_selected_entity(
            pending.original_query,
            pending.entity_type,
            selected,
        )
        self.sessions.clear(channel_id, thread_ts)
        return self._resolve_knowledge(
            user_query=pending.original_query,
            search_query=refined_query,
            action_token=pending.action_token,
            channel_id=channel_id,
            thread_ts=thread_ts,
            context_channel_id=pending.context_channel_id,
        )

    def _process_query(
        self,
        *,
        user_query: str,
        action_token: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> ResolverResult:
        plan = self.gemini.analyze_query(user_query)
        intent = plan.get("intent", "resolve_knowledge")
        search_query = plan.get("search_query", user_query)
        content_types = plan.get("content_types", ["messages", "files", "channels"])

        if intent == "clarify":
            return ResolverResult(
                text=(
                    "Could you share a bit more detail? For example, mention a person, "
                    "channel, project name, or timeframe so I can search your workspace."
                ),
                thread_ts=thread_ts,
            )

        if intent == "disambiguate_user":
            return self._disambiguate_entity(
                entity_type="user",
                entity_name=plan.get("entity_name") or search_query,
                original_query=user_query,
                search_query=search_query,
                action_token=action_token,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
            )

        if intent == "disambiguate_channel":
            return self._disambiguate_entity(
                entity_type="channel",
                entity_name=plan.get("entity_name") or search_query,
                original_query=user_query,
                search_query=search_query,
                action_token=action_token,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
            )

        return self._resolve_knowledge(
            user_query=user_query,
            search_query=search_query,
            action_token=action_token,
            channel_id=channel_id,
            thread_ts=thread_ts,
            content_types=content_types,
            context_channel_id=channel_id,
        )

    def _disambiguate_entity(
        self,
        *,
        entity_type: EntityType,
        entity_name: str,
        original_query: str,
        search_query: str,
        action_token: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> ResolverResult:
        content_types = ["users"] if entity_type == "user" else ["channels"]

        try:
            data = self.search.search_context(
                query=search_query,
                action_token=action_token,
                content_types=content_types,
                include_context_messages=False,
                context_channel_id=channel_id,
                limit=5,
            )
        except SlackSearchError as exc:
            return ResolverResult(
                text=self._format_search_error(exc),
                thread_ts=thread_ts,
            )

        results = data.get("results", {})
        key = "users" if entity_type == "user" else "channels"
        candidates = results.get(key, [])

        if not candidates:
            return ResolverResult(
                text=f"I couldn't find any {key} matching *{entity_name}* in your workspace.",
                thread_ts=thread_ts,
            )

        if len(candidates) == 1:
            refined = self.gemini.resolve_with_selected_entity(
                original_query,
                entity_type,
                candidates[0],
            )
            return self._resolve_knowledge(
                user_query=original_query,
                search_query=refined,
                action_token=action_token,
                channel_id=channel_id,
                thread_ts=thread_ts,
                context_channel_id=channel_id,
            )

        text = self.gemini.format_disambiguation(
            entity_type=entity_type,
            entity_name=entity_name,
            candidates=candidates,
            original_query=original_query,
        )

        session = ResolverSession(
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            phase="awaiting_disambiguation",
            pending=PendingDisambiguation(
                original_query=original_query,
                entity_type=entity_type,
                entity_name=entity_name,
                candidates=candidates,
                action_token=action_token,
                context_channel_id=channel_id,
            ),
        )
        self.sessions.put(session)

        blocks = self._disambiguation_blocks(entity_type, candidates, text)
        return ResolverResult(text=text, blocks=blocks, thread_ts=thread_ts)

    def _resolve_knowledge(
        self,
        *,
        user_query: str,
        search_query: str,
        action_token: str,
        channel_id: str,
        thread_ts: str,
        content_types: list[str] | None = None,
        context_channel_id: str | None = None,
    ) -> ResolverResult:
        try:
            data = self.search.search_knowledge(
                query=search_query,
                action_token=action_token,
                content_types=content_types,
                context_channel_id=context_channel_id or channel_id,
            )
        except SlackSearchError as exc:
            return ResolverResult(
                text=self._format_search_error(exc),
                thread_ts=thread_ts,
            )

        results = data.get("results", {})
        if not any(results.get(k) for k in ("messages", "files", "channels", "users")):
            return ResolverResult(
                text=(
                    "I searched your workspace but didn't find relevant messages, files, "
                    "or channels. Try rephrasing with a project name, channel, or person."
                ),
                thread_ts=thread_ts,
            )

        answer = self.gemini.resolve_answer(user_query, results)
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": answer},
            }
        ]
        return ResolverResult(text=answer, blocks=blocks, thread_ts=thread_ts)

    @staticmethod
    def _match_candidate(
        reply: str,
        candidates: list[dict[str, Any]],
        entity_type: EntityType,
    ) -> dict[str, Any] | None:
        reply_lower = reply.strip().lower()

        if reply_lower.isdigit():
            idx = int(reply_lower) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]

        for candidate in candidates:
            if entity_type == "user":
                names = [
                    candidate.get("full_name", ""),
                    candidate.get("display_name", ""),
                    candidate.get("real_name", ""),
                ]
            else:
                names = [candidate.get("name", ""), candidate.get("topic", "")]
            if any(name and name.lower() in reply_lower for name in names):
                return candidate
            if any(name and reply_lower in name.lower() for name in names):
                return candidate

        return None

    @staticmethod
    def _disambiguation_blocks(
        entity_type: EntityType,
        candidates: list[dict[str, Any]],
        intro_text: str,
    ) -> list[dict[str, Any]]:
        options: list[str] = []
        for i, candidate in enumerate(candidates, start=1):
            if entity_type == "user":
                label = candidate.get("full_name") or candidate.get("display_name", "Unknown")
                title = candidate.get("title") or candidate.get("email") or ""
                options.append(f"*{i}.* {label}" + (f" — {title}" if title else ""))
            else:
                name = candidate.get("name", "unknown")
                topic = candidate.get("topic") or candidate.get("purpose") or ""
                options.append(f"*{i}.* #{name}" + (f" — {topic}" if topic else ""))

        body = intro_text + "\n\n" + "\n".join(options)
        return [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]

    @staticmethod
    def _format_search_error(exc: SlackSearchError) -> str:
        messages = {
            "invalid_action_token": (
                "I couldn't authenticate this search request. "
                "Please mention me again in this channel to start a fresh search."
            ),
            "missing_scope": (
                "I'm missing required search permissions. "
                "Ask your workspace admin to reinstall the app with Real-Time Search scopes."
            ),
            "feature_not_enabled": (
                "Real-Time Search isn't enabled for this workspace yet. "
                "Contact Slack support to enroll in the RTS API program."
            ),
            "rate_limited": "Slack rate-limited the search. Please wait a moment and try again.",
        }
        return messages.get(
            exc.error,
            f"Search failed ({exc.error}). Please try again.",
        )
