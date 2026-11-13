"""Errors raised by the AY Inbox HTTP client."""

from __future__ import annotations

from typing import Any


class AyInboxError(Exception):
    """Base class for every error this package raises."""


class AyInboxAPIError(AyInboxError):
    """The API answered with an error status.

    `code` is the API's stable machine key; branch on it and on `status`, never on `message`.
    """

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        reason: str | None = None,
        retry_after: float | None = None,
        body: Any = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.reason = reason
        self.retry_after = retry_after
        self.body = body
        super().__init__(self._describe())

    def _describe(self) -> str:
        code = f"{self.code} ({self.reason})" if self.reason else self.code
        text = f"AY Inbox API error {self.status} {code}: {self.message}"
        if self.request_id:
            text += f" [request_id: {self.request_id}]"
        return text

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, "
            f"message={self.message!r}, request_id={self.request_id!r})"
        )


class AyInboxConnectionError(AyInboxError):
    """The request did not get an answer: DNS, TLS, connection or timeout failure."""
