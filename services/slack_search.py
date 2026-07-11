"""Slack Real-Time Search API client for assistant.search.context."""

from __future__ import annotations

from typing import Any

from slack_sdk.errors import SlackApiError

ContentType = str  # "messages" | "files" | "channels" | "users"
ChannelType = str  # "public_channel" | "private_channel" | "mpim" | "im"

DEFAULT_CONTENT_TYPES: list[ContentType] = ["messages", "files", "channels", "users"]
DEFAULT_CHANNEL_TYPES: list[ChannelType] = [
    "public_channel",
    "private_channel",
    "mpim",
    "im",
]


class SlackSearchError(Exception):
    """Raised when assistant.search.context returns an error."""

    def __init__(self, error: str, response: dict[str, Any] | None = None):
        self.error = error
        self.response = response or {}
        super().__init__(error)


class SlackSearchService:
    """Wraps Slack's assistant.search.context Real-Time Search endpoint."""

    def __init__(self, client: Any):
        self.client = client

    def search_context(
        self,
        *,
        query: str,
        action_token: str,
        content_types: list[ContentType] | None = None,
        include_context_messages: bool = True,
        channel_types: list[ChannelType] | None = None,
        context_channel_id: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
        sort: str | None = None,
        sort_dir: str | None = None,
        after: int | None = None,
        before: int | None = None,
        include_bots: bool = False,
        disable_semantic_search: bool = False,
    ) -> dict[str, Any]:
        """
        Search Slack workspace content on behalf of the user who triggered the action.

        Required for bot tokens:
          - query: search string or natural-language question
          - action_token: from the message/app_mention event payload

        Key optional parameters:
          - content_types: mix of messages, files, channels, users
          - include_context_messages: surrounding thread/channel messages around matches
        """
        payload: dict[str, Any] = {
            "query": query,
            "action_token": action_token,
            "content_types": content_types or DEFAULT_CONTENT_TYPES,
            "include_context_messages": include_context_messages,
            "channel_types": channel_types or DEFAULT_CHANNEL_TYPES,
            "limit": min(limit, 20),
            "include_bots": include_bots,
        }

        if context_channel_id:
            payload["context_channel_id"] = context_channel_id
        if cursor:
            payload["cursor"] = cursor
        if sort:
            payload["sort"] = sort
        if sort_dir:
            payload["sort_dir"] = sort_dir
        if after is not None:
            payload["after"] = after
        if before is not None:
            payload["before"] = before
        if disable_semantic_search:
            payload["disable_semantic_search"] = True

        try:
            response = self.client.api_call(
                api_method="assistant.search.context",
                json=payload,
            )
        except SlackApiError as exc:
            error = exc.response.get("error", str(exc))
            raise SlackSearchError(error, exc.response) from exc

        data = response.data if hasattr(response, "data") else dict(response)
        if not data.get("ok"):
            raise SlackSearchError(data.get("error", "unknown_error"), data)

        return data

    def search_users(
        self,
        *,
        query: str,
        action_token: str,
        limit: int = 5,
        context_channel_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search workspace users — used for entity disambiguation."""
        data = self.search_context(
            query=query,
            action_token=action_token,
            content_types=["users"],
            include_context_messages=False,
            limit=limit,
            context_channel_id=context_channel_id,
        )
        return data.get("results", {}).get("users", [])

    def search_knowledge(
        self,
        *,
        query: str,
        action_token: str,
        content_types: list[ContentType] | None = None,
        context_channel_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search messages/files/channels with surrounding conversation context."""
        return self.search_context(
            query=query,
            action_token=action_token,
            content_types=content_types or ["messages", "files", "channels"],
            include_context_messages=True,
            limit=limit,
            context_channel_id=context_channel_id,
        )
