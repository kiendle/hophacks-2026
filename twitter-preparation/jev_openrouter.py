"""OpenRouter transport and Jev response adapters.

The Jev protocol uses a canonical ``jev-1.13.0`` model identifier.  OpenRouter
accepts the pinned ``typesafe/jev-1.13`` identifier and reports the resolved
version in its response.  This module keeps that translation at the provider
boundary: callers retain their canonical request and the exact provider body
returned by OpenRouter, while protocol validation/classification operate on
short-lived copies with the canonical identifier restored.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from numbers import Real
from typing import Any

import aiohttp
import httpx

from build_jev_request import MODEL as CANONICAL_MODEL
from jev_protocol import classify, validate_response


OPENROUTER_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "typesafe/jev-1.13"
OPENROUTER_RESOLVED_MODEL = "typesafe/jev-1.13-20260917"

# Direct TypeSafe pricing is $0.042 per million input tokens; output is free.
INPUT_PRICE_PER_TOKEN = 0.042 / 1_000_000.0


_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _response_payload(response: Mapping[str, Any] | httpx.Response) -> Mapping[str, Any]:
    """Return a response mapping without modifying the source object."""
    if isinstance(response, httpx.Response):
        try:
            payload = response.json()
        except Exception as error:
            raise ValueError("response body is not valid JSON") from error
    else:
        payload = response
    if not isinstance(payload, Mapping):
        raise ValueError("response must be an object")
    return payload


def _is_typesafe_provider(response: Mapping[str, Any]) -> bool:
    return response.get("provider") == "TypeSafe"


def _mapped_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a canonical request and map only its top-level model field."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    if request.get("model") != CANONICAL_MODEL:
        raise ValueError(f"request.model must be {CANONICAL_MODEL}")
    copied = dict(request)
    copied["model"] = OPENROUTER_MODEL
    return copied


def _canonical_request_copy(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    if request.get("model") != CANONICAL_MODEL:
        raise ValueError(f"request.model must be {CANONICAL_MODEL}")
    return dict(request)




class OpenRouterClient:
    """Small aiohttp client exposing the engine's httpx response contract."""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float,
        concurrency: int,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a nonempty string")
        try:
            timeout_value = float(timeout_seconds)
        except (OverflowError, TypeError, ValueError):
            timeout_value = math.nan
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, Real)
            or not math.isfinite(timeout_value)
            or timeout_value <= 0.0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency <= 0
        ):
            raise ValueError("concurrency must be a positive integer")

        self.api_key = api_key
        self.timeout_seconds = timeout_value
        self.concurrency = concurrency
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "OpenRouterClient":
        if self._session is not None:
            raise RuntimeError("OpenRouterClient is already open")
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        connector = aiohttp.TCPConnector(
            limit=self.concurrency,
            limit_per_host=self.concurrency,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            auto_decompress=False,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                **_JSON_HEADERS,
            },
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
    ) -> httpx.Response:
        """POST a canonical Jev payload and adapt aiohttp's response to httpx."""
        if self._session is None:
            raise RuntimeError("OpenRouterClient must be used as an async context manager")
        outbound = _mapped_request(json)
        try:
            body = json_module_dumps(outbound)
        except (TypeError, ValueError) as error:
            raise ValueError(f"request is not canonicalizable JSON: {error}") from error

        # Pass bytes rather than aiohttp's json= helper so the attached httpx
        # request exactly describes the bytes sent over the wire.
        request_headers = {
            "Authorization": f"Bearer {self.api_key}",
            **_JSON_HEADERS,
        }
        request = httpx.Request(
            "POST",
            url,
            headers=request_headers,
            content=body,
        )
        async with self._session.post(url, data=body, headers=_JSON_HEADERS) as upstream:
            raw_body = await upstream.read()
            raw_headers = [
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in upstream.raw_headers
            ]
            extensions = {
                "http_version": f"HTTP/{upstream.version.major}.{upstream.version.minor}".encode(
                    "ascii"
                ),
                "reason_phrase": upstream.reason.encode("latin-1")
                if upstream.reason
                else b"",
            }
            return httpx.Response(
                int(upstream.status),
                headers=raw_headers,
                content=raw_body,
                request=request,
                extensions=extensions,
            )


def json_module_dumps(payload: Mapping[str, Any]) -> bytes:
    """Serialize JSON with the compact UTF-8 encoding used by httpx."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def validate_jev_response(
    request: Mapping[str, Any],
    response: Mapping[str, Any] | httpx.Response,
) -> dict[str, int]:
    """Validate either a direct Jev response or an exact TypeSafe OR response."""
    request_copy = _canonical_request_copy(request)
    payload = _response_payload(response)
    model = payload.get("model")

    if model == CANONICAL_MODEL:
        return validate_response(request_copy, payload)

    if model != OPENROUTER_RESOLVED_MODEL:
        raise ValueError(
            "response.model must be the canonical Jev model or the pinned "
            "OpenRouter resolved model"
        )
    if not _is_typesafe_provider(payload):
        raise ValueError("OpenRouter response provider must be exactly TypeSafe")

    # validate_response enforces exact model equality.  Use a copy so the raw
    # OpenRouter response remains untouched for durable provenance storage.
    mapped_request = dict(request_copy)
    mapped_request["model"] = OPENROUTER_RESOLVED_MODEL
    return validate_response(mapped_request, payload)


def classify_jev_response(
    response: Mapping[str, Any] | httpx.Response,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify a direct or pinned OpenRouter Jev response without mutation."""
    payload = _response_payload(response)
    model = payload.get("model")
    if model == CANONICAL_MODEL:
        return classify(payload, policy)
    if model != OPENROUTER_RESOLVED_MODEL:
        raise ValueError(
            "response.model must be the canonical Jev model or the pinned "
            "OpenRouter resolved model"
        )
    if not _is_typesafe_provider(payload):
        raise ValueError("OpenRouter response provider must be exactly TypeSafe")

    canonical_response = dict(payload)
    canonical_response["model"] = CANONICAL_MODEL
    return classify(canonical_response, policy)


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite nonnegative number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return numeric


def response_cost_usd(
    response: Mapping[str, Any] | httpx.Response,
    input_tokens: int,
    route: str,
) -> float:
    """Return provider-reported OR cost or deterministic direct input cost."""
    if not isinstance(route, str):
        raise ValueError("route must be 'openrouter' or 'typesafe'")
    if route == "openrouter":
        payload = _response_payload(response)
        usage = payload.get("usage")
        if not isinstance(usage, Mapping) or "cost" not in usage:
            raise ValueError("OpenRouter response.usage.cost is missing")
        return _finite_nonnegative(usage["cost"], "response.usage.cost")

    if route != "typesafe":
        raise ValueError("route must be 'openrouter' or 'typesafe'")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
    ):
        raise ValueError("input_tokens must be a nonnegative integer")
    return float(input_tokens) * INPUT_PRICE_PER_TOKEN


__all__ = [
    "OPENROUTER_ENDPOINT",
    "OPENROUTER_MODEL",
    "OPENROUTER_RESOLVED_MODEL",
    "OpenRouterClient",
    "validate_jev_response",
    "classify_jev_response",
    "response_cost_usd",
]
