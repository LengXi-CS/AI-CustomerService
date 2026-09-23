#!/usr/bin/env python3
"""Run a representative question set against the local /api/v1/query endpoint.

Purpose: a fast, side-effect-free regression check of the routing + retrieval
chain. ``/api/v1/query`` calls ``AssistantEngine.process_text_query`` and returns
the reply; it never touches ``dispatch_outbound``, so **no WeChat message is
sent** — not to the group and not to the owner. Real-device acceptance still has
to be done by hand.

Each case declares the outbound channels that count as correct plus any
substrings that must never appear in the answer, so a run is self-judging.
Judging the *content* of an answer is still the reviewer's job.

One behaviour deliberately cannot be covered here — "人工介入后 bot 静默" is a
property of ``ConversationStateStore`` + the poller (a ``human_active`` task is
never claimed), not of ``process_text_query``. It is pinned by
``tests/test_conversation_state.py`` instead.

Usage (use the venv interpreter directly; this machine's bash PATH is broken, so
avoid pipes and unix tools):

    .venv/Scripts/python.exe scripts/regression_representative_queries.py \
        --out logs/regression-representative.json

Exit code is 0 when at least one case passed.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Case:
    """One regression case and what makes it correct."""

    case_id: str
    question: str
    # ReplyDeliverable.channel_used values that count as correct.
    expected: tuple[str, ...]
    probes: str
    # Substrings that must never reach a customer (internal text, rule hints).
    must_not_contain: tuple[str, ...] = field(default_factory=tuple)


CASES: list[Case] = [
    # --- 结构化事实 ---
    Case(
        "S-01",
        "SKU编码 6976070460164 的产品名称、关联工厂 ID、关联品类 ID 和商标编码是什么？",
        ("structured",),
        "结构化 sku 属性",
    ),
    Case(
        "S-02",
        "SKU编码 6976070460164 的产品尺寸是多少？",
        ("structured",),
        "缺失属性：不得编造",
    ),
    Case(
        "F-01",
        "周口美腾实业有限公司有几份医护级认证？",
        ("structured",),
        "计数类计划：count 必须带进上下文，不得答'暂未记录'",
    ),
    Case("F-02", "周口美腾实业有限公司有哪些认证？", ("structured",), "认证枚举"),
    Case("F-03", "把周口美腾实业有限公司的营业执照发我。", ("structured",), "附件交付：营业执照"),
    Case(
        "F-07",
        "周口美腾实业有限公司有哪些医护级认证技术标准？",
        ("structured",),
        "技术标准与认证区分",
    ),
    Case("C-02", "把品类 HS005 的第三方检测报告发我。", ("structured",), "附件交付：品类检测报告"),
    Case("C-04", "品类 HS033 有生产许可证吗？", ("structured",), "存在性判断（有）"),
    Case("B-01", "产品批次检测报告表目前有哪些 SKU 编码？", ("structured",), "批次报告枚举"),
    Case("T-01", "商标信息表里目前有哪些商标？", ("structured",), "商标枚举"),
    Case(
        "X-01",
        "SKU 6976070460164 关联的品类、工厂和商标分别是什么？",
        ("structured",),
        "related 跨表",
    ),
    Case("X-03", "瀚思一次性毛巾关联工厂的营业执照发我。", ("structured",), "SKU→工厂附件"),
    Case(
        "X-04",
        "品类 HS005 对应哪个工厂？这个工厂有哪些医护级认证？",
        ("structured",),
        "品类→工厂",
    ),
    # --- 叙述知识问答 ---
    Case("W-07", "一次性内裤能穿多久？", ("narrative",), "叙述通道"),
    Case(
        "W-11",
        "“特护级”具体是怎么认定的？包括哪些要求或认证？",
        ("narrative",),
        "叙述通道（连打时会因 FastGPT 限流误判，见 --pause-between）",
    ),
    Case("W-12", "想买瀚思的产品，现在哪些平台有官方店？", ("narrative",), "叙述通道"),
    # --- 标准需求但缺实体 → 必须澄清（不得落人工协助） ---
    Case("N-03", "这两款检测报告麻烦提供下", ("clarify",), "标准需求缺实体 → 澄清"),
    Case("N-05", "查一下产品", ("clarify",), "标准需求缺信息 → 澄清"),
    # --- 无实质业务内容 → 必须静默 ---
    Case("N-06", "嗯", ("silent",), "纯语气词 → 静默"),
    Case("N-07", "哦", ("silent",), "纯语气词 → 静默"),
    Case("N-08", "？", ("silent",), "纯符号 → 静默"),
    # --- 非标准需求 → 只安抚 + 私聊负责人 ---
    Case("N-01", "帮我改一下发票抬头", ("soothe",), "非标准需求 → 安抚"),
    Case("N-02", "帮我投诉一下上周的物流", ("soothe",), "非标准需求 → 安抚"),
    Case("N-09", "你们能不能便宜点，给个批发价", ("soothe",), "议价 → 安抚"),
    # --- 标准需求但档案查不到 → 资料不足 + 转人工 ---
    Case(
        "N-04",
        "6976070460041 的检测报告",
        ("structured", "structured_no_evidence"),
        "该 SKU 有报告记录 → 应正常给附件（此前误答'未找到'）",
    ),
    Case(
        "N-10",
        "周口美腾实业有限公司有 ISO 9001 认证吗？",
        ("structured", "structured_no_evidence"),
        "库中大概率为空的资质 → 存在性查询，不得判非标准/静默",
    ),
    Case(
        "N-11",
        "把周口美腾实业有限公司的消防验收报告发我。",
        ("structured_no_evidence", "structured", "clarify"),
        "计划被校验拒绝 → 转人工；内部校验文案绝不外泄（这是本条真正的硬约束）",
        ("已注册的附件字段", "必须选择", "validity"),
    ),
    # --- 对照组 ---
    Case("N-12", "6976070461130 的商品信息和工厂", ("structured",), "对照组：正常执行"),
]


def _post(base_url: str, query: str, user_id: str, timeout: float) -> dict[str, object]:
    payload = json.dumps({"query": query, "user_id": user_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/query",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "detail": exc.read().decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001 - probe reports any failure as data
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5330")
    parser.add_argument("--user-id", default="regression_probe")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--pause-between",
        type=float,
        default=0.0,
        help=(
            "Seconds to sleep between cases. Back-to-back questions trip a rate "
            "limit in front of FastGPT: /api/core/dataset/searchTest starts "
            "self-redirecting (308 to the same URL) for a few seconds, so the "
            "narrative cases can fail spuriously. Measured recovery is 6-10s; "
            "production is unaffected because questions arrive ~2 minutes apart."
        ),
    )
    parser.add_argument("--out", default="", help="Write raw JSON results to this path")
    parser.add_argument("--only", default="", help="Comma-separated case ids (default: all)")
    args = parser.parse_args()

    wanted = {item.strip().upper() for item in args.only.split(",") if item.strip()}
    cases = [c for c in CASES if not wanted or c.case_id.upper() in wanted]

    records: list[dict[str, object]] = []
    for index, case in enumerate(cases):
        if index and args.pause_between > 0:
            time.sleep(args.pause_between)
        started = time.perf_counter()
        result = _post(args.base_url, case.question, args.user_id, args.timeout)
        elapsed = time.perf_counter() - started
        answer = str(result.get("answer") or result.get("error") or "")
        channel = str(result.get("channel") or "")

        leaked = [bad for bad in case.must_not_contain if bad in answer]
        if leaked:
            verdict = "LEAK"
        elif channel in case.expected:
            verdict = "PASS"
        elif not answer:
            verdict = "NO-REPLY"
        else:
            verdict = "FAIL"

        record = {
            "case_id": case.case_id,
            "question": case.question,
            "probes": case.probes,
            "expected_channels": list(case.expected),
            "channel": channel,
            "verdict": verdict,
            "leaked": leaked,
            "elapsed_s": round(elapsed, 2),
            "answer": answer,
            "deliverables": result.get("deliverables", []),
            "raw_error": result.get("detail", ""),
        }
        records.append(record)
        print(
            f"[{verdict}] {case.case_id} channel={channel!r} expected={list(case.expected)} "
            f"files={len(record['deliverables'])} {elapsed:.1f}s"
        )
        print(f"    Q: {case.question}")
        print(f"    A: {answer.replace(chr(10), ' / ')[:280]}")
        print(f"    probes: {case.probes}")
        if leaked:
            print(f"    !! leaked internal text: {leaked}")
        print()

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"raw results written to {out_path}")

    passed = sum(1 for r in records if r["verdict"] == "PASS")
    problems = [f"{r['case_id']}:{r['verdict']}" for r in records if r["verdict"] != "PASS"]
    print(f"done: {len(records)} cases, {passed} PASS, {len(problems)} not PASS {problems}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
