# FastGPT 微信智能客服助手 — Windows 服务器快速部署指南

本指南专为企业内网 Windows Server（2016 / 2019 / 2022 或 Windows 10/11 专业版/企业版）设计。
2026-10-16更新：本机项目位于 `D:\瀚思AI客服`，后端由NSSM服务 `FastGptAssistant` 托管，当前公网回调依赖本地Docker中的frpc转发。FastGPT可使用本机容器或外部服务，以实际配置为准；使用外部FastGPT并不免除当前frpc链路对Docker的依赖。下文 `C:\FastGpt` 为新部署示例目录，操作现有机器时替换为实际项目目录。

当前功能、生效方式和代码验证见[项目现状与验收](docs/项目现状与验收.md)。本文配置示例统一使用占位符，不能直接当作生产配置使用；凭证只在服务器本机填写。

---

## 目录
- [一、部署架构概览](#一部署架构概览)
- [二、服务器前置环境准备](#二服务器前置环境准备)
- [三、代码拉取与依赖一键安装](#三代码拉取与依赖一键安装)
- [四、生产环境配置文件 (.env)](#四生产环境配置文件-env)
- [五、初始业务数据同步](#五初始业务数据同步)
- [六、服务启动与验证](#六服务启动与验证)
- [七、生产环境后台常驻与开机自启](#七生产环境后台常驻与开机自启)
- [八、日常运维与常见排错](#八日常运维与常见排错)

---

## 一、部署架构概览

```mermaid
flowchart TD
    subgraph 云端服务
        RAG[外部 FastGPT 知识库（可选）]
        DeepSeek[DeepSeek LLM 推理接口]
        GeWeCloud[GeWe 微信网关服务]
        Lark[飞书多维表格 OpenAPI]
        FRPS[公网 frps 转发入口]
    end

    subgraph 内网 Windows 服务器 [端口: 5330]
        FastAPI[FastAPI 核心编排引擎]
        DB[(SQLite 确定性读模型\n data/read-model.db)]
        State[(会话状态库\n data/conversation-state.db)]
        Media[本地媒体资源库\n data/resources/attachments]
        FRPC[Docker frpc 容器]
        LocalRAG[本机 FastGPT 容器（可选）]
    end

    GeWeCloud -->|1. Webhook 推送回调| FRPS
    FRPS --> FRPC
    FRPC --> FastAPI
    FastAPI -->|2. 主数据/质检报告精准匹配| DB
    FastAPI -->|3. 按实际 API 地址检索| RAG
    FastAPI -->|或使用本机知识库| LocalRAG
    FastAPI -->|4. 意图拆分与多轮推理| DeepSeek
    FastAPI -->|5. 格式化纯文本及附件回复| GeWeCloud
    Lark -->|定时同步更新主数据| DB
```

---

## 二、服务器前置环境准备

### 1. 硬件与网络要求
* **硬件最低配置**：2 核 CPU / 4GB 内存 / 20GB 可用磁盘空间（轻量极简）。
* **网络连通性**：
  * 允许访问实际配置的FastGPT、模型、GeWe及飞书服务地址。
  * frp部署需允许本机frpc连接远端frps，远端回调端口映射至本地5330；无须据此将本机5330直接暴露公网。其他入口按实际网络方案配置。

### 2. 软件安装
在服务器上打开 PowerShell（管理员权限）：

#### (1) 安装 Git
前往官网下载并安装 [Git for Windows](https://git-scm.com/download/win)。

#### (2) 安装 uv（现代高性能 Python 包管理器，强烈推荐）
在 PowerShell 中执行以下官方命令一键安装 `uv`（免去繁琐的 Python 环境变量配置）：
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
> 安装后如果提示找不到 `uv`，重启 PowerShell 窗口即可。
> `uv` 会在安装项目依赖时**自动下载并隔离安装 Python 3.12**，无需手动配置系统 Python。

#### (3) 准备实际使用的容器及节点软件

当前机器需要Docker引擎和frpc容器持续运行；如使用本机FastGPT，还需其容器栈。按所选Docker部署方式准备运行环境并配置重启后的恢复；Windows服务先启动而Docker尚未就绪时，后端健康与公网回调可能出现不同状态。

AidNet主程序及其内置连接进程由运行状态页单独检测。它们与Docker中的回调frpc是两条链路，不能互相替代。

---

## 三、代码拉取与依赖一键安装

1. **获取代码**：
   在服务器上将代码放置于目标路径（例如 `C:\FastGpt`）：
   ```powershell
   git clone <你的Git仓库地址> C:\FastGpt
   cd C:\FastGpt
   ```

2. **一键同步创建虚拟环境与依赖**：
   ```powershell
   uv sync
   ```
   > 该命令会在 10~30 秒内自动下载 Python 3.12 并在根目录下创建 `.venv` 虚拟环境，完成全部依赖库的高速安装。

---

## 四、生产环境配置文件 (.env)

在项目根目录 `C:\FastGpt` 下创建或检查 `.env` 文件。请参照以下模板填写：

```ini
# ==========================================
# 1. 核心应用与服务端口
# ==========================================
APP_HOST=0.0.0.0
APP_PORT=5330
LOG_LEVEL=INFO

# ==========================================
# 2. FastGPT 云端知识库配置 (Sealos)
# ==========================================
FASTGPT_API_URL=https://fastgpt.example.com/api
FASTGPT_DATASET_ID=<知识库ID>
FASTGPT_API_KEY=<在服务器本机填写应用API密钥>
FASTGPT_ROOT_KEY=<仅确有需要时在本机填写>

# ==========================================
# 3. 核心 LLM 推理模型配置 (DeepSeek)
# ==========================================
EA_DEEPSEEK_API_KEY=<在服务器本机填写模型API密钥>
EA_DEEPSEEK_BASE_URL=https://api.deepseek.com
EA_DEEPSEEK_CHAT_MODEL=deepseek-flash

# ==========================================
# 4. 微信接入网关配置 (GeWe)
# ==========================================
GEWE_BASE_URL=http://api.geweapi.com
GEWE_TOKEN=<在服务器本机填写GeWe Token>
GEWE_APP_ID=<当前实例AppID>
BOT_WXID=<机器人wxid>

# 回调与多媒体外链公网访问根地址 (不要带末尾斜杠)
# 生产推荐使用公司域名，如：https://ai.yourcompany.com
PUBLIC_BASE_URL=https://callback.example.com
GEWE_PUBLIC_BASE_URL=https://callback.example.com
GEWE_CALLBACK_URL=https://callback.example.com/api/wechat/callback

# ==========================================
# 5. 飞书多维表格同步 (主数据读模型)
# ==========================================
EA_LARK_APP_ID=<飞书应用ID>
EA_LARK_APP_SECRET=<在服务器本机填写应用密钥>
EA_LARK_BITABLE_APP_TOKEN=<多维表格Token>
EA_LARK_BITABLE_TABLE_ID=<数据表ID>

# ==========================================
# 6. 本地数据与安全存储
# ==========================================
EA_READ_MODEL_DATABASE=data/read-model.db
EA_CONVERSATION_STATE_DB_PATH=data/conversation-state.db
EA_WECHAT_PAYLOAD_VAULT_KEY=<由本机安全生成并持久保存的保险库密钥>
```

### 检查群聊策略配置 (`config/wechat_groups.toml`)
打开 `config/wechat_groups.toml`，确保你要激活的微信群 ID 已填入列表中，例如：
```toml
[global]
enabled = true
reply_to_staff_when_mentioned = true

[[groups]]
group_id = "49195424680@chatroom"
mode = "active"
require_customer_mention = false
allow_images = true
allow_files = true
owner_id = "wxid_2i863b4r6r3y22"
# 可选：该群等待60秒；删除此项即跟随当前运行默认值
conversation_quiet_window_seconds = 60
```

也可在WebUI配置每群等待0–86400整数秒，0表示不额外等待。群策略热加载，`.env`默认等待和Python代码需重启。详见[WebUI说明](docs/群管理WebUI.md)。保险库密钥需跨重启保留，替换或丢失会影响既有加密原文和媒体的恢复。

---

## 五、初始业务数据同步

首次部署或商品数据有更新时，需要从飞书多维表格拉取商品数据、工厂信息以及质检报告附件：

```powershell
# 从飞书全量同步商品表与附件至本地 SQLite
uv run python scripts/ops.py sync-bitable
```
*同步完成后，会自动生成 `data/read-model.db` 并下载附件到 `data/resources/attachments`。*

---

## 六、服务启动与验证

### 1. 前台调试启动
先在控制台前台启动，确认无任何报错：

已有NSSM服务正在监听5330时，使用后文的服务重启流程核验，不再运行此调试命令。
```powershell
uv run python scripts/ops.py serve
```
出现 `Uvicorn running on http://0.0.0.0:5330` 及 `Application startup complete` 即表示服务启动成功。

### 2. 核心功能自测验证（在新的 PowerShell 窗口中执行）
```powershell
# 1. 验证基础健康检查 (应返回 status: healthy)
curl http://127.0.0.1:5330/health

# 2. 验证云端知识库问答
uv run python scripts/ops.py query "想买瀚思的产品，现在哪些平台有官方店？"

# 3. 验证本地主数据精确查表
uv run python scripts/ops.py query "查询商品 6976070461130 的信息与关联工厂"

# 4. 验证自动化单测全流程 (全绿灯通过)
uv run pytest tests/test_fastgpt_retrieval.py
```

---

## 七、生产环境后台常驻与开机自启

为了保证 Windows 服务器重启或注销登录后，AI 客服助手依然能在后台持续运行，推荐以下两种方式：

### 推荐方案 A：使用 NSSM 注册为 Windows 原生系统服务（最稳定、生产首选）

1. **下载 NSSM**：
   下载 [NSSM (Non-Sucking Service Manager)](https://nssm.cc/download)，将 `nssm.exe` 放置于 `C:\Windows\System32` 或项目根目录。
2. **注册服务**：
   在管理员权限 PowerShell 中执行：
   ```powershell
   # 注册服务名为 FastGptAssistant
   nssm install FastGptAssistant "C:\FastGpt\.venv\Scripts\python.exe" "-m uvicorn fastgpt_assistant.server.app:app --host 0.0.0.0 --port 5330"
   nssm set FastGptAssistant AppDirectory "C:\FastGpt"
   nssm set FastGptAssistant AppStdout "C:\FastGpt\logs\5330.out.log"
   nssm set FastGptAssistant AppStderr "C:\FastGpt\logs\5330.err.log"
   nssm set FastGptAssistant Start SERVICE_AUTO_START
   ```
3. **启动与管理**：
   ```powershell
   # 启动服务
   net start FastGptAssistant

   # 停止服务
   net stop FastGptAssistant

   # 查看服务状态
   sc.exe query FastGptAssistant
   ```
   > 优势：Windows 开机无需登录账号自动启动、崩溃自动拉起、标准服务管理。

### 现有服务更新与核验（本机）

当前只使用一个Uvicorn进程，不添加 `--workers`。在管理员PowerShell执行：

```powershell
Set-Location -LiteralPath "D:\瀚思AI客服"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/restart_app.ps1
```

脚本先只读检查未终态任务，再重启Windows服务并轮询健康接口。未终态任务可能在首次轮询继续处理；脚本不会清空线上任务。还需核对WebUI运行状态中的启动时间晚于本轮源码修改时间、调度器正常、配置运行值一致，并查看新启动日志。普通桌面会话没有服务控制权限时，需由管理员执行。

源码重启和前端部署是两步：在项目根目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_webui.ps1`，会安装锁定依赖、执行类型检查和构建，输出到 `src/fastgpt_assistant/server/admin_static`，随后刷新浏览器。已安装依赖时也可在 `webui` 运行 `pnpm build`。只有代码测试或 `/health` 正常不足以证明新UI和新业务逻辑都已加载。当前名片补修的现场确认状态见[项目现状与验收](docs/项目现状与验收.md)。

---

### 方案 B：使用 PowerShell 脚本后台启动

仅适用于没有注册Windows服务的独立部署。现有NSSM托管机器不要混用以下启停脚本，以免被服务自动拉起或误停其他进程：

**启动服务脚本 (`scripts/start_server.ps1`)**：
```powershell
# 确保 logs 目录存在
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

# 杀掉可能占用的 5330 旧进程
Get-NetTCPConnection -LocalPort 5330 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }

# 静默后台启动
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","fastgpt_assistant.server.app:app","--host","0.0.0.0","--port","5330" `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "logs\5330.out.log" -RedirectStandardError "logs\5330.err.log"

Write-Host "FastGPT Assistant 已在后台启动，端口: 5330"
```

**停止服务脚本 (`scripts/stop_server.ps1`)**：
```powershell
Get-NetTCPConnection -LocalPort 5330 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force
    Write-Host "已停止监听端口 5330 的进程 (PID: $($_.OwningProcess))"
}
```

---

## 八、日常运维与常见排错

### 1. 微信群内 @机器人 无响应
* **检查 1：GeWe 账号是否在线**
  在项目根目录运行单测或快速验证命令，确认 GeWe 登录态是否有效。
* **检查 2：回调地址是否可通**
  先在WebUI运行状态检查Docker引擎及frpc，再在GeWe检测实际注册回调。`Failed to connect to /主机:端口`表示连接阶段失败，尚未进入需求或模型逻辑，应核对远端端口、frps映射、本机frpc及后端监听。公网 `/health` 可辅助检查，但不能替代实际POST回调检测。
* **检查 3：白名单限制**
  检查 `config/wechat_groups.toml` 是否已将当前群聊的 `group_id` 纳入配置。

### 2. 知识库检索失败或返回转人工
* 检查云端 FastGPT 服务是否可用，登录 `https://fastgpt-uaqixmiy.sealosbja.site` 确认服务运行正常。
* 检查 `.env` 中的 `FASTGPT_API_KEY` 是否有效。

### 本地状态显示异常

WebUI“未找到Docker命令”表示客服服务找不到本机CLI，不等同于桌面Docker一定未打开。检测已兼容系统及用户安装位置；若源码已更新而页面仍显示旧提示，先核对服务是否重启加载，再检查服务账户权限。Docker运行、容器健康及AidNet进程存在都不能单独证明外部业务连接正常。

### 3. 日志排查
所有运行时日志均输出至 `logs/` 目录：
* `logs/5330.out.log`：常规标准访问与查询调度日志
* `logs/5330.err.log`：错误与异常堆栈追踪
