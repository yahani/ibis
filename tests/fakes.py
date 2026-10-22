"""A fake Claude API for tests: the real SDK client over a mock HTTP transport.

Requests go through the SDK exactly as in production (schema, parsing, error mapping), but
responses come from the test or from a recorded fixture, and nothing leaves the machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
import httpx2

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def message(
    reply: dict | str,
    *,
    model: str = "claude-haiku-5-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 4000,
    output_tokens: int = 500,
    cache_read: int | None = None,
) -> dict:
    """A Messages API response body whose text block is `reply` (a dict is sent as JSON)."""
    text = reply if isinstance(reply, str) else json.dumps(reply)
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
        },
    }


class FakeClaude:
    """Answers each request with the next queued response, and keeps the requests sent."""

    def __init__(self, *responses: dict | httpx2.Response | Exception):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.client = anthropic.Anthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self._handle)),
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx2.Response):
            return item
        return httpx2.Response(200, json=item)


def error_response(status: int, message: str = "test error") -> httpx2.Response:
    kind = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
    }.get(status, "api_error")
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": kind, "message": message}}
    )


def recorded(name: str) -> dict:
    """A response body recorded from the live API by scripts/record_fixtures.py."""
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]
