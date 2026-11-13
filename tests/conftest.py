from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
API_KEY = "aybox_test_key_0123456789abcdefghijklmnopqrstuvwxyzABCDE"
BASE = "https://api.ayinbox.com/v1"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


Handler = Callable[[httpx.Request], httpx.Response]


class MockAPI:
    """Serves queued responses and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[tuple[str, str], list[httpx.Response]] = {}

    def add(self, method: str, path: str, *responses: httpx.Response) -> None:
        self.responses.setdefault((method, path), []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.responses.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(404, json=fixture("errors/404_not_found.json"))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sync_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def api() -> MockAPI:
    mock = MockAPI()
    mock.add("GET", "/v1/ai/tools", json_response(200, fixture("ai_tools_list.json")))
    mock.add("POST", "/v1/ai/tools/list_campaigns/call", json_response(200, fixture("ai_tool_call.json")))
    return mock


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AYINBOX_API_KEY", raising=False)
    monkeypatch.delenv("AYINBOX_BASE_URL", raising=False)
