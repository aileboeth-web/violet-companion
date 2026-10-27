"""Initialise an Violet installation (phase 10.4). Called by the installer after the program is in place; safe to run
again (it only creates what is missing):

    python -m tools.violet_setup init [--json] [--reset-admin]
    python -m tools.violet_setup account info|reset-password|reset-entrance [--entrance X | --no-entrance] [--json]

  * the database: created with every table and migration (VioletDatabase does that on open);
  * the admin password: generated once (20 random characters), stored only as a scrypt hash with "must change at
    first login"; printed ONCE in the JSON result. An existing password is kept (unless --reset-admin);
  * a new install also gets a random username and security entrance (wx_ilink_py.auth, Violet stage 2);
  * the tuned Chat / Proactive prompt versions are selected in the Control Center settings when nothing chose a
    prompt yet (a fresh install has no .env with the prompt texts -- without this it would run the short code
    defaults);
  * nothing about the model service, channels or tools: those are set in the Control Center.

The last line of output is one JSON object (the installer reads it).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys

DEFAULT_PROMPTS = {"prompt.chat": "v1", "prompt.proactive": "v1"}     # the stable versions (docs: decisions T23)


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value):
            return value


def init(reset_admin: bool = False) -> dict:
    from ai.settings import ENV_PATH
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    data_dir = os.environ.get("VIOLET_DATA_DIR") or "./data"
    result: dict = {"ok": True, "data_dir": os.path.abspath(data_dir)}

    from database import VioletDatabase
    db = VioletDatabase()
    try:
        result["database"] = str(db.db_path)
    finally:
        db.close()

    from wx_ilink_py.auth import AdminStore, generate_username
    store = AdminStore(data_dir)
    fresh = not store.has_password()
    if fresh and not store.username():
        # Violet stage 2: a new install gets a random username and security entrance (1Panel style). An existing
        # install keeps what it has -- adding them on upgrade would lock out an admin who does not know them yet
        store.set_username(generate_username())
    if fresh and not store.entrance():
        store.set_entrance(None)
    if reset_admin or fresh:
        password = generate_password()
        store.set_password(password, must_change=True)
        result["admin"] = {"user": store.username() or "admin", "password": password, "must_change": True,
                           "created": True}
    else:
        result["admin"] = {"user": store.username() or "admin", "created": False,
                           "must_change": bool(store.load().get("must_change"))}
    result["admin"]["entrance"] = store.entrance()

    from ai.control_config import ControlStore, prompt_versions
    control = ControlStore(data_dir)
    values = control.values()
    seed = {}
    for key, version in DEFAULT_PROMPTS.items():
        kind = key.split(".")[1]
        env_key = f"{kind.upper()}_LLM_PROMPT"
        if key not in values and not os.environ.get(env_key, "").strip() and version in prompt_versions(kind):
            seed[key] = version
    if seed:
        control.save(seed, "installer", "安装时选择稳定的提示词版本")
    result["prompts_selected"] = seed
    return result


def account(action: str, entrance: str | None = None) -> dict:
    """The admin account for `violet info | reset-password | reset-entrance` (Violet stage 2). Never prints a stored
    secret: a password appears only when reset-password has just made it.

      info            username, security entrance, password state
      reset-password  a new random initial password (must be changed at the next login); ends every session
      reset-entrance  a new random entrance, or ``entrance`` ("" removes it -- every path reachable again)
    """
    from ai.settings import ENV_PATH
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    from wx_ilink_py.auth import AdminStore
    store = AdminStore(os.environ.get("VIOLET_DATA_DIR") or "./data")
    result: dict = {"ok": True}
    if action == "reset-password":
        password = generate_password()
        store.set_password(password, must_change=True)
        result["password"] = password
    elif action == "reset-entrance":
        store.set_entrance(entrance)
    data = store.load()
    result.update(user=store.username() or "admin", username_set=bool(store.username()), entrance=store.entrance(),
                  password_set=bool(data.get("password_hash")), must_change=bool(data.get("must_change")))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.violet_setup")
    parser.add_argument("command", choices=["init", "account"])
    parser.add_argument("action", nargs="?", choices=["info", "reset-password", "reset-entrance"], default="info")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--reset-admin", action="store_true")
    parser.add_argument("--entrance", help="reset-entrance: this value instead of a random one")
    parser.add_argument("--no-entrance", action="store_true", help="reset-entrance: remove the security entrance")
    args = parser.parse_args(argv)
    try:
        if args.command == "account":
            result = account(args.action, "" if args.no_entrance else args.entrance)
        else:
            result = init(reset_admin=args.reset_admin)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
