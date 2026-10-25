"""Single entry point for Claude calls; records token usage against the active cost_run."""
import anthropic

import config
import cost_tracker

_client = None


def _get_client():
    """The shared Anthropic client, created on first use (sending the workspace id header if set)."""
    global _client
    if _client is None:
        config.require_keys("ANTHROPIC_API_KEY")
        # Keys that are not scoped to a workspace need the workspace id sent as a header.
        workspace_id = config.get("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        _client = anthropic.Anthropic(default_headers=headers)
    return _client


def create_message(step, **kwargs):
    """Wrapper over messages.create. `step` is the cost label (tagging, brief_generation, ...)."""
    kwargs.setdefault("model", config.ANTHROPIC_MODEL)
    kwargs.setdefault("max_tokens", 4096)
    response = _get_client().messages.create(**kwargs)
    cost_tracker.record_claude(step, kwargs["model"], response.usage)
    return response


def tool_input(response, tool_name):
    """Return the input dict of the named tool_use block, or None if the model did not call it."""
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    return None


def text_of(response):
    """The concatenated text blocks of a response."""
    return "".join(b.text for b in response.content if b.type == "text").strip()
