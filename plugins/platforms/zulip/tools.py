"""Client tools for CLI/TUI sessions (#78050).

The platform adapter stays deferred in CLI/TUI processes, so discovery imports
this module (the manifest's ``provides_tools`` asks for it) and calls
``register_tools(ctx)`` to make the zulip tools available there. The gateway
path registers the same tools through the package ``register()``.
"""

from __future__ import annotations

from .attachments import register_zulip_attachment_tool
from .search_tool import register_zulip_search_tool
from .topic_tool import register_zulip_topic_message_tool


def register_tools(ctx) -> None:
    register_zulip_search_tool(ctx)
    register_zulip_attachment_tool(ctx)
    register_zulip_topic_message_tool(ctx)
