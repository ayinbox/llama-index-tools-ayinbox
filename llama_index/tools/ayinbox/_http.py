"""A small HTTP client for the two AY Inbox agent-tool endpoints."""

from __future__ import annotations

import asyncio
import email.utils
import json
import math
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from ._errors import AyInboxAPIError, AyInboxConnectionError, AyInboxError
from ._version import __version__

DEFAULT_BASE_URL = "https://api.ayinbox.com/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
USER_AGENT = f"llama-index-tools-ayinbox/{__version__}"

_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_AFTER_CAP = 120.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0
_CODE_PATTERN = re.compile(r"^[a-z0-9_]+$")
_DEFAULT_CODES = {
    400: "bad_request",
    401: "unauthorized",
    402: "insufficient_credits",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "unprocessable",
    429: "rate_limit_exceeded",
    500: "internal_error",
    501: "not_implemented",
    503: "service_unavailable",
}
# Exceptions raised before any byte of the request reached the server.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)

ToolDefinition = dict[str, Any]


def _default_code(status: int) -> str:
    if status in _DEFAULT_CODES:
        return _DEFAULT_CODES[status]
    return "internal_error" if status >= 500 else "error"


def _parse_retry_after(response: httpx.Response, body: Any) -> float | None:
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
        try:
            parsed = email.utils.parsedate_to_datetime(header)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            return max(0.0, parsed.timestamp() - time.time())
    if isinstance(body, dict):
        if isinstance(body.get("retry_after"), (int, float)):
            return float(body["retry_after"])
        if isinstance(body.get("retry_after_ms"), (int, float)):
            return float(math.ceil(body["retry_after_ms"] / 1000))
    return None


