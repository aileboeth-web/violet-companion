# Violet 的私有 SearXNG（Phase 9.3）

Violet 的 `web_search` 默认用自建的开源 SearXNG（`VIOLET_WEB_SEARCH_PROVIDER=searxng`），不依赖付费搜索 API（博查 / Brave 的代码保留为可选实现，不是默认来源）。这个目录是 SearXNG 在 Linux 服务器上的 Docker Compose 部署，**只在你明确执行 `./searxng.sh up` 时**才会拉镜像、启动容器。Violet、`start.sh`、`git pull` 都不会运行它，也不会安装 Docker（仓库里没有 git 钩子）。

**采用的方式（用户决定）：Violet 不容器化，照旧用 `start.sh` 跑在服务器上；SearXNG 跑在 Docker 里，只通过宿主机 `127.0.0.1:18089` 提供搜索。**

## 文件

| 文件 | 作用 | 在 git 里 |
|---|---|---|
| `compose.yaml` | 服务定义：官方镜像 `docker.io/searxng/searxng`，容器 `violet-searxng`，以 977:977 运行、不带任何 capability，端口只发布到 `127.0.0.1` | 是 |
| `config/settings.yml` | SearXNG 设置（只读挂载）：打开 JSON 输出、私有实例（关闭 limiter、非公开）、超时短于 Violet 的 10 秒 | 是 |
| `env_example` | `.env` 模板（没有密钥） | 是 |
| `searxng.sh` | 管理脚本：init / check / up / upgrade / down / restart / status / logs / test | 是 |
| `validate_config.py` | 静态检查上面三个文件（python3 + PyYAML；不联网、不调用 Docker） | 是 |
| `.env` | 真实配置：随机 `SEARXNG_SECRET`、端口、镜像（`init` 生成，权限 600） | **否** |
| `cache/` | SearXNG 缓存（持久化，属于 977:977） | 否 |
| `state/image-digests.log` | 每次 up / upgrade 用的镜像 digest（回滚用） | 否 |

## 网络设计

```
Violet（start.sh，宿主机）──http://127.0.0.1:18089──> violet-searxng 容器 :8080 ──> 互联网上的搜索引擎
```

- **不对外开放**：唯一发布的端口绑定在 `127.0.0.1`，公网和局域网都连不到；不加入 1Panel 的网络，不需要 OpenResty 反代。
- **不冲突**：独立的项目名、容器名、网络名（`violet-searxng`），端口默认 18089（`.env` 可改；`check` 会检查是否被占用）。不用 PostgreSQL；不需要 Redis / Valkey（limiter 关闭），所以不会碰已有的 Redis。
- **网络不能设为 internal**：SearXNG 要访问外部搜索引擎。
- **Violet 这边的安全策略**（`ai/web`）：`VIOLET_WEB_SEARXNG_URL` 是唯一受信任的内部地址——只有搜索请求能去、不跟随跳转；`fetch_url` 永远不能访问它（本机、内网、Docker 地址照样全部拒绝，SSRF 防护不变）。明文 http 只允许本机 / 内网 IP / Docker 服务名，其他主机必须 https；地址里不能带用户名密码、参数。地址不可用时后台“工具”页的联网卡片会写明原因。
- **以后如果 Violet 也放进 Docker（现在不做）**：让 Violet 的容器加入外部网络 `violet-searxng`（compose 里 `networks: { violet-searxng: { external: true } }`），把 `VIOLET_WEB_SEARXNG_URL` 改成 `http://searxng:8080`（网络别名已经配好），就可以去掉 `127.0.0.1:18089` 的端口发布。

## 与官方镜像核对过的配置（2026-10-24，按 docs.searxng.org 和 searxng 仓库 container/ 目录）

