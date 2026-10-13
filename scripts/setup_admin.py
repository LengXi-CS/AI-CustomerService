"""Set/reset the local WebUI admin password without putting it in argv or logs."""

from __future__ import annotations

import getpass

from fastgpt_assistant.core.admin_auth import configure_admin
from fastgpt_assistant.core.config import settings


def main() -> int:
    print("设置群管理后台密码（至少 6 个字符；输入不会显示）。")
    password = getpass.getpass("新密码：")
    if password != getpass.getpass("再次输入："):
        print("两次输入不一致，未修改管理员凭证。")
        return 1
    try:
        configure_admin(settings.admin_auth_path, password)
    except (ValueError, OSError) as exc:
        print(f"设置失败：{exc}")
        return 1
    print("管理员密码已设置，旧会话将失效。访问 http://127.0.0.1:5330/admin/ 登录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
