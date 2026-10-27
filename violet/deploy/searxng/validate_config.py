"""Static checks of deploy/searxng (Violet phase 9.3): compose.yaml, config/settings.yml, env_example.

    python3 validate_config.py [dir]      exit 0 = fine, 1 = problems (printed); needs PyYAML

No Docker, no network: it reads the files only. "./searxng.sh check" runs it when python3 + PyYAML are there;
the real syntax check on the server is "docker compose config" (also in check). Imported by the offline tests."""
from __future__ import annotations

import re
import sys
from pathlib import Path

OFFICIAL_IMAGE = re.compile(r"^(docker\.io/)?searxng/searxng([:@].+)?$")
PLACEHOLDER = "set-by-SEARXNG_SECRET-environment-variable"


def _default(value: str) -> str:
    """'${VAR:-default}' -> 'default' (what compose uses when VAR is unset); anything else unchanged."""
    m = re.fullmatch(r"\$\{[A-Z0-9_]+:-([^}]*)\}", str(value))
    return m.group(1) if m else str(value)


def validate(folder: str | Path = Path(__file__).resolve().parent) -> list[str]:
    import yaml
    folder = Path(folder)
    problems: list[str] = []
    compose = yaml.safe_load((folder / "compose.yaml").read_text(encoding="utf-8"))
    settings = yaml.safe_load((folder / "config" / "settings.yml").read_text(encoding="utf-8"))
    env_lines = (folder / "env_example").read_text(encoding="utf-8").splitlines()

    # -- compose: one service, official image, no public port, own network, hardened, persistent dirs
    if compose.get("name") != "violet-searxng":
        problems.append("compose: project name must be violet-searxng")
    services = compose.get("services") or {}
    if set(services) != {"searxng"}:
        problems.append(f"compose: exactly one service 'searxng' expected, got {sorted(services)}")
    s = services.get("searxng") or {}
    if not OFFICIAL_IMAGE.match(_default(s.get("image", ""))):
        problems.append(f"compose: image must be the official searxng/searxng, got {s.get('image')}")
    if s.get("container_name") != "violet-searxng":
        problems.append("compose: container_name must be violet-searxng")
    for port in s.get("ports") or []:
        text = str(port)
        if not text.startswith("127.0.0.1:"):
            problems.append(f"compose: port {text} is not bound to 127.0.0.1 (would be reachable from outside)")
        if not text.endswith(":8080"):
            problems.append(f"compose: port {text} must map to the container's 8080")
    for key in ("network_mode", "privileged", "pid", "ipc"):
        if key in s:
            problems.append(f"compose: {key} is not allowed")
    if (s.get("cap_drop") or []) != ["ALL"]:
        problems.append("compose: cap_drop must be [ALL]")
    if s.get("cap_add"):
        problems.append(f"compose: no capabilities are needed (runs as 977:977), got cap_add {s.get('cap_add')}")
    if str(s.get("user")) != "977:977":
        problems.append("compose: user must be 977:977 (the official image has no USER and would run as root)")
    if "no-new-privileges:true" not in (s.get("security_opt") or []):
        problems.append("compose: security_opt no-new-privileges:true missing")
    volumes = [str(v) for v in s.get("volumes") or []]
    for target, mode in (("/etc/searxng", "ro"), ("/var/cache/searxng", "rw")):
        mounts = [v for v in volumes if v.split(":")[1:2] == [target]]
        if not mounts or not mounts[0].startswith("./"):
            problems.append(f"compose: {target} must be a bind mount of a folder next to compose.yaml")
        elif mounts[0].split(":")[2:3] != [mode]:
            problems.append(f"compose: {target} must be mounted {mode}")
    env = s.get("environment") or {}
    if not isinstance(env, dict):
        problems.append("compose: environment must be a mapping")
        env = {}
    if not str(env.get("SEARXNG_SECRET", "")).startswith("${SEARXNG_SECRET:?"):
        problems.append("compose: SEARXNG_SECRET must come from .env and be required (${SEARXNG_SECRET:?...})")
    if str(env.get("SEARXNG_LIMITER", "")).lower() != "false" or str(env.get("SEARXNG_PUBLIC_INSTANCE", "")).lower() != "false":
        problems.append("compose: SEARXNG_LIMITER and SEARXNG_PUBLIC_INSTANCE must be false (private instance)")
    if str(env.get("FORCE_OWNERSHIP", "")).lower() != "false":
        problems.append("compose: FORCE_OWNERSHIP must be false (the entrypoint would chown the git-tracked config/)")
    if str(env.get("GRANIAN_HOST", "")) != "0.0.0.0" or str(env.get("GRANIAN_PORT", "")) != "8080":
        problems.append("compose: GRANIAN_HOST 0.0.0.0 and GRANIAN_PORT 8080 expected")
    nets = s.get("networks") or {}
    if "violet-searxng" not in nets or "searxng" not in ((nets.get("violet-searxng") or {}).get("aliases") or []):
        problems.append("compose: the service must be on network violet-searxng with alias searxng")
    top = (compose.get("networks") or {}).get("violet-searxng") or {}
    if top.get("name") != "violet-searxng" or top.get("external"):
        problems.append("compose: network violet-searxng must be created by this project under that exact name")
    if top.get("internal"):
        problems.append("compose: the network cannot be internal (SearXNG must reach the search engines)")
    if s.get("restart") not in ("unless-stopped", "always"):
        problems.append("compose: restart policy missing")
    if ((s.get("logging") or {}).get("options") or {}).get("max-size") is None:
        problems.append("compose: log size limit missing")
    health = " ".join(map(str, ((s.get("healthcheck") or {}).get("test") or [])))
    if "/usr/local/searxng/.venv/bin/python" not in health or "/healthz" not in health:
        problems.append("compose: healthcheck must call /healthz with the image's own Python (no wget / curl in it)")

    # -- settings: JSON on, private, no real secret in git
    if settings.get("use_default_settings") is not True:
        problems.append("settings: use_default_settings must be true")
    server = settings.get("server") or {}
    if server.get("secret_key") != PLACEHOLDER:
        problems.append("settings: server.secret_key must stay the placeholder (the secret comes from SEARXNG_SECRET)")
    if server.get("limiter") is not False or server.get("public_instance") is not False:
        problems.append("settings: server.limiter and server.public_instance must be false")
    if "json" not in ((settings.get("search") or {}).get("formats") or []):
        problems.append("settings: search.formats must include json (Violet uses /search?format=json)")
    timeout = (settings.get("outgoing") or {}).get("max_request_timeout")
    if not isinstance(timeout, (int, float)) or timeout >= 10:
        problems.append("settings: outgoing.max_request_timeout must be below Violet's 10 s search timeout")

    # -- env_example: a template without a secret
    values = dict(line.split("=", 1) for line in env_lines if "=" in line and not line.lstrip().startswith("#"))
    if values.get("SEARXNG_SECRET", None) != "":
        problems.append("env_example: SEARXNG_SECRET must be present and empty")
    port = values.get("SEARXNG_HOST_PORT", "")
    if not port.isdigit() or not 1024 < int(port) < 65536:
        problems.append("env_example: SEARXNG_HOST_PORT must be a port above 1024")
    if not OFFICIAL_IMAGE.match(values.get("SEARXNG_IMAGE", "")):
        problems.append("env_example: SEARXNG_IMAGE must be the official image")
    return problems


def main(argv: list[str]) -> int:
    problems = validate(argv[1] if len(argv) > 1 else Path(__file__).resolve().parent)
    for p in problems:
        print("✗ " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
