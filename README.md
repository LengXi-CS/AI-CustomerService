# FastGPT 微信智能客服助手 (FastGpt Assistant)

基于模式 B（本地 Core 确定性查表 + FastGPT 语义知识库 RAG）构建的企业微信客服智能代理系统。

## 核心架构与模块组织

1. **确定性主数据查表 (Mode B)**：工厂、SKU、品类、商标、批次报告五张业务表由飞书 OpenAPI 拉取入库 SQLite 读模型（`data/read-model.db`），支持商品主数据毫秒级精准检索与实体关联（支持跨表关联查询时目标实体核心属性如 `factory_name` 的自动补全与兜底）。
2. **官网语义知识库 (FastGPT RAG)**：29 篇官方清洗文档导入 FastGPT 知识库（Docker 容器运行于 3001 端口，pgvector + Ollama bge-m3），剥离 Frontmatter 噪声切片入库。
3. **微信端集成 (GeweAPI) 与气泡纯文本友好排版**：对接 GeWe 微信网关，支持原生多模态上下文接入与气泡引用回复；全面贯彻微信端 100% 纯文本排版（严禁 Markdown，自动过滤 `**` 粗体与 `#` 标题），严格遵循客观事实性，杜绝客服动作臆造与虚假承诺。
4. **统一运维与 CLI (`scripts/ops.py`)**：提供测试、lint、数据同步、文档导入及服务启停的一键入口。

## 常用操作命令

所有命令在 `C:\Users\24501\Desktop\FastGpt` 根目录执行：

```bash
# 1. 运行完整单元测试套件
.venv\Scripts\python.exe -m pytest -q

# 2. 代码风格与规范检查 (Ruff 0 错误)
.venv\Scripts\ruff.exe check src tests

# 3. 从飞书 OpenAPI 全量同步多维表格并构建 SQLite 读模型 (仅元数据或含下载)
.venv\Scripts\python.exe scripts/ops.py sync-bitable
# 跳过耗时附件物理下载（仅构建表结构与元数据索引）
.venv\Scripts\python.exe scripts/ops.py sync-bitable --no-download

# 4. 重新切片并导入官网知识库至 FastGPT
.venv\Scripts\python.exe scripts/ops.py import-docs

# 5. 启动后端服务 (端口 5330)
.venv\Scripts\python.exe scripts/ops.py serve

# 6. 后端直接提问测试 (CLI)
.venv\Scripts\python.exe scripts/ops.py query "EO灭菌和电子束灭菌有什么区别？"
.venv\Scripts\python.exe scripts/ops.py query "查询商品 6976070461130 的信息与关联工厂"
.venv\Scripts\python.exe scripts/ops.py query "查询商标 瀚思 的相关信息"
```

## 服务端口拓扑与接口

- **FastGPT Web / API**: `http://localhost:3001`
- **FastGPT MinIO**: `http://localhost:9010` (API) / `9011` (Console)
- **FastGPT Assistant 后端服务**: `http://localhost:5330`
- **健康检查端点**: `GET http://localhost:5330/health`
- **直接查询接口**: `POST http://localhost:5330/api/v1/query`
- **微信 GeWe 回调端点**: `POST http://localhost:5330/api/wechat/callback`

## 当前实现基线（2026-09-19）

- 每个客户需求独立创建 task 和状态机；`human_active` 按 task 保持粘性。
- 群级上下文保留最近 60 条消息；引用消息规划时补入前后各 5 条；上下文和入站媒体不按时间 TTL 清理，只随消息窗口淘汰。
- 图片媒体按原消息 ID和 task 绑定，支持先发图片后发问题及引用图片恢复；检测报告查询固定沿 `SKU → category → third_party_reports` 取附件。
- 当前自动化基线：`ruff check src tests` 通过，`pytest -q` **179 passed**；5330 的最近运行 PID 为 **93120**，`/health` 为 200。微信客户端是否通过仍须负责人实际确认。

下一会话待做：统一全模态 LLM 节点的输入链路。当前视觉分析节点仍只收到图片和固定通用提示，完整需求、引用文本和选中的群上下文在后续规划阶段才送入；下一步应显式把这些输入和 task 图片传给全模态节点，输出结构化候选/置信度/目标数量及可校验查询计划，同时保留本地 SKU 精确核验。详见 `需求级任务隔离设计.md`、`整改规划与进度.md` 和 `交接文档.md` 的最新章节。
