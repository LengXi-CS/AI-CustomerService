# 运维与验证脚本

在项目根目录运行。Windows服务已监听5330时，使用重启脚本更新，不另起调试服务。

| 用途 | 脚本 |
|---|---|
| 重启与任务检查 | `restart_app.ps1`、`check_pending_tasks.py` |
| 管理页构建与管理员初始化 | `build_webui.ps1`、`setup_admin.py` |
| 部署及局域网访问 | `setup_frpc.ps1`、`configure_lan_access.py`、`enable_lan_access.ps1` |
| 统一命令、数据同步与知识库导入 | `ops.py`、`sync_bitable.py`、`run_sync_bitable.ps1`、`import_fastgpt_docs.py` |
| 查询及状态排查 | `smoke_query.py`、`check_data_status.py`、`show_chat_log.py`、`show_quote_template.py` |
| 需求关联及原生引用验证 | `check_issue_association.py`、`test_native_quote.py` |
| 代表性业务回归 | `regression_representative_queries.py`、`test_dataset_queries.py` |
| 历史cpolar部署或独立进程部署 | `get_public_url.py`、`set_public_base_url.py`、`start_server.ps1`、`stop_server.ps1` |

其他具名配置工具继续保留，调用前查看各自参数说明。验证脚本可能调用模型；`test_native_quote.py` 加 `--send` 会实际外发，普通只读检查不要附加该参数。cpolar查询脚本不能发现当前frp映射；独立进程启停脚本不与本机NSSM服务混用。

以下划线开头的12个一次性脚本及14份日志已移到[日期归档](../archive/2026-10-16/README.md)。归档脚本含旧机器或现场假设，不是当前运维入口；需要复用时先恢复原路径并核对用途。
