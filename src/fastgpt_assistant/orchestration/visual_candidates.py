"""Deterministic validation of image-derived SKU candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from fastgpt_assistant.core.query_engine import ReadModel

ResolutionStatus = Literal["none", "unique", "ambiguous"]
_DIGITS_RE = re.compile(r"\D+")
# Keys that carry the image model's own certainty. They gate candidate promotion
# here and must never leak into the planner prompt (see planner_safe_analysis).
_CONFIDENCE_KEYS = frozenset({"confidence", "image_confidence"})


@dataclass(frozen=True, slots=True)
class VisualSkuResolution:
    """Local read-model result for one or more visual candidates."""

    status: ResolutionStatus
    sku_codes: tuple[str, ...] = ()
    reason: str = ""


def normalize_candidate_code(value: object) -> str:
    """Normalize OCR output without inventing or padding digits."""
    return _DIGITS_RE.sub("", str(value or "").strip())


def _valid_ean13(value: str) -> bool:
    if len(value) != 13 or not value.isdigit():
        return False
    checksum = sum(
        int(digit) * (3 if index % 2 else 1)
        for index, digit in enumerate(value[:12])
    )
    return (10 - checksum % 10) % 10 == int(value[-1])


def _candidate_codes(
    analyses: list[dict[str, object]], *, minimum_confidence: float = 0.80
) -> list[str]:
    codes: list[str] = []
    for analysis in analyses:
        if float(analysis.get("image_confidence") or 0.0) < minimum_confidence:
            continue
        candidates = analysis.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_type = str(candidate.get("type") or "").strip().lower()
            if candidate_type not in {"upc", "sku"}:
                continue
            try:
                confidence = float(candidate.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < minimum_confidence:
                continue
            code = normalize_candidate_code(candidate.get("normalized") or candidate.get("raw"))
            if not code:
                continue
            if candidate_type == "upc" and not _valid_ean13(code):
                continue
            if code not in codes:
                codes.append(code)
    return codes


def planner_safe_analysis(analysis: dict[str, object]) -> dict[str, object]:
    """Strip OCR confidence numbers before the planner reads an image analysis.

    The planner must judge *intent* and *what is missing*. If it also sees the
    image model's certainty, it absorbs that uncertainty as if it were intent
    uncertainty — the fused-score collapse described in
    ``架构诊断-越改越不如意的根因.md`` 根因 3.

    The numbers stay available to :func:`resolve_visual_skus`, which is the only
    consumer that should gate on them: promotion happens against the local read
    model, deterministically, not by the planner's impression of a photo.
    """
    safe: dict[str, object] = {}
    for key, value in analysis.items():
        if key in _CONFIDENCE_KEYS:
            continue
        if key == "candidates" and isinstance(value, list):
            safe[key] = [
                {k: v for k, v in item.items() if k not in _CONFIDENCE_KEYS}
                for item in value
                if isinstance(item, dict)
            ]
        else:
            safe[key] = value
    return safe


def resolve_visual_skus(
    read_model: ReadModel,
    analyses: list[dict[str, object]],
    *,
    minimum_confidence: float = 0.80,
) -> VisualSkuResolution:
    """Resolve visual identifiers against SQLite exactly once.

    Callers may lower the model-confidence floor only when the user explicitly
    requests multiple pictured products.  Exact local SKU validation remains
    mandatory, so a lower vision score can recover a real SKU but cannot invent
    a product from an unverified OCR string.
    """
    minimum_confidence = max(0.0, min(1.0, float(minimum_confidence)))
    candidate_codes = _candidate_codes(
        analyses, minimum_confidence=minimum_confidence
    )
    if not candidate_codes:
        reason = (
            "no_high_confidence_candidate"
            if minimum_confidence >= 0.80
            else "no_locally_verified_candidate"
        )
        return VisualSkuResolution("none", reason=reason)

    matched: list[str] = []
    with read_model.connection() as connection:
        for code in candidate_codes:
            row = connection.execute(
                "SELECT sku_code FROM skus WHERE sku_code = ? LIMIT 1",
                (code,),
            ).fetchone()
            if row is not None and str(row[0]) not in matched:
                matched.append(str(row[0]))

    if not matched:
        return VisualSkuResolution("none", reason="candidate_not_found_in_read_model")
    if len(matched) > 1:
        return VisualSkuResolution("ambiguous", tuple(matched), "multiple_verified_candidates")
    return VisualSkuResolution("unique", tuple(matched), "one_verified_candidate")


def is_image_dependent_query(query: str) -> bool:
    """Detect queries whose answer would rely on the preceding image."""
    normalized = "".join(query.split()).lower()
    if is_multi_visual_query(query):
        return True
    return any(
        marker in normalized
        for marker in (
            "图片",
            "图里",
            "图中",
            "这个产品",
            "这个商品",
            "这款",
            "这两款",
            "这几款",
            "该款",
            "此款",
            "该产品",
            "这个质检报告",
            "这个检测报告",
            "这个检验报告",
            "这个报告",
            "条码",
            "条形码",
            "upc",
            "ean",
            "包装",
            "背标",
        )
    )


def is_multi_visual_query(query: str) -> bool:
    """Detect an explicit request to process multiple pictured products separately."""
    normalized = "".join(query.split()).lower()
    if any(
        marker in normalized
        for marker in (
            "分别",
            "各自",
            "这些",
            "这几",
            "多个",
            "多款",
            "多种",
            "几款",
            "几种",
            "若干",
            "这些产品",
            "这些商品",
            "每个产品",
            "每个商品",
        )
    ):
        return True
    # Numeric quantities are intentionally generalized: 三款、三个、5个、
    # 12种等都表达多商品请求，不为某个具体数字维护分支。
    quantity_pattern = r"[一二两三四五六七八九十百千万\d]+(?:款|个|种|件|类)(?:产品|商品)?"
    return bool(re.search(quantity_pattern, normalized))


def is_report_query(query: str) -> bool:
    """Detect requests for inspection or quality reports."""
    normalized = "".join(query.split()).lower()
    return any(
        marker in normalized
        for marker in (
            "检测报告",
            "质检报告",
            "检验报告",
            "检验检测",
            "第三方报告",
        )
    )
