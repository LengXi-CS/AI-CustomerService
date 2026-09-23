"""FastGPT Knowledge Base & Application API Client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 实测（2026-09-18）：同一个 POST 打 /api/core/dataset/searchTest，会**成串**自重定向。
# 定量规律（每轮先发 1 次、间隔 N 秒再发 1 次）：
#   N=1s → 308 仍 308；N=3s → 308 仍 308；N=6s → 恢复 401；N=10s → 两次都 401。
# 即冷却窗口约 3~6 秒。此前 0.4 秒的重试落在同一窗口内，等于没重试。
#
# 由此得出两条硬结论：
# 1. **不要开 `follow_redirects`**：Location 与请求 URL 完全相同（自重定向），
#    跟随会一路循环到 "Exceeded maximum allowed redirects"，比不跟随更糟；
# 2. 重试间隔必须**跨过冷却窗口**，所以取 3.5 秒、共 3 次（t=0 / 3.5 / 7.0）。
#
# 注意：这是**客户端缓解**，不是根治。FastGPT 容器侧为何成串自重定向仍需单独排查。
_RETRYABLE_STATUS = frozenset({301, 302, 303, 307, 308, 500, 502, 503, 504})
_RETRY_DELAY_SECONDS = 3.5
_RETRY_ATTEMPTS = 3


@dataclass(slots=True)
class FastGptSearchResult:
    """A matched knowledge chunk returned by FastGPT dataset search."""

    id: str
    q: str
    a: str
    score: float
    source_name: str = ""
    chunk_index: int = 0


@dataclass(slots=True)
class FastGptChatResponse:
    """Standardized response from FastGPT application completion."""

    answer: str
    quote_sources: list[str] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)


class FastGptClient:
    """Client for FastGPT dataset search and application completions."""

    def __init__(
        self,
        base_url: str = "http://localhost:3001",
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        raw_base = base_url.rstrip("/")
        if raw_base.endswith("/api"):
            raw_base = raw_base[:-4].rstrip("/")
        self.base_url = raw_base
        self.api_key = api_key
        self.timeout = timeout
        # 刻意保持 httpx 默认的 follow_redirects=False，理由见文件头注释：
        # FastGPT 会自重定向，跟随会变成循环。瞬时故障用重试兜，不用跟随兜。
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def _post_with_retry(
        self,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> httpx.Response:
        """POST with a bounded retry that spans the measured self-redirect cooldown.

        Only 3xx/5xx and transport errors are retried; a 4xx is a real request
        problem and is surfaced immediately. When every attempt fails, the last
        response's ``raise_for_status()`` (or the transport error) propagates, so
        callers see the same failure they saw before — just much less often.
        """
        last_error: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = self._client.post(path, headers=headers, json=json)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if resp.status_code not in _RETRYABLE_STATUS:
                    resp.raise_for_status()
                    return resp
                last_error = httpx.HTTPStatusError(
                    f"retryable status {resp.status_code} for {path}",
                    request=resp.request,
                    response=resp,
                )
            if attempt + 1 < _RETRY_ATTEMPTS:
                logger.info(
                    "FastGPT %s returned a retryable failure (attempt %d/%d: %s); "
                    "waiting %.1fs for the redirect cooldown",
                    path,
                    attempt + 1,
                    _RETRY_ATTEMPTS,
                    last_error,
                    _RETRY_DELAY_SECONDS,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FastGptClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _headers(self, custom_token: str | None = None) -> dict[str, str]:
        token = custom_token or self.api_key
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def search_dataset(
        self,
        dataset_id: str,
        text: str,
        *,
        limit: int = 5,
        similarity: float = 0.5,
        search_mode: str = "embedding",
    ) -> list[FastGptSearchResult]:
        """Search knowledge chunks directly from a FastGPT dataset."""
        payload = {
            "datasetId": dataset_id,
            "text": text,
            "limit": limit,
            "similarity": similarity,
            "searchMode": search_mode,
        }
        resp = self._post_with_retry(
            "/api/core/dataset/searchTest",
            headers=self._headers(),
            json=payload,
        )
        data = resp.json()
        if data.get("code") != 200:
            logger.warning(
                "FastGPT search code %s: %s",
                data.get("code"),
                data.get("message"),
            )
            return []

        results: list[FastGptSearchResult] = []
        for item in data.get("data", {}).get("list", []):
            raw_score = item.get("score")
            score_val = 0.0
            if isinstance(raw_score, list) and raw_score:
                score_val = float(raw_score[0].get("value") or 0.0)
            elif isinstance(raw_score, (int, float)):
                score_val = float(raw_score)

            results.append(
                FastGptSearchResult(
                    id=str(item.get("id") or ""),
                    q=str(item.get("q") or ""),
                    a=str(item.get("a") or ""),
                    score=score_val,
                    source_name=str(item.get("sourceName") or ""),
                    chunk_index=int(item.get("chunkIndex") or 0),
                )
            )
        return results

    def chat_completion(
        self,
        app_token: str,
        messages: list[dict[str, str]],
        *,
        chat_id: str | None = None,
        detail: bool = True,
    ) -> FastGptChatResponse:
        """Query FastGPT application via OpenAI-compatible chat completions."""
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": False,
            "detail": detail,
        }
        if chat_id:
            payload["chatId"] = chat_id

        resp = self._client.post(
            "/api/v1/chat/completions",
            headers=self._headers(app_token),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        answer = ""
        quote_sources: list[str] = []
        choices = data.get("choices") or []
        if choices:
            answer = choices[0].get("message", {}).get("content", "").strip()

        # Extract quote sources if available
        new_detail = data.get("responseData") or []
        for item in new_detail:
            if isinstance(item, dict) and item.get("moduleType") == "datasetSearchNode":
                quote_list = item.get("quoteList") or []
                for q in quote_list:
                    src = q.get("sourceName")
                    if src and src not in quote_sources:
                        quote_sources.append(src)

        return FastGptChatResponse(
            answer=answer,
            quote_sources=quote_sources,
            raw_response=data,
        )
