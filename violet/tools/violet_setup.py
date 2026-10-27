"""Initialise an Violet installation (phase 10.4). Called by the installer after the program is in place; safe to run
again (it only creates what is missing):

    python -m tools.violet_setup init [--json] [--reset-admin]

  * the database: created with every table and migration (VioletDatabase does that on open);
  * the admin password: generated once (20 random characters), stored only as a scrypt hash with "must change at
    first login"; printed ONCE in the JSON result. An existing password is kept (unless --reset-admin);
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

    from wx_ilink_py.auth import AdminStore
    store = AdminStore(data_dir)
    if reset_admin or not store.has_password():
        password = generate_password()
        store.set_password(password, must_change=True)
        result["admin"] = {"user": "admin", "password": password, "must_change": True, "created": True}
    else:
        result["admin"] = {"user": "admin", "created": False, "must_change": bool(store.load().get("must_change"))}

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.violet_setup")
    parser.add_argument("command", choices=["init"])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--reset-admin", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = init(reset_admin=args.reset_admin)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
