from fastgpt_assistant.orchestration.demand_splitter import (
    split_and_merge_demands,
    validate_model_demands,
)


def test_strong_separators_split_independent_demands() -> None:
    demands = split_and_merge_demands(["查 SKU 甲；再查 SKU 乙\n还要看商标"])

    assert demands == ["查 SKU 甲", "再查 SKU 乙", "还要看商标"]


def test_follow_up_message_is_merged_into_previous_demand() -> None:
    demands = split_and_merge_demands(["查 SKU 甲", "还有这个产品的工厂名称呢"])

    assert demands == ["查 SKU 甲\n还有这个产品的工厂名称呢"]


def test_explicit_new_question_after_follow_up_stays_independent() -> None:
    demands = split_and_merge_demands(["查 SKU 甲", "还有这个产品的工厂", "另外查 SKU 乙"])

    assert demands == ["查 SKU 甲\n还有这个产品的工厂", "查 SKU 乙"]


def test_model_segments_must_preserve_the_whole_message() -> None:
    message = "查 SKU 甲；再查 SKU 乙"

    assert validate_model_demands(message, ["查 SKU 甲", "再查 SKU 乙"]) == [
        "查 SKU 甲",
        "再查 SKU 乙",
    ]
    assert validate_model_demands(message, ["查 SKU 甲"]) is None
    assert validate_model_demands(message, ["查 SKU 甲", "再查不存在的 SKU"]) is None
