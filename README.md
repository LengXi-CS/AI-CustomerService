# FastGPT 微信智能客服助手 (FastGpt Assistant)

配置 WebUI 已支持群管理、回复策略、模型与连接、运行状态、修改记录与备份恢复、聊天记录浏览、保存前差异确认与管理员登录。聊天记录可查看群聊时间线、引用及本地留存附件，并按身份、类型、关键词和日期筛选。首次启用及部署步骤见 [WebUI 使用说明](docs/群管理WebUI.md)。

模型安抚已支持按客户原始需求及补充生成简短回复，并根据负责人通知配置限制后续动作表述。输入、提示词位置与生效方式见 [安抚话术生成](docs/安抚话术生成.md)。

运行状态页已加入本机 Docker 引擎、frpc 回调转发容器、AidNet 主程序及其内置连接进程的只读检测，支持异常提示和每15秒刷新。详情见 [WebUI 使用说明](docs/群管理WebUI.md)。

基于模式 B（本地 Core 确定性查表 + FastGPT 语义知识库 RAG）构建的企业微信客服智能代理系统。

## 核心架构与模块组织

1. **确定性主数据查表 (Mode B)**：工厂、SKU、品类、商标、批次报告五张业务表由飞书 OpenAPI 拉取入库 SQLite 读模型（`data/read-model.db`），支持商品主数据毫秒级精准检索与实体关联（支持跨表关联查询时目标实体核心属性如 `factory_name` 的自动补全与兜底）。
2. **官网语义知识库 (FastGPT RAG)**：29 篇官方清洗文档导入 FastGPT 知识库（Docker 容器运行于 3001 端口，pgvector + Ollama bge-m3），剥离 Frontmatter 噪声切片入库。
3. **微信端集成 (GeweAPI) 与气泡纯文本友好排版**：对接 GeWe 微信网关，支持原生多模态上下文接入与气泡引用回复；全面贯彻微信端 100% 纯文本排版（严禁 Markdown，自动过滤 `**` 粗体与 `#` 标题），严格遵循客观事实性，杜绝客服动作臆造与虚假承诺。
4. **统一运维与 CLI (`scripts/ops.py`)**：提供测试、lint、数据同步、文档导入及服务启停的一键入口。

## 常用操作命令

所有命令在项目根目录执行，本机为 `D:\瀚思AI客服`。已由 Windows 服务托管时，更新代码使用部署指南中的重启流程；不要同时另起一个监听5330的进程。

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

## 当前实现基线（2026-10-16）

- 每条需求保留独立 task；逐群开启 `associate_customer_demands` 后关联同一事项、统一等待并持久化安抚去重。不同客户、订单及独立主题隔离。
- 文本安抚和澄清支持逐群开启 `native_quote_replies`，使用原消息自身的引用元数据；信息不足时回退为带事项摘要的普通文本。开关以当前群管理配置为准。
- 模型安抚接收原始需求、同事项补充、引用及任务图片，生成简短真人客服话术；人工接管、已关闭、已安抚及发送结果不明等限制仍在发送链路执行。
- 当天群消息在本地保留，模型上下文按60000字符预算选择；人工接管及图片绑定跨午夜、跨重启保留。已回复事项也可登记接管，后续催办继承控制状态，明确交回后才恢复自动处理。
- 客户文字在模型拆分前持久登记；人工待判、领取任务及发送前均检查未完成入站消息。撤回标记持久保存，重启恢复不能复活已撤回需求。
- “回复策略”支持每群独立等待0–86400秒；未设置则跟随当前运行的全局默认。群覆盖值热加载，全局默认修改需重启。
- 个人名片42、企业微信名片66不创建需求、不触发安抚、不登记接管；配置群内仍归档为名片上下文。后续真实文字问题及明确引用名片的文字需求正常处理。
- 图片按原消息及任务绑定；检测报告仍沿 `SKU → category → third_party_reports` 查询并核验。全模态输入协议已实现，旧交接中“待统一输入链路”的描述为历史记录。
- 最近代码验证：全量 **686 passed**，Ruff通过；按群等待时间前端类型检查、构建及模拟浏览器验收通过。代码验证不代表最新修复已经被正式服务加载；名片修复仍待重启后核验。

## 文档入口

- [目录与归档](docs/目录与归档.md)：日常维护目录、保留资料和历史文件恢复清单。
- [项目现状与验收](docs/项目现状与验收.md)：当前规则、验证证据与重启后检查步骤。
- [配置项速查](docs/配置项速查.md)、[WebUI 使用说明](docs/群管理WebUI.md)：按群等待、配置保存和运行状态。
- [当天上下文与人工接管](docs/当天上下文与人工接管.md)、[安抚话术生成](docs/安抚话术生成.md)：链路与审计修复。
- [Windows 部署指南](快速部署指南-Windows服务器.md)、[工程交接](交接文档.md)：服务、隧道和历史记录。
