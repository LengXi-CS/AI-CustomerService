"""Test deterministic SQLite query execution against the real read model."""

from pathlib import Path

import pytest

from fastgpt_assistant.core.media_store import MediaStore
from fastgpt_assistant.core.query_engine import (
    ReadModel,
    SqliteQueryExecutor,
    _repair_cross_scope_select,
)
from fastgpt_assistant.core.schema import QueryEntity, QueryPlan, QueryRelation


@pytest.fixture
def query_executor() -> SqliteQueryExecutor:
    project_root = Path(__file__).resolve().parent.parent
    db_path = project_root / "data" / "read-model.db"
    assert db_path.is_file(), f"Read model database missing: {db_path}"
    read_model = ReadModel(db_path)
    return SqliteQueryExecutor(read_model)


def test_read_model_counts(query_executor: SqliteQueryExecutor) -> None:
    counts = query_executor.read_model.table_counts()
    assert counts["factories"] >= 20
    assert counts["skus"] >= 150
    assert counts["categories"] >= 50
    assert counts["trademarks"] >= 5


def test_count_factories(query_executor: SqliteQueryExecutor) -> None:
    plan = QueryPlan(
        channel="structured",
        operation="count",
        scope="factory",
        confidence=1.0,
    )
    result = query_executor.execute(plan)
    assert result["status"] == "ok"
    assert result["data"]["count"] >= 20


def test_query_sku_record(query_executor: SqliteQueryExecutor) -> None:
    plan = QueryPlan(
        channel="structured",
        operation="record_value",
        scope="sku",
        entities=[QueryEntity(type="sku", id="6976070461130")],
        select=["sku_code", "product_name", "trademark_code"],
        confidence=1.0,
    )
    res = query_executor.execute_to_result(plan)
    assert res.status == "answered"
    records = res.metadata["data"]["records"]
    assert len(records) == 1
    assert records[0]["fields"]["sku_code"] == "6976070461130"
    assert len(res.provenances) >= 1
    assert res.provenances[0].record_id == "6976070461130"


def test_related_sku_to_trademark(query_executor: SqliteQueryExecutor) -> None:
    plan = QueryPlan(
        channel="structured",
        operation="related",
        scope="sku",
        entities=[QueryEntity(type="sku", id="6976070461130")],
        relations=[QueryRelation(name="sku_to_trademark")],
        select=["trademark_code", "trademark_name"],
        confidence=1.0,
    )
    result = query_executor.execute(plan)
    assert result["status"] == "ok"
    assert len(result["data"]["records"]) >= 1
    assert result["data"]["records"][0]["relation"] == "sku_to_trademark"


def test_sku_cross_scope_name_is_repaired_to_related_query() -> None:
    """A planner omission must not surface as an unregistered SKU field."""
    plan = QueryPlan(
        channel="structured",
        operation="record_value",
        scope="sku",
        entities=[QueryEntity(type="sku", id="6976070461130")],
        select=["category_name"],
        confidence=1.0,
    )
    repaired = _repair_cross_scope_select(plan)
    assert repaired.operation == "related"
    assert [relation.name for relation in repaired.relations] == ["sku_to_category"]


def test_sku_category_attachment_is_repaired_to_related_query() -> None:
    plan = QueryPlan(
        channel="structured",
        operation="file",
        scope="sku",
        entities=[QueryEntity(type="sku", id="6976070461130")],
        select=["third_party_reports"],
        response_mode="deliver_files",
        confidence=1.0,
    )
    repaired = _repair_cross_scope_select(plan)
    assert repaired.operation == "related"
    assert [relation.name for relation in repaired.relations] == ["sku_to_category"]


def test_media_url_preserves_file_extension(tmp_path) -> None:
    deliverable = MediaStore(tmp_path).build_deliverable(
        "med_abc",
        "检测报告.pdf",
        base_download_url="https://public.example",
    )
    assert deliverable.download_url == "https://public.example/v1/media/med_abc.pdf"


def test_related_sku_to_factory_auto_completes_factory_name(
    query_executor: SqliteQueryExecutor,
) -> None:
    """Ensure related query automatically complements factory_name
    even if planner only requested factory_id.
    """
    plan = QueryPlan(
        channel="structured",
        operation="related",
        scope="sku",
        entities=[QueryEntity(type="sku", id="6976070461130")],
        relations=[QueryRelation(name="sku_to_factory")],
        select=["sku_code", "product_name", "factory_id"],
        confidence=1.0,
    )
    result = query_executor.execute(plan)
    assert result["status"] == "ok"
    records = result["data"]["records"]
    assert len(records) >= 1
    factory_record = records[0]
    assert factory_record["relation"] == "sku_to_factory"
    assert factory_record["fields"]["factory_id"] == "020001"
    # Verify factory_name is automatically populated
    assert "factory_name" in factory_record["fields"]
    assert "医疗器械有限公司" in factory_record["fields"]["factory_name"]


