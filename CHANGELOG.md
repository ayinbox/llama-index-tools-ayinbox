# Changelog

## 0.1.0 (unreleased)

- `AyInboxToolSpec`: builds LlamaIndex `FunctionTool`s from `GET /v1/ai/tools`, with `toolsets`, `include` and `exclude` filters and sync and async loading.
- Each tool runs `POST /v1/ai/tools/{name}/call` on the sync and async paths, returns the API's fenced `result_text` as its content and the whole `data` object as `raw_output`, and passes the API's JSON Schema to the LLM unchanged.
- API errors become `AyInboxToolError`, carrying the API's `code` and `request_id`.
- Retries on `429`, `502`, `503` and `504` honouring `Retry-After`; a tool call is retried on a server error only when it carries an `idempotency_key`.
