"""AY Inbox tool spec: every agent tool a credential may call, as LlamaIndex `FunctionTool`s."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar

import httpx
from llama_index.core.tools import FunctionTool, ToolMetadata
from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from pydantic import BaseModel, ConfigDict, create_model

from ._errors import AyInboxAPIError, AyInboxError
from ._http import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, AyInboxToolsClient, ToolDefinition

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class AyInboxToolError(AyInboxError):
    """A tool call the API refused or could not answer.

    The message names the API's `code` and `request_id`; LlamaIndex agents hand it to the model as an
    error tool output. `status`, `code`, `request_id` and `reason` are `None` for a network failure.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.reason = reason

    @classmethod
    def from_error(cls, error: AyInboxError) -> AyInboxToolError:
        if isinstance(error, AyInboxAPIError):
            return cls(
                str(error), status=error.status, code=error.code, request_id=error.request_id, reason=error.reason
            )
        return cls(str(error))


class JSONSchemaArgs(BaseModel):
    """Argument model whose JSON schema is the API's own `input_schema`, passed to the LLM unchanged."""

    model_config = ConfigDict(extra="allow")
    source_schema: ClassVar[dict[str, Any]] = _EMPTY_SCHEMA

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(cls.source_schema)


def schema_model(name: str, schema: Any) -> type[JSONSchemaArgs]:
    """Build an argument model for one tool from its JSON Schema."""
    if not isinstance(schema, dict):
        schema = dict(_EMPTY_SCHEMA)
    elif "properties" not in schema:
        schema = {**schema, "properties": {}}
    model = create_model(f"{name}_args", __base__=JSONSchemaArgs)
    model.source_schema = schema
    return model


def _result_content(data: dict[str, Any]) -> str:
    text = data.get("result_text")
    if isinstance(text, str):
        return text
    result = data.get("result")
    return result if isinstance(result, str) else json.dumps(result)


class AyInboxToolSpec(BaseToolSpec):
    """AY Inbox agent tools for LlamaIndex.

    The catalogue comes from `GET /ai/tools`, fetched once on the first `to_tool_list()` or
    `to_tool_list_async()`. It holds only the tools the credential's scopes allow, and never a tool that
    sends mail. Each tool's output content is the API's `result_text`, which fences text written by people
    outside the workspace as data; `ToolOutput.raw_output` is the whole `data` object.

    Example:
        ```python
        from llama_index.tools.ayinbox import AyInboxToolSpec

        tools = AyInboxToolSpec(toolsets=["inbox"]).to_tool_list()  # reads AYINBOX_API_KEY
        ```
    """

    # Tools come from the API, not from methods on this class.
    spec_functions: list[SPEC_FUNCTION_TYPE] = []

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        toolsets: Sequence[str] | None = None,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
        async_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client = AyInboxToolsClient(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
            async_http_client=async_http_client,
        )
        self.toolsets = list(toolsets) if toolsets else None
        self.include = list(include) if include is not None else None
        self.exclude = list(exclude) if exclude else []
        self._definitions: list[ToolDefinition] | None = None

    def __repr__(self) -> str:
        return f"AyInboxToolSpec(base_url={self.client.base_url!r}, toolsets={self.toolsets!r})"

    def to_tool_list(
        self,
        spec_functions: list[SPEC_FUNCTION_TYPE] | None = None,
        func_to_metadata_mapping: dict[str, ToolMetadata] | None = None,
    ) -> list[FunctionTool]:
        """Return the tools, fetching the catalogue on the first call."""
        if self._definitions is None:
            self._definitions = self._filter(self.client.list_tools(self.toolsets))
        return [self._tool(d) for d in self._definitions]

    async def to_tool_list_async(
        self,
        spec_functions: list[SPEC_FUNCTION_TYPE] | None = None,
        func_to_metadata_mapping: dict[str, ToolMetadata] | None = None,
    ) -> list[FunctionTool]:
        """Async variant of `to_tool_list`."""
        if self._definitions is None:
            self._definitions = self._filter(await self.client.alist_tools(self.toolsets))
        return [self._tool(d) for d in self._definitions]

    def _filter(self, definitions: Sequence[ToolDefinition]) -> list[ToolDefinition]:
        names = [d["name"] for d in definitions]
        if self.include is not None:
            missing = sorted(set(self.include) - set(names))
            if missing:
                raise ValueError(
                    "These tools are not offered to this credential (check its scopes and toolsets): "
                    + ", ".join(missing)
                )
        keep = set(self.include) if self.include is not None else set(names)
        keep -= set(self.exclude)
        return [d for d in definitions if d["name"] in keep]

    def _tool(self, definition: ToolDefinition) -> FunctionTool:
        name: str = definition["name"]
        metadata = ToolMetadata(
            name=name,
            description=definition.get("description") or f"AY Inbox tool {name}.",
            fn_schema=schema_model(name, definition.get("input_schema")),
        )
        return FunctionTool.from_defaults(
            fn=self._sync_fn(name),
            async_fn=self._async_fn(name),
            tool_metadata=metadata,
            callback=_result_content,
            async_callback=self._async_content,
        )

    @staticmethod
    async def _async_content(data: dict[str, Any]) -> str:
        return _result_content(data)

    def _sync_fn(self, name: str) -> Callable[..., dict[str, Any]]:
        client = self.client

        def call(**kwargs: Any) -> dict[str, Any]:
            try:
                return client.call_tool(name, kwargs)
            except AyInboxError as error:
                raise AyInboxToolError.from_error(error) from error

        call.__name__ = name
        return call

    def _async_fn(self, name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
        client = self.client

        async def acall(**kwargs: Any) -> dict[str, Any]:
            try:
                return await client.acall_tool(name, kwargs)
            except AyInboxError as error:
                raise AyInboxToolError.from_error(error) from error

        acall.__name__ = name
        return acall
