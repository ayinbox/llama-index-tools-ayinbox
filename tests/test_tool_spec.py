from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from llama_index.core.tools import FunctionTool
from llama_index.core.tools.calling import acall_tool, call_tool
from llama_index.core.tools.tool_spec.base import BaseToolSpec

from llama_index.tools.ayinbox import (
    AyInboxAPIError,
    AyInboxError,
    AyInboxToolError,
    AyInboxToolSpec,
    __version__,
)

from .conftest import API_KEY, MockAPI, fixture, json_response

CATALOGUE = {
    "data": [
        {"name": "list_threads", "description": "List threads.", "input_schema": {"type": "object"}},
        {
            "name": "list_campaigns",
            "description": "List the workspace's campaigns.",
            "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "add_tag",
            "description": "Tag a contact.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "Contact UUID."},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["contact_id"],
            },
        },
    ]
}


def make_spec(api: MockAPI, **kwargs: Any) -> AyInboxToolSpec:
    spec = AyInboxToolSpec(API_KEY, http_client=api.sync_client(), async_http_client=api.async_client(), **kwargs)
    spec.client._sleep = lambda _seconds: None

    async def no_sleep(_seconds: float) -> None:
        return None

    spec.client._asleep = no_sleep
    return spec


def only_tool(api: MockAPI) -> FunctionTool:
    tools = make_spec(api).to_tool_list()
    assert len(tools) == 1
    return tools[0]


def test_is_a_tool_spec(api: MockAPI) -> None:
    assert isinstance(make_spec(api), BaseToolSpec)


