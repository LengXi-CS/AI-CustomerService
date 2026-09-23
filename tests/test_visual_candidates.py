"""Tests for deterministic image candidate validation."""

from __future__ import annotations

import sqlite3

from fastgpt_assistant.core.query_engine import ReadModel
from fastgpt_assistant.orchestration.visual_candidates import (
    is_image_dependent_query,
    is_multi_visual_query,
    is_report_query,
    normalize_candidate_code,
    planner_safe_analysis,
    resolve_visual_skus,
)


def _read_model(tmp_path) -> ReadModel:
    path = tmp_path / "read-model.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE skus (sku_code TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO skus (sku_code) VALUES (?)",
            [("6976070460164",), ("6976070460171",)],
        )
    return ReadModel(path)


def _analysis(*codes: tuple[str, float], image_confidence: float = 0.96) -> dict[str, object]:
    return {
        "image_confidence": image_confidence,
        "candidates": [
            {
                "type": "upc",
                "raw": code,
                "normalized": code,
                "confidence": confidence,
            }
            for code, confidence in codes
        ],
    }


def test_unique_visual_candidate_requires_local_exact_match(tmp_path) -> None:
    result = resolve_visual_skus(
        _read_model(tmp_path),
        [_analysis(("6976 070460164", 0.98))],
    )

    assert result.status == "unique"
    assert result.sku_codes == ("6976070460164",)


def test_multiple_verified_visual_candidates_are_ambiguous(tmp_path) -> None:
    result = resolve_visual_skus(
        _read_model(tmp_path),
        [_analysis(("6976070460164", 0.98), ("6976070460171", 0.98))],
    )

    assert result.status == "ambiguous"
    assert result.sku_codes == ("6976070460164", "6976070460171")


def test_low_confidence_or_invalid_candidate_is_not_promoted(tmp_path) -> None:
    result = resolve_visual_skus(
        _read_model(tmp_path),
        [_analysis(("6976070460164", 0.79), image_confidence=0.79)],
    )

    assert result.status == "none"
    assert normalize_candidate_code("UPC: 6976-070460164") == "6976070460164"


def test_image_dependent_query_markers_are_conservative() -> None:
    assert is_image_dependent_query("图片里的产品工厂是谁")
    assert is_image_dependent_query("这个产品的商标")
    assert is_image_dependent_query("这个质检报告发一下")
    assert is_image_dependent_query("这三个产品的检测报告")
    assert not is_image_dependent_query("官网客服电话是多少")
    assert is_multi_visual_query("请分别查询这两个产品的工厂")
    assert is_multi_visual_query("请查询这三个产品的检测报告")
    assert is_multi_visual_query("请查询5个商品的检测报告")
    assert not is_multi_visual_query("请查询这个产品的工厂")
    assert is_report_query("这两款质检报告麻烦提供下")
    assert not is_report_query("请查询这个产品的工厂")


def test_explicit_multi_product_query_can_use_lower_vision_floor(tmp_path) -> None:
    read_model = _read_model(tmp_path)
    analyses = [
        {
            "image_confidence": 0.72,
            "candidates": [
                {
                    "type": "upc",
                    "raw": code,
                    "normalized": code,
                    "confidence": 0.72,
                }
                for code in ("6976070460164", "6976070460171")
            ],
        }
    ]

    result = resolve_visual_skus(read_model, analyses, minimum_confidence=0.50)

    assert result.status == "ambiguous"
    assert result.sku_codes == ("6976070460164", "6976070460171")


def test_planner_safe_analysis_drops_every_confidence_number() -> None:
    analysis: dict[str, object] = {
        "intent": "barcode",
        "visible_text": "6976070460164",
        "candidates": [
            {
                "type": "upc",
                "raw": "6976070460164",
                "normalized": "6976070460164",
                "confidence": 0.96,
            }
        ],
        "image_confidence": 0.42,
    }

    assert planner_safe_analysis(analysis) == {
        "intent": "barcode",
        "visible_text": "6976070460164",
        "candidates": [
            {"type": "upc", "raw": "6976070460164", "normalized": "6976070460164"}
        ],
    }
    # The raw analysis is untouched: the deterministic resolver still gates on
    # these numbers, only the planner prompt is shielded from them.
    assert analysis["image_confidence"] == 0.42
    assert analysis["candidates"][0]["confidence"] == 0.96


def test_planner_safe_analysis_ignores_malformed_candidates() -> None:
    safe = planner_safe_analysis(
        {"candidates": ["junk", {"type": "upc", "confidence": 0.9}]}
    )

    assert safe == {"candidates": [{"type": "upc"}]}
