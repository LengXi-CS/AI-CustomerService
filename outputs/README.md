# 当前产物

2026-10-16整理后，本目录保留当前文档直接引用的最终证据、可复用验证脚本及已有业务交付物。

- 最新完整代码回归686项通过：[测试输出](contact-card-tests-final.txt)、[JUnit结果](contact-card-tests-final.xml)。
- 名片原回调离线验证：[结果](contact-card-replay.json)、[回放脚本](replay_contact_cards.py)。
- 每群等待阶段验证：[测试输出](group-wait-tests.txt)、[JUnit结果](group-wait-tests.xml)、[桌面截图](group-waits-desktop.png)、[手机截图](group-waits-mobile.png)、[模拟浏览器脚本](verify_group_wait_ui.cjs)。
- `20261015-customer-intake/`：已有客户信息收集表及支撑资料，完整保留，不作为临时测试输出处理。

脚本从项目根目录调用；其模型或原归档读取行为以脚本实现为准，不因名称含“回放”就认定为不访问任何现场数据。历史分轮审计、旧基线及重复输出已移至[归档](../archive/2026-10-16/README.md)。

当前验证状态及现场验收要求见[项目现状与验收](../docs/项目现状与验收.md)。文档链接指向本机现有产物，不等于这些验证已在正式服务执行。
