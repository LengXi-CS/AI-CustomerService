"""Test backend dataset and structured queries against running FastAPI server (port 5330)."""

import json
import time

import httpx

BASE_URL = "http://127.0.0.1:5330"


def test_queries() -> None:
    client = httpx.Client(base_url=BASE_URL, timeout=60.0)

    # 1. Health check
    print("=== 1. HEALTH CHECK ===")
    try:
        r = client.get("/health")
        print(f"Status: {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Health check failed: {e}")
        return

    # 2. Test FastGPT Knowledge base dataset queries
    test_cases = [
        {
            "category": "FastGPT 知识库问答 (FAQ - 灭菌方式)",
            "query": "EO灭菌和电子束灭菌有什么区别？",
        },
        {
            "category": "FastGPT 知识库问答 (FAQ - 抗菌标准)",
            "query": "抗菌10A和7A有什么区别？",
        },
        {
            "category": "FastGPT 知识库问答 (产品特性 - 空气裤)",
            "query": "金标莱赛尔日抛空气裤有什么核心特点？",
        },
        {
            "category": "SQLite 结构化主数据查询 (SKU 查询)",
            "query": "查询商品 6976070461130 的信息与关联工厂",
        },
        {
            "category": "SQLite 结构化主数据查询 (商标查询)",
            "query": "查询商标 瀚思 的相关信息",
        },
    ]

    print("\n=== 2. RUNNING DATASET & STRUCTURED QUERIES ===")
    for idx, tc in enumerate(test_cases, 1):
        print(f"\n--- [Test {idx}] {tc['category']} ---")
        print(f"Question: {tc['query']}")
        start_t = time.time()
        try:
            resp = client.post(
                "/api/v1/query",
                json={"query": tc["query"], "user_id": f"test_user_{idx}"},
            )
            elapsed = time.time() - start_t
            if resp.status_code == 200:
                data = resp.json()
                print(f"Channel: {data.get('channel')} (in {elapsed:.2f}s)")
                print(f"Answer:\n{data.get('answer')}")
            else:
                print(f"HTTP Error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"Request failed: {e}")


if __name__ == "__main__":
    test_queries()
