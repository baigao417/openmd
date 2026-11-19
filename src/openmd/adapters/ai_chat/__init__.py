"""Conversation identity resolution; response anchors never change identity."""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from openmd.adapters.opencli import OpenCliPlatformAdapter
from openmd.backends.ai_chat import conversation_id_for_url


class AiChatAdapter(OpenCliPlatformAdapter):
    def resolve_items(self, input_value: str, *, limit: int | None = None, all: bool = False):
        items = super().resolve_items(input_value, limit=limit, all=all)
        resolved = []
        for item in items:
            if item.raw_meta.get("chat_site") != "grok":
                resolved.append(item)
                continue
            conversation_id = conversation_id_for_url("grok", item.url)
            if not conversation_id:
                resolved.append(item)
                continue
            anchor = parse_qs(urlsplit(item.url).query).get("rid", [None])[0]
            metadata = {**item.raw_meta}
            if anchor:
                metadata["anchor_response_id"] = anchor
            resolved.append(replace(item, id=conversation_id,
                                    url=f"https://grok.com/c/{conversation_id}", raw_meta=metadata))
        return resolved


def create_adapter(platform_id: str, default_backend: str):
    return AiChatAdapter(platform_id, default_backend)
