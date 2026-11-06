# Deploying SearXNG with Docker (alternative way)

> **You usually don't need this page.** If you installed Violet with the one-line command, click **Install web search** (一键安装联网搜索) under **Tools & phone → Web tools** (工具与手机 → 联网工具) in the control panel (or run `/opt/violet/violet searxng install` on the server). That way needs no Docker, registers a system service `violet-searxng`, and listens only on 127.0.0.1. This page is only for people without systemd, or who prefer Docker.

Violet's `web_search` uses a self-hosted, open-source SearXNG by default (`VIOLET_WEB_SEARCH_PROVIDER=searxng`) and does not rely on paid search APIs (the Bocha / Brave code is kept as an optional implementation, not the default source). This directory is a Docker Compose deployment of SearXNG. It pulls images and starts containers **only when you explicitly run `./searxng.sh up`**. Violet itself never runs it and never installs Docker.

In this setup Violet keeps running on the server as usual (not in a container), SearXNG runs in Docker, and search is offered only on the host's `127.0.0.1:18089`.

## Files

| File | Purpose | In git |
|---|---|---|
| `compose.yaml` | Service definition: official image `docker.io/searxng/searxng`, container `violet-searxng`, runs as 977:977 with no capabilities, port published only on `127.0.0.1` | yes |
| `config/settings.yml` | SearXNG settings (mounted read-only): JSON output on, private instance (limiter off, not public), timeouts shorter than Violet's 10 seconds | yes |
| `env_example` | `.env` template (no secrets) | yes |
| `searxng.sh` | Management script: init / check / up / upgrade / down / restart / status / logs / test | yes |
| `validate_config.py` | Static check of the three files above (python3 + PyYAML; no network, no Docker calls) | yes |
| `.env` | Real configuration: random `SEARXNG_SECRET`, port, image (created by `init`, mode 600) | **no** |
| `cache/` | SearXNG cache (persistent, owned by 977:977) | no |
| `state/image-digests.log` | The image digest used by each up / upgrade (for rollback) | no |

## Network design

```
Violet (start.sh, host) ──http://127.0.0.1:18089──> violet-searxng container :8080 ──> search engines on the internet
```

- **Not exposed**: the only published port is bound to `127.0.0.1`, so neither the internet nor the LAN can reach it. It does not join 1Panel's network and needs no OpenResty reverse proxy.
- **No conflicts**: its own project, container and network name (`violet-searxng`); default port 18089 (changeable in `.env`; `check` tests whether it is taken). No PostgreSQL; no Redis / Valkey needed (limiter off), so an existing Redis is never touched.
- **The network cannot be internal**: SearXNG has to reach external search engines.
- **Violet's security policy** (`ai/web`): `VIOLET_WEB_SEARXNG_URL` is the only trusted internal address. Only search requests go there, and redirects are not followed. `fetch_url` can never reach it (local, private and Docker addresses are still all refused; SSRF protection is unchanged). Plain http is allowed only for localhost / private IPs / Docker service names; every other host must use https. The address may not contain a username, password or query parameters. When the address is unusable, the web card on the panel's **Tools** page says why.
- **If Violet also moves into Docker later (not done now)**: have Violet's container join the external network `violet-searxng` (in compose: `networks: { violet-searxng: { external: true } }`), change `VIOLET_WEB_SEARXNG_URL` to `http://searxng:8080` (the network alias is already set up), and the `127.0.0.1:18089` port publishing can be removed.

## Configuration checked against the official image (2026-10-24, per docs.searxng.org and the searxng repository's container/ directory)

| Item | Official image | What we do here |
|---|---|---|
| Secret | `server.secret_key` can be overridden by `SEARXNG_SECRET` | Only in `.env` (random 64 hex characters); settings.yml holds a placeholder |
| Other variables | `SEARXNG_BASE_URL`, `SEARXNG_LIMITER`, `SEARXNG_PUBLIC_INSTANCE` | All set; limiter / public off |
| Mounts | Config `/etc/searxng`, data `/var/cache/searxng` | `./config` (read-only), `./cache` (read-write) |
| Port | 8080 inside the container (Granian: `GRANIAN_HOST` defaults to `::`) | `GRANIAN_HOST=0.0.0.0` (IPv4, works on any Docker network), published on `127.0.0.1:18089` |
| Run user | The image has no `USER` (root by default); app files belong to 977:977 | `user: "977:977"`, `cap_drop: [ALL]`, `no-new-privileges` |
| Directory ownership | The entrypoint's `FORCE_OWNERSHIP` (default true) recursively chowns the mounts as root | `FORCE_OWNERSHIP=false`: the `config/` in git is left alone; `init` hands only `cache/` to 977 |
| settings.yml | When missing, the entrypoint copies a template and generates a secret; when present, it is left alone | Our file exists, so a read-only mount works |
| JSON API | `search.formats` must include `json`, otherwise `/search?format=json` returns 403 | Included |
| Health check | The image has no HEALTHCHECK and no wget / curl; Python is at `/usr/local/searxng/.venv/bin/python` | That Python requests `http://127.0.0.1:8080/healthz` |

## Deploying (run by hand on the server)

Prerequisite: Docker and the compose v2 plugin are already installed on the server (this script does not install them; follow the official Docker docs or install through 1Panel). Run as root (or with sudo).

```bash
cd /opt/violet/deploy/searxng      # wherever Violet actually is on your server
chmod +x searxng.sh                      # already executable in git; only needed if the copy lost the mode
./searxng.sh init                        # creates .env (random secret), cache/ (owned by 977:977) and state/
./searxng.sh check                       # checks only: Docker, .env, directory owners, config, compose syntax, port / name conflicts
./searxng.sh up                          # deploys: checks again, then after you type yes pulls the image, starts it, runs a health check and one test search
```

Then in Violet's `.env`:

```
VIOLET_WEB_ENABLED=1
VIOLET_WEB_SEARCH_PROVIDER=searxng
VIOLET_WEB_SEARXNG_URL=http://127.0.0.1:18089
```

Restart Violet (`start.sh`). The **Tools** (工具) page of `/control` should show "搜索服务 searxng · 搜索 可用" (search service searxng · search available).

## Day to day

| Task | Command |
|---|---|
| Status / health | `./searxng.sh status` |
| Logs | `./searxng.sh logs`, follow with `./searxng.sh logs -f` |
| One test search (from the server itself) | `./searxng.sh test` |
| Stop / start | `./searxng.sh down`, `./searxng.sh up` |
| Restart | `./searxng.sh restart` |
| Upgrade | `./searxng.sh upgrade` (records the current image first, then pulls the new one and recreates; config and cache are kept) |
| Roll back | Put the digest from the `before-upgrade` line of `state/image-digests.log` into `.env` as `SEARXNG_IMAGE=docker.io/searxng/searxng@sha256:…`, then `./searxng.sh up` |
| Change settings | Edit `config/settings.yml`, then `./searxng.sh restart` |

## Worth a look on the first deployment (the image could not be run locally)

- `./searxng.sh logs`: when running as 977, the entrypoint says it is not root and will not change owners (expected); there should be no write failures for `cache/`.
- `./searxng.sh status` shows healthy; `./searxng.sh test` gets JSON results.
- Which default engines are reachable from a server in mainland China; unreachable ones show up under "engines with no response" (没有响应的引擎) in `test`, and you can keep only the working ones as described at the end of `settings.yml`.
- The `latest` image follows official updates. If the official variable names or entrypoint change in the future, trust `logs`, and pin a version by digest when needed (see Roll back).