class AyInboxToolsClient:
    """Lists and calls AY Inbox agent tools over `GET /ai/tools` and `POST /ai/tools/{name}/call`.

    The API key falls back to `AYINBOX_API_KEY` and the base URL to `AYINBOX_BASE_URL`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
        async_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        key = api_key or os.environ.get("AYINBOX_API_KEY")
        if not key:
            raise AyInboxError("No AY Inbox API key: pass api_key or set AYINBOX_API_KEY.")
        self.__api_key = key
        self.base_url = (base_url or os.environ.get("AYINBOX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._http_client = http_client
        self._async_http_client = async_http_client
        self._owns_sync = False
        self._owns_async = False
        self._sleep: Callable[[float], None] = time.sleep
        self._asleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def __repr__(self) -> str:
        return f"AyInboxToolsClient(base_url={self.base_url!r}, api_key='***')"

    # -- public API ---------------------------------------------------------

    def list_tools(self, toolsets: Sequence[str] | None = None) -> list[ToolDefinition]:
        """Return the tool definitions the credential may call."""
        body = self._request("GET", "/ai/tools", params=self._list_params(toolsets), retry_safe=True)
        return self._tool_definitions(body)

    async def alist_tools(self, toolsets: Sequence[str] | None = None) -> list[ToolDefinition]:
        """Async variant of `list_tools`."""
        body = await self._arequest("GET", "/ai/tools", params=self._list_params(toolsets), retry_safe=True)
        return self._tool_definitions(body)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one tool and return the API's `data` object (`name`, `result`, `result_text`, `untrusted`)."""
        args = dict(arguments or {})
        body = self._request("POST", self._call_path(name), json_body=args, retry_safe=self._call_is_retry_safe(args))
        return self._call_data(body)

    async def acall_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Async variant of `call_tool`."""
        args = dict(arguments or {})
        body = await self._arequest(
            "POST", self._call_path(name), json_body=args, retry_safe=self._call_is_retry_safe(args)
        )
        return self._call_data(body)

    def close(self) -> None:
        """Close the HTTP clients this object created (injected clients are left open)."""
        if self._http_client is not None and self._owns_sync:
            self._http_client.close()
            self._http_client = None

    async def aclose(self) -> None:
        """Close the async HTTP client this object created."""
        if self._async_http_client is not None and self._owns_async:
            await self._async_http_client.aclose()
            self._async_http_client = None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _list_params(toolsets: Sequence[str] | None) -> dict[str, str]:
        params = {"format": "ayinbox"}
        if toolsets:
            params["toolsets"] = ",".join(toolsets)
        return params

    @staticmethod
    def _call_path(name: str) -> str:
        return f"/ai/tools/{quote(name, safe='')}/call"

    @staticmethod
    def _call_is_retry_safe(args: Mapping[str, Any]) -> bool:
        # A tool call is a POST: the server only dedupes a retry when the call carries an idempotency key.
        key = args.get("idempotency_key")
        return isinstance(key, str) and key != ""

    @staticmethod
    def _tool_definitions(body: Any) -> list[ToolDefinition]:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise AyInboxError("Unexpected response from GET /ai/tools: no data array.")
        return [item for item in data if isinstance(item, dict) and isinstance(item.get("name"), str)]

    @staticmethod
    def _call_data(body: Any) -> dict[str, Any]:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise AyInboxError("Unexpected response from POST /ai/tools/{name}/call: no data object.")
        return data

    def _headers(self, has_body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.__api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _redact(self, text: str) -> str:
        return text.replace(self.__api_key, "***") if self.__api_key in text else text

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        return value

    def _sync_client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self.timeout)
            self._owns_sync = True
        return self._http_client

    def _async_client(self) -> httpx.AsyncClient:
        if self._async_http_client is None:
            self._async_http_client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_async = True
        return self._async_http_client

    def _build(
        self, client: httpx.Client | httpx.AsyncClient, method: str, path: str, params: Any, json_body: Any
    ) -> httpx.Request:
        content = None if json_body is None else json.dumps(json_body).encode()
        return client.build_request(
            method,
            self.base_url + path,
            params=params,
            content=content,
            headers=self._headers(content is not None),
            timeout=self.timeout,
        )

    def _error_from(self, response: httpx.Response) -> AyInboxAPIError:
        text = response.text
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        status = response.status_code
        if isinstance(body, dict):
            code = body.get("code")
            error = body.get("error")
            if not isinstance(code, str) or not code:
                code = error if isinstance(error, str) and _CODE_PATTERN.match(error) else _default_code(status)
            message = body.get("message")
            if not isinstance(message, str) or not message:
                message = error if isinstance(error, str) and error else response.reason_phrase
            request_id = body.get("request_id") or response.headers.get("x-request-id")
            reason = body.get("reason") if isinstance(body.get("reason"), str) else None
        else:
            code = _default_code(status)
            message = text.strip()[:500] or response.reason_phrase
            request_id = response.headers.get("x-request-id")
            reason = None
        return AyInboxAPIError(
            status=status,
            code=code,
            message=self._redact(message),
            request_id=request_id if isinstance(request_id, str) else None,
            reason=reason,
            retry_after=_parse_retry_after(response, body),
            body=self._redact_value(body) if body is not None else self._redact(text),
        )

    def _wait(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, _RETRY_AFTER_CAP)
        return random.uniform(0, min(_BACKOFF_CAP, _BACKOFF_BASE * (2**attempt)))

    def _decide(self, attempt: int, retry_safe: bool, response: httpx.Response | None, exc: Exception | None) -> bool:
        if attempt >= self.max_retries:
            return False
        if exc is not None:
            return retry_safe or isinstance(exc, _NOT_SENT)
        assert response is not None
        if response.status_code == 429:
            return True  # a refused 429 never ran the request
        return retry_safe and response.status_code in _RETRY_STATUSES

    def _connection_error(self, exc: Exception, method: str) -> AyInboxConnectionError:
        text = f"Could not reach AY Inbox ({type(exc).__name__}: {self._redact(str(exc))})."
        if method != "GET" and not isinstance(exc, _NOT_SENT):
            text += " The call may or may not have run; check before repeating it."
        return AyInboxConnectionError(text)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def _request(self, method: str, path: str, *, params: Any = None, json_body: Any = None, retry_safe: bool) -> Any:
        client = self._sync_client()
        attempt = 0
        while True:
            request = self._build(client, method, path, params, json_body)
            try:
                response = client.send(request)
            except httpx.HTTPError as exc:
                if self._decide(attempt, retry_safe, None, exc):
                    self._sleep(self._wait(attempt, None))
                    attempt += 1
                    continue
                raise self._connection_error(exc, method) from None
            if response.is_success:
                return self._decode(response)
            error = self._error_from(response)
            if self._decide(attempt, retry_safe, response, None):
                self._sleep(self._wait(attempt, error.retry_after))
                attempt += 1
                continue
            raise error

    async def _arequest(
        self, method: str, path: str, *, params: Any = None, json_body: Any = None, retry_safe: bool
    ) -> Any:
        client = self._async_client()
        attempt = 0
        while True:
            request = self._build(client, method, path, params, json_body)
            try:
                response = await client.send(request)
            except httpx.HTTPError as exc:
                if self._decide(attempt, retry_safe, None, exc):
                    await self._asleep(self._wait(attempt, None))
                    attempt += 1
                    continue
                raise self._connection_error(exc, method) from None
            if response.is_success:
                return self._decode(response)
            error = self._error_from(response)
            if self._decide(attempt, retry_safe, response, None):
                await self._asleep(self._wait(attempt, error.retry_after))
                attempt += 1
                continue
            raise error