| 项 | 官方镜像 | 这里的做法 |
|---|---|---|
| 密钥 | `server.secret_key` 可由 `SEARXNG_SECRET` 覆盖 | 只放在 `.env`（随机 64 位十六进制）；settings.yml 里是占位符 |
| 其他变量 | `SEARXNG_BASE_URL`、`SEARXNG_LIMITER`、`SEARXNG_PUBLIC_INSTANCE` | 都设置了；limiter / public 关闭 |
| 挂载 | 配置 `/etc/searxng`，数据 `/var/cache/searxng` | `./config`（只读）、`./cache`（读写） |
| 端口 | 容器内 8080（Granian：`GRANIAN_HOST` 默认 `::`） | `GRANIAN_HOST=0.0.0.0`（IPv4，任何 Docker 网络都可用），发布到 `127.0.0.1:18089` |
| 运行用户 | 镜像没有 `USER`（默认 root），应用文件属于 977:977 | `user: "977:977"`，`cap_drop: [ALL]`，`no-new-privileges` |
| 目录属主 | 入口脚本 `FORCE_OWNERSHIP`（默认 true）会以 root 递归 chown 挂载目录 | `FORCE_OWNERSHIP=false`：不改 git 里的 `config/`；`init` 只把 `cache/` 交给 977 |
| settings.yml | 不存在时入口脚本复制模板并生成密钥；存在时不改 | 我们的文件已存在，所以只读挂载可行 |
| JSON API | `search.formats` 要包含 `json`，否则 `/search?format=json` 返回 403 | 已包含 |
| 健康检查 | 镜像没有 HEALTHCHECK，也没有 wget / curl；Python 在 `/usr/local/searxng/.venv/bin/python` | 用这个 Python 请求 `http://127.0.0.1:8080/healthz` |

## 正式部署（等项目上传 GitHub、服务器拉取代码后，由你手动执行）

前提：服务器已经装好 Docker 和 compose v2 插件（本脚本不安装；按 Docker 官方文档或 1Panel 安装），用 root（或 sudo）执行。

```bash
cd /opt/violet/deploy/searxng      # 按服务器上 Violet 的实际位置
chmod +x searxng.sh                      # git 里已经是可执行；拉取方式丢了权限时才需要
./searxng.sh init                        # 生成 .env（随机密钥）、cache/（交给 977:977）和 state/
./searxng.sh check                       # 只检查：Docker、.env、目录属主、配置、compose 语法、端口 / 名称冲突
./searxng.sh up                          # 正式部署：再检查一次，输入 yes 后拉镜像、启动、健康检查、试搜一次
```

然后在 Violet 的 `.env`：

```
VIOLET_WEB_ENABLED=1
VIOLET_WEB_SEARCH_PROVIDER=searxng
VIOLET_WEB_SEARXNG_URL=http://127.0.0.1:18089
```

重启 Violet（`start.sh`）。后台 `/control` 的“工具”页应显示“搜索服务 searxng · 搜索 可用”。

## 日常

| 做什么 | 命令 |
|---|---|
| 看状态 / 健康 | `./searxng.sh status` |
| 看日志 | `./searxng.sh logs`，持续看 `./searxng.sh logs -f` |
| 试搜一次（从服务器本机） | `./searxng.sh test` |
| 停 / 启 | `./searxng.sh down`，`./searxng.sh up` |
| 重启 | `./searxng.sh restart` |
| 升级 | `./searxng.sh upgrade`（先记录当前镜像，再拉新版、重建；配置和缓存保留） |
| 回滚 | 把 `state/image-digests.log` 里 `before-upgrade` 行的 digest 写进 `.env`：`SEARXNG_IMAGE=docker.io/searxng/searxng@sha256:…`，再 `./searxng.sh up` |
| 改设置 | 改 `config/settings.yml`，再 `./searxng.sh restart` |

## 第一次部署时仍要看一眼的事（本地无法运行镜像）

- `./searxng.sh logs`：以 977 运行时入口脚本会提示没有 root、不改属主（预期）；不应有 `cache/` 写入失败。
- `./searxng.sh status` 显示 healthy；`./searxng.sh test` 能拿到 JSON 结果。
- 从国内服务器能连上哪些默认引擎；连不上的会出现在 `test` 的“没有响应的引擎”里，可按 `settings.yml` 末尾的说明只保留能用的引擎。
- 镜像 `latest` 会随官方更新；官方的变量名或入口脚本将来若有变化，以 `logs` 为准，需要时用 digest 固定版本（见回滚）。
