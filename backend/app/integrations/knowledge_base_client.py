"""Generic, read-only Company RAG HTTP client.

Supported provider contract:
``POST {api_base_url}/chunk/retrieve`` with an ``api-key`` header and a body
containing ``query``, ``topK``, ``similarityThreshold`` and
``retrieveStrategy``.  The provider may return a list directly or under
``data``, ``hits``, ``results`` or ``list``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx


COMPANY_RAG_CONFIG_MISSING = "COMPANY_RAG_CONFIG_MISSING"
COMPANY_RAG_API_KEY_MISSING = "COMPANY_RAG_API_KEY_MISSING"
COMPANY_RAG_CONNECTION_ERROR = "COMPANY_RAG_CONNECTION_ERROR"
COMPANY_RAG_RESPONSE_ERROR = "COMPANY_RAG_RESPONSE_ERROR"


@dataclass(slots=True)
class CompanyRagResult:
    success: bool
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CompanyRagSettings:
    api_base_url: str
    api_key: str
    timeout_seconds: int = 30


class CompanyRagClient:
    def __init__(self, *, settings: CompanyRagSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(float(settings.timeout_seconds), connect=10.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def retrieve_chunks(self, payload: Mapping[str, Any]) -> CompanyRagResult:
        if not self._settings.api_base_url:
            return CompanyRagResult(False, error_code=COMPANY_RAG_CONFIG_MISSING, error_message="Company RAG URL is not configured.")
        if not self._settings.api_key:
            return CompanyRagResult(False, error_code=COMPANY_RAG_API_KEY_MISSING, error_message="Company RAG API key is not configured.")
        url = f"{self._settings.api_base_url.rstrip('/')}/chunk/retrieve"
        try:
            response = await self._client.post(
                url,
                headers={"api-key": self._settings.api_key},
                json=dict(payload),
            )
        except httpx.HTTPError as exc:
            return CompanyRagResult(False, error_code=COMPANY_RAG_CONNECTION_ERROR, error_message=str(exc))
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_error:
            return CompanyRagResult(False, error_code=COMPANY_RAG_RESPONSE_ERROR, error_message=f"Provider returned HTTP {response.status_code}.", http_status=response.status_code, raw=body if isinstance(body, dict) else {})
        if isinstance(body, dict) and body.get("success") is False:
            return CompanyRagResult(False, error_code=str(body.get("code") or COMPANY_RAG_RESPONSE_ERROR), error_message=str(body.get("message") or "Provider rejected the query."), http_status=response.status_code, raw=body)
        data = body.get("data", body) if isinstance(body, dict) else body
        return CompanyRagResult(True, data=data, http_status=response.status_code, raw=body if isinstance(body, dict) else {})

    async def health_check(self) -> CompanyRagResult:
        return await self.retrieve_chunks({"query": "health check", "topK": 1, "similarityThreshold": 0.0, "retrieveStrategy": 2})


def build_company_rag_client(*, api_base_url: str, api_key: str, timeout_seconds: int = 30) -> CompanyRagClient:
    return CompanyRagClient(settings=CompanyRagSettings(api_base_url=api_base_url, api_key=api_key, timeout_seconds=timeout_seconds))