def test_tool_list_is_converted(api: MockAPI) -> None:
    tool = only_tool(api)
    assert tool.metadata.name == "list_campaigns"
    assert tool.metadata.description == "List the workspace's campaigns."
    assert tool.metadata.to_openai_tool() == {
        "type": "function",
        "function": {
            "name": "list_campaigns",
            "description": "List the workspace's campaigns.",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
    }


def test_schema_is_passed_through_unchanged() -> None:
    api = MockAPI()
    api.add("GET", "/v1/ai/tools", json_response(200, CATALOGUE))
    tools = {t.metadata.name: t for t in make_spec(api).to_tool_list()}
    assert tools["add_tag"].metadata.get_parameters_dict() == CATALOGUE["data"][2]["input_schema"]
    assert tools["list_threads"].metadata.get_parameters_dict() == {"type": "object", "properties": {}}


def test_list_request_headers_and_query(api: MockAPI) -> None:
    make_spec(api, toolsets=["inbox", "campaigns"]).to_tool_list()
    request = api.requests[0]
    assert str(request.url) == "https://api.ayinbox.com/v1/ai/tools?format=ayinbox&toolsets=inbox%2Ccampaigns"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["user-agent"] == f"llama-index-tools-ayinbox/{__version__}"
    assert request.headers["accept"] == "application/json"


def test_catalogue_is_fetched_once(api: MockAPI) -> None:
    spec = make_spec(api)
    spec.to_tool_list()
    spec.to_tool_list()
    assert len(api.requests) == 1


def test_sync_call_round_trip(api: MockAPI) -> None:
    output = only_tool(api).call(limit=5)
    assert output.content == '{"campaigns":[]}'
    assert output.raw_output == fixture("ai_tool_call.json")["data"]
    assert not output.is_error
    request = api.requests[-1]
    assert request.method == "POST"
    assert request.url.path == "/v1/ai/tools/list_campaigns/call"
    assert json.loads(request.content) == {"limit": 5}
    assert request.headers["content-type"] == "application/json"


def test_call_through_llama_index_dispatch(api: MockAPI) -> None:
    output = call_tool(only_tool(api), {"limit": 2})
    assert output.content == '{"campaigns":[]}'
    assert json.loads(api.requests[-1].content) == {"limit": 2}


async def test_async_call_round_trip(api: MockAPI) -> None:
    tools = await make_spec(api).to_tool_list_async()
    output = await tools[0].acall(limit=5)
    assert output.content == '{"campaigns":[]}'
    assert output.raw_output["result"] == {"campaigns": []}
    assert json.loads(api.requests[-1].content) == {"limit": 5}


@pytest.mark.parametrize(
    ("fixture_name", "status", "code"),
    [
        ("errors/403_forbidden.json", 403, "forbidden"),
        ("errors/404_not_found.json", 404, "not_found"),
        ("errors/422_unprocessable.json", 422, "unprocessable"),
        ("errors/402_insufficient_credits.json", 402, "insufficient_credits"),
        ("errors/409_idempotency_outcome_unknown.json", 409, "idempotency_outcome_unknown"),
        ("errors/500_internal_error.json", 500, "internal_error"),
    ],
)
def test_api_error_becomes_tool_error(api: MockAPI, fixture_name: str, status: int, code: str) -> None:
    api.responses[("POST", "/v1/ai/tools/list_campaigns/call")] = [json_response(status, fixture(fixture_name))]
    tool = only_tool(api)
    with pytest.raises(AyInboxToolError) as info:
        tool.call(limit=1)
    assert info.value.status == status
    assert info.value.code == code
    assert info.value.request_id == "req-fixture-0001"
    output = call_tool(tool, {"limit": 1})
    assert output.is_error
    assert f"{status} {code}" in output.content
    assert "req-fixture-0001" in output.content


async def test_async_api_error_becomes_tool_error(api: MockAPI) -> None:
    api.responses[("POST", "/v1/ai/tools/list_campaigns/call")] = [
        json_response(403, fixture("errors/403_api_key_mailbox_limited.json"))
    ]
    tools = await make_spec(api).to_tool_list_async()
    output = await acall_tool(tools[0], {"limit": 1})
    assert output.is_error
    assert "api_key_mailbox_limited" in output.content and "req-fixture-0001" in output.content


def test_category_rate_limit_without_code(api: MockAPI) -> None:
    api.responses[("POST", "/v1/ai/tools/list_campaigns/call")] = [
        json_response(429, fixture("errors/429_category_rate_limited.json"), {"X-Request-Id": "hdr-req-1"})
    ]
    spec = make_spec(api, max_retries=0)
    with pytest.raises(AyInboxAPIError) as info:
        spec.client.call_tool("list_campaigns", {})
    assert info.value.code == "rate_limit_exceeded"
    assert info.value.request_id == "hdr-req-1"
    assert info.value.retry_after == 12


def test_list_error_raises_api_error(api: MockAPI) -> None:
    api.responses[("GET", "/v1/ai/tools")] = [json_response(401, fixture("errors/401_api_key_holder_left.json"))]
    with pytest.raises(AyInboxAPIError) as info:
        make_spec(api).to_tool_list()
    assert info.value.code == "api_key_holder_left"


def test_get_is_retried_on_503_then_succeeds(api: MockAPI) -> None:
    api.responses[("GET", "/v1/ai/tools")] = [
        json_response(503, fixture("errors/503_service_unavailable.json"), {"Retry-After": "0"}),
        json_response(200, fixture("ai_tools_list.json")),
    ]
    assert len(make_spec(api).to_tool_list()) == 1
    assert len(api.requests) == 2


def test_tool_call_without_idempotency_key_is_not_retried_on_503(api: MockAPI) -> None:
    api.responses[("POST", "/v1/ai/tools/list_campaigns/call")] = [
        json_response(503, fixture("errors/503_service_unavailable.json")),
        json_response(200, fixture("ai_tool_call.json")),
    ]
    with pytest.raises(AyInboxAPIError):
        make_spec(api).client.call_tool("list_campaigns", {"limit": 1})
    assert len(api.requests) == 1


async def test_tool_call_with_idempotency_key_is_retried_on_503(api: MockAPI) -> None:
    api.responses[("POST", "/v1/ai/tools/list_campaigns/call")] = [
        json_response(503, fixture("errors/503_service_unavailable.json")),
        json_response(200, fixture("ai_tool_call.json")),
    ]
    data = await make_spec(api).client.acall_tool("list_campaigns", {"idempotency_key": "k-1"})
    assert data["name"] == "list_campaigns"
    assert len(api.requests) == 2


def test_tool_call_is_retried_on_429(api: MockAPI) -> None:
    api.responses[("POST", "/v1/ai/tools/list_campaigns/call")] = [
        json_response(429, fixture("errors/429_api_key_rate_limited.json"), {"Retry-After": "0"}),
        json_response(200, fixture("ai_tool_call.json")),
    ]
    assert make_spec(api).client.call_tool("list_campaigns")["name"] == "list_campaigns"
    assert len(api.requests) == 2


def test_network_failure_becomes_tool_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    api = MockAPI()
    api.add("GET", "/v1/ai/tools", json_response(200, fixture("ai_tools_list.json")))
    spec = AyInboxToolSpec(
        API_KEY,
        http_client=api.sync_client(),
        async_http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    )
    tool = spec.to_tool_list()[0]
    spec.client._http_client = httpx.Client(transport=httpx.MockTransport(fail))
    output = call_tool(tool, {"limit": 1})
    assert output.is_error
    assert "Could not reach AY Inbox" in output.content
    assert "may or may not have run" in output.content


def test_include_and_exclude() -> None:
    api = MockAPI()
    api.add("GET", "/v1/ai/tools", json_response(200, CATALOGUE))

    def names(**kwargs: Any) -> list[str]:
        return [str(t.metadata.name) for t in make_spec(api, **kwargs).to_tool_list()]

    assert names(include=["add_tag", "list_threads"]) == ["list_threads", "add_tag"]
    assert names(exclude=["add_tag"]) == ["list_threads", "list_campaigns"]
    assert names(include=["add_tag"], exclude=["add_tag"]) == []


def test_include_of_a_tool_not_offered_raises() -> None:
    api = MockAPI()
    api.add("GET", "/v1/ai/tools", json_response(200, CATALOGUE))
    with pytest.raises(ValueError, match="send_reply"):
        make_spec(api, include=["send_reply"]).to_tool_list()


def test_env_fallbacks(api: MockAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AYINBOX_API_KEY", API_KEY)
    monkeypatch.setenv("AYINBOX_BASE_URL", "https://example.test/v1/")
    spec = AyInboxToolSpec(http_client=api.sync_client())
    assert spec.client.base_url == "https://example.test/v1"
    spec.to_tool_list()
    assert api.requests[0].url.host == "example.test"
    assert api.requests[0].headers["authorization"] == f"Bearer {API_KEY}"


def test_missing_key_raises() -> None:
    with pytest.raises(AyInboxError, match="AYINBOX_API_KEY"):
        AyInboxToolSpec()


def test_key_is_never_leaked(api: MockAPI) -> None:
    echo = {"error": "Bad Request", "message": f"bad token {API_KEY}", "code": "bad_request", "request_id": "r1"}
    api.responses[("POST", "/v1/ai/tools/list_campaigns/call")] = [json_response(400, echo)]
    spec = make_spec(api)
    tool = spec.to_tool_list()[0]
    output = call_tool(tool, {"limit": 1})
    surfaces = [
        repr(spec),
        str(spec),
        repr(spec.client),
        repr(tool),
        repr(tool.metadata),
        tool.metadata.fn_schema_str,
        output.content,
        repr(output),
        str(vars(spec)),
    ]
    for surface in surfaces:
        assert API_KEY not in surface
    with pytest.raises(AyInboxAPIError) as info:
        spec.client.call_tool("list_campaigns")
    assert API_KEY not in str(info.value)
    assert API_KEY not in repr(info.value)
    assert API_KEY not in json.dumps(info.value.body)