def test_clean_wechat_text() -> None:
    from fastgpt_assistant.adapters.llm.chat_model import clean_wechat_text

    raw = "**商品信息**\n### 工厂详情\n- 编码: 123\n```json\n{}\n```"
    cleaned = clean_wechat_text(raw)
    assert "**" not in cleaned
    assert "###" not in cleaned
    assert "```" not in cleaned
    assert "【工厂详情】" in cleaned
    assert "· 编码: 123" in cleaned


def test_clean_wechat_text_strips_the_remaining_markdown_markers() -> None:
    """微信气泡不渲染 Markdown；模型偶尔仍带语法标记，必须全部收敛成纯文本。"""
    from fastgpt_assistant.adapters.llm.chat_model import clean_wechat_text

    raw = (
        "请看下面：\n"
        "1. 第一项\n"
        "2) 第二项\n"
        "> 引用说明\n"
        "---\n"
        "`sku_code`\n"
        "| 商品 | 工厂 |\n"
        "| --- | --- |\n"
        "| 口罩 | 河南 |\n"
        "~~作废~~文本\n"
    )
    cleaned = clean_wechat_text(raw)

    assert "· 第一项" in cleaned
    assert "· 第二项" in cleaned
    assert "引用说明" in cleaned
    assert "sku_code" in cleaned
    assert "商品 · 工厂" in cleaned
    assert "口罩 · 河南" in cleaned
    assert "作废文本" in cleaned
    for marker in ("1. ", "2) ", ">", "---", "|", "`", "~~", "| --- |"):
        assert marker not in cleaned, marker


def test_clean_wechat_text_keeps_dimension_asterisks() -> None:
    """商品名里的 `*` 是尺寸分隔符，不能被当成强调标记。

    回归防护：旧规则 `\\*{1,3}(.*?)\\*{1,3}` 会把「瀚思一次性枕套白色50*80*2个」
    吃成「瀚思一次性枕套白色50802个」—— 尺寸被静默抹掉，属于客户可见的事实损坏。
    """
    from fastgpt_assistant.adapters.llm.chat_model import clean_wechat_text

    for raw in (
        "一次性睡袋单人款160*220",
        "自粘绷带5cm*4.5米",
        "瀚思一次性枕套白色50*80*2个",
        "瀚思一次性枕套印花款50*80*2个",
        "清洗液（0.9%生理盐水）15ml*20支/盒(妆字号)",
        "成人游泳私密防水贴单片10*25cm",
        "· 规格：50*80*2个",
    ):
        assert clean_wechat_text(raw) == raw


def test_clean_wechat_text_leaves_a_good_bubble_answer_untouched() -> None:
    """已经合规的气泡文本必须逐字保留 —— 清理只能去标记，绝不能改写内容。"""
    from fastgpt_assistant.adapters.llm.chat_model import clean_wechat_text

    bubble = (
        "【商品信息】\n"
        "· SKU编码：6976070461130\n"
        "· 商品名称：瀚思一次性枕套白色50*80*2个\n"
        "· 规格：50*80*2个\n"
        "\n"
        "【关联工厂】\n"
        "· 工厂名称：河南瀚思医疗器械有限公司"
    )
    assert clean_wechat_text(bubble) == bubble


def test_repair_cross_scope_sku_to_category() -> None:
    project_root = Path(__file__).resolve().parent.parent
    db_path = project_root / "data" / "read-model.db"
    media_dir = project_root / "data" / "resources" / "attachments"
    executor = SqliteQueryExecutor(
        ReadModel(db_path),
        media_store=MediaStore(resources_root=media_dir),
    )
    plan = QueryPlan(
        schema_version="query-plan-v1",
        channel="structured",
        operation="file",
        scope="category",
        entities=[QueryEntity(type="sku", id="6976070461215")],
        select=["third_party_reports"],
        confidence=0.9,
    )
    result = executor.execute_to_result(plan)
    assert result.is_success is True
    assert any(p.table == "categories" for p in result.provenances)
    assert len(result.deliverables) >= 1
