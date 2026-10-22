"""A fake Claude API for tests: the real SDK client over a mock HTTP transport.

Requests go through the SDK exactly as in production (schema, parsing, error mapping), but
responses come from the test or from a recorded fixture, and nothing leaves the machine.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
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


Response = dict | httpx2.Response | Exception


class FakeClaude:
    """Answers each request with the next queued response, or with `route(request)` when given,
    and keeps the requests sent (thread-safe enough for the pipeline's parallel reads)."""

    def __init__(self, *responses: Response, route: Callable[[dict], Response] | None = None):
        self.responses = list(responses)
        self.route = route
        self.requests: list[dict] = []
        self.headers: list[dict[str, str]] = []
        self.client = anthropic.Anthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self._handle)),
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        item = self.route(body) if self.route else self.responses.pop(0)
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


def document_of(body: dict) -> bytes:
    """The PDF a request sent."""
    return base64.standard_b64decode(body["messages"][0]["content"][0]["source"]["data"])


def by_document(answers: dict[Path, dict[str, Response]]) -> Callable[[dict], Response]:
    """Route requests by the PDF sent and the model asked: {pdf path: {model: response}}."""
    table = {path.read_bytes(): by_model for path, by_model in answers.items()}

    def route(body: dict) -> Response:
        by_model = table[document_of(body)]
        return by_model[body["model"]]

    return route


def models_asked(fake: FakeClaude, pdf: Path) -> list[str]:
    data = pdf.read_bytes()
    return [b["model"] for b in fake.requests if document_of(b) == data]


def recorded(name: str) -> dict:
    """A response body recorded from the live API by scripts/record_fixtures.py."""
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]
