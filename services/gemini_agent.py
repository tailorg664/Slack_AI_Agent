"""Gemini LLM integration for query analysis and answer synthesis."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from google import genai
from google.genai import types

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

ANALYZE_QUERY_PROMPT = """\
You are the planning brain for "Smart Entity Disambiguator & Knowledge Resolver", \
a Slack workspace assistant.

Given a user question, decide how to search Slack using assistant.search.context.

Return ONLY valid JSON with this shape:
{
  "intent": "disambiguate_user" | "disambiguate_channel" | "resolve_knowledge" | "clarify",
  "entity_name": "<name to disambiguate, or null>",
  "search_query": "<optimized query for assistant.search.context>",
  "content_types": ["messages", "files", "channels", "users"],
  "reasoning": "<one sentence>"
}

Rules:
- If the user mentions a person by first name only (e.g. "Jason") and context is ambiguous, \
use intent "disambiguate_user" with content_types ["users"].
- If the user asks about a channel/project name that may match multiple channels, \
use intent "disambiguate_channel" with content_types ["channels"].
- For factual/historical workspace questions, use intent "resolve_knowledge" with \
content_types ["messages", "files", "channels"] and a search_query that preserves key terms.
- Phrase search_query as a natural-language question when semantic search is helpful \
(e.g. "What is the latest on project Gizmo?").
- If the query is too vague, use intent "clarify".
"""

RESOLVE_ANSWER_PROMPT = """\
You are "Smart Entity Disambiguator & Knowledge Resolver", a Slack workspace assistant.

Answer the user's question using ONLY the provided Slack search results. \
Do not invent facts. If results are insufficient, say so clearly.

Format:
1. A concise direct answer (2-4 sentences).
2. A "Sources" section listing permalinks from the search results you used.

Keep Slack mrkdwn formatting: use *bold* sparingly, `<url|label>` for links.
"""

DISAMBIGUATE_PROMPT = """\
You are helping disambiguate a Slack workspace entity for the user.

Present the candidates clearly and ask the user to pick one. \
Keep it brief. Use numbered options. Include name, title/role, and email when available.
"""


class GeminiAgent:
    """Gemini-powered query planner and answer synthesizer."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY environment variable is required")
        self.client = genai.Client(api_key=key)
        self.model = model or DEFAULT_MODEL

    def _generate(self, system: str, user: str) -> str:
        response = self.client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.2,
            ),
        )
        return (response.text or "").strip()

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return json.loads(cleaned)

    def analyze_query(self, user_query: str) -> dict[str, Any]:
        """Plan the RTS search strategy for a user question."""
        raw = self._generate(ANALYZE_QUERY_PROMPT, user_query)
        try:
            plan = self._parse_json(raw)
        except json.JSONDecodeError:
            plan = {
                "intent": "resolve_knowledge",
                "entity_name": None,
                "search_query": user_query,
                "content_types": ["messages", "files", "channels"],
                "reasoning": "Fallback: could not parse planner JSON.",
            }

        plan.setdefault("intent", "resolve_knowledge")
        plan.setdefault("search_query", user_query)
        plan.setdefault("content_types", ["messages", "files", "channels"])
        return plan

    def format_disambiguation(
        self,
        *,
        entity_type: str,
        entity_name: str,
        candidates: list[dict[str, Any]],
        original_query: str,
    ) -> str:
        """Generate a user-facing disambiguation message."""
        payload = json.dumps(
            {
                "entity_type": entity_type,
                "entity_name": entity_name,
                "original_query": original_query,
                "candidates": candidates,
            },
            indent=2,
        )
        return self._generate(DISAMBIGUATE_PROMPT, payload)

    def resolve_answer(self, user_query: str, search_results: dict[str, Any]) -> str:
        """Synthesize a grounded answer from RTS search results."""
        payload = json.dumps(
            {"user_query": user_query, "search_results": search_results},
            indent=2,
            default=str,
        )
        return self._generate(RESOLVE_ANSWER_PROMPT, payload)

    def resolve_with_selected_entity(
        self,
        original_query: str,
        entity_type: str,
        selected: dict[str, Any],
    ) -> str:
        """Build a refined search query after the user picks a disambiguated entity."""
        user_id = selected.get("user_id")
        channel_name = selected.get("name")
        if entity_type == "user" and user_id:
            return f"{original_query} with <@{user_id}>"
        if entity_type == "channel" and channel_name:
            return f"{original_query} in #{channel_name}"
        return original_query
