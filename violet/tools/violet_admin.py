"""Violet Control Center administrator (phase 9.0). Run from the Violet folder:

    python -m tools.violet_admin set-password      set or replace the admin password (asked twice, never echoed)
    python -m tools.violet_admin logout-all        end every dashboard session
    python -m tools.violet_admin status            password set or not, token state; nothing secret is printed
    python -m tools.violet_admin token-disable     the admin token (VIOLET_ADMIN_TOKEN / admin_token file) stops working
                                                for login and Bearer at once; needs a password first
    python -m tools.violet_admin token-rotate      a new token replaces the old one at once (printed once, only its hash
                                                is stored); update scripts that use Bearer
    python -m tools.violet_admin token-enable      back to VIOLET_ADMIN_TOKEN / the admin_token file

Every token change and a new password end all dashboard sessions. No restart is needed.

The password is stored only as a scrypt hash in <VIOLET_DATA_DIR>/control/admin.json. Setting it ends every existing
session. There is no default password and no way to read it back. Uses VIOLET_DATA_DIR (default ./data), as Violet does.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime


def _store():
    from dotenv import load_dotenv

    from ai.settings import ENV_PATH
    load_dotenv(ENV_PATH, override=False)
    from wx_ilink_py.auth import AdminStore
    return AdminStore(os.environ.get("VIOLET_DATA_DIR") or "./data")


def main(argv: list[str] | None = None, read_secret=getpass.getpass) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.violet_admin", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["set-password", "logout-all", "status", "token-disable", "token-rotate",
                                            "token-enable"])
    args = parser.parse_args(argv)
    store = _store()
    if args.command == "status":
        data = store.load()
        at = data.get("password_set_at")
        print(f"数据目录: {store.dir}")
        print("管理员密码: " + (f"已设置（{datetime.fromtimestamp(at):%Y-%m-%d %H:%M}）" if data.get("password_hash")
                                else "未设置（只能用管理令牌登录）"))
        print("管理令牌: " + {"default": "使用 VIOLET_ADMIN_TOKEN / admin_token 文件", "rotated": "已更换（只保存哈希）",
                              "disabled": "已禁用"}[store.token_mode()])
        return 0
    if args.command == "token-disable":
        try:
            store.disable_token()
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print("管理令牌已禁用（登录和 Bearer 都不再接受）；所有后台登录已失效。")
        return 0
    if args.command == "token-rotate":
        new = store.rotate_token()
        print("新的管理令牌（只显示这一次，请保存好；旧令牌已失效，所有后台登录已失效）:")
        print(new)
        return 0
    if args.command == "token-enable":
        store.enable_default_token()
        print("已恢复使用 VIOLET_ADMIN_TOKEN / admin_token 文件里的令牌；所有后台登录已失效。")
        return 0
    if args.command == "logout-all":
        store.end_all_sessions()
        print("所有后台登录已失效。")
        return 0
    from wx_ilink_py.auth import password_problem
    first = read_secret("新的管理员密码（至少 12 个字符）: ")
    problem = password_problem(first)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    if read_secret("再输一次: ") != first:
        print("两次输入不一致，没有修改。", file=sys.stderr)
        return 2
    store.set_password(first)
    print(f"已设置。哈希保存在 {store.path}；原有的后台登录都已失效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
