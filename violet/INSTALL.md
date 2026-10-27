# 安装 Violet

## 一条命令安装

在一台全新的 Linux 服务器上（不需要先下载源码、装 git 或配置 Python）：

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash
```

各种 Linux 都用这一条：以 root 登录的（例如 Debian 最小安装，没有 sudo）直接安装；普通用户会自动通过 sudo 重新运行（输入你的密码）；普通用户而且没有 sudo 的，会提示先执行 `su -` 切换到 root。下面的 `/opt/violet/violet …` 命令也是一样，不需要自己加 sudo。

装的是 GitHub Releases 上最新发布的版本。选项放在 `-s --` 后面，例如 `| bash -s -- --access public`。自己发布到别的地址时：`tools/make_release.py --base <发布地址>` 生成发布文件，`release.json` 里的 `install_command` 就是对应的命令。

安装程序会依次：找到或自动安装 Python 3.11+（root 时用系统包管理器；系统自带的版本太旧时，例如 Ubuntu 22.04，再装 python3.11）→ 下载程序并核对 SHA-256 → 检查系统（venv、磁盘、内存、目录权限）→ 复制程序到 `/opt/violet/app` → 创建虚拟环境并安装依赖 → 生成配置 `config/violet.env` 和数据目录 → 选择端口（默认 7100，被占用就自动选下一个空闲的）→ 生成随机的安全入口、用户名和一次性的初始密码 → 创建系统用户 `violet`、注册 systemd 服务（崩溃自动重启、开机自启）→ 启动并做健康检查 → 输出后台地址、用户名、初始密码和网络检查结果。

安装完成时会显示类似这样的信息（和 1Panel 一样，地址最后一段是安全入口）：

```
管理后台地址：
  https://203.0.113.10:7100/k3x9q2m7wz8a
  安全入口：/k3x9q2m7wz8a（地址里必须带上它，否则所有页面都显示 404）
用户名：violet_q7m2kx
初始密码：Xb7…（只显示这一次；第一次登录后必须修改）
```

已经有程序目录时也可以在目录里运行：`bash install.sh`。不想用 root：加 `--user-install`，装到 `~/.local/share/violet`，用 systemd 用户服务（没有 systemd 时用 Violet 自带的守护进程；这种方式没有自动更新）。Windows / macOS：`python tools/violet_installer.py install`。

同一份数据只能运行一个 Violet：已经有 Violet 在运行时，再启动一个会直接退出并说明原因（避免同一条消息被回复两次）。

## 安装完以后

1. 打开输出里的后台地址（带安全入口），用用户名和初始密码登录，先改成自己的密码（改之前其他页面都打不开）。用户名和安全入口可以在“账号安全”页修改。
2. “开始”页：填写模型服务的 API 地址和 Key，点“保存并测试”；给各个 Agent 选模型。
3. “渠道”页：扫码登录微信，或者添加 QQ 官方机器人（AppID + AppSecret，不需要 NapCat）/ Telegram / QQ（OneBot v11，可选），用绑定码绑定你自己的账号。
4. “工具”页（可选）：添加 MCP 服务器，给工具分类（只读 / 写入 / 高风险 / 拒绝）、授权给 Agent、点“测试”。
5. “诊断”页：哪里不正常、为什么、怎么处理；能安全自动修复的有按钮。

其余（人设、记忆、主动消息、虚拟生活、联网搜索、图片、回复检查、时区、日志、更新方式……）都可以以后在后台“设置”页调整，不需要编辑任何文件。标着“重启后生效”的设置，保存后点“重启 Violet”即可。只有安装器管理的项目（目录、端口、HTTPS 证书）用服务器上的命令改。

## 访问方式

| 安装参数 | 后台在哪里能打开 |
|---|---|
| `--access public`（root 安装的默认） | 公网和局域网都能打开：`https://服务器公网IP:端口/安全入口`，强制 HTTPS（自签名证书，浏览器会提示“不安全”，核对安装输出里的证书指纹后继续访问）。安装时自动装 openssl、查询公网 IP（和 1Panel 一样问外部服务）、在本机防火墙（ufw / firewalld）放行端口（加 `--keep-firewall` 不动防火墙）。云服务器还要在控制台安全组里放行这个端口 |
| `--access lan` | 只给局域网用，同样是 HTTPS；本机防火墙只给出放行命令 |
| `--access local`（普通用户 `--user-install` 的默认） | 只有服务器本机。远程管理：`ssh -L 7100:127.0.0.1:7100 用户@服务器`，再在自己电脑打开 `http://127.0.0.1:7100/安全入口` |
| `--access proxy` | 你自己的 HTTPS 反向代理（Caddy / Nginx，同一台机器）转发到 127.0.0.1 |

已经装好的随时可以切换：重新运行安装命令并加上 `-s -- --access public`（或 local / lan / proxy），数据、账号、端口都保留，只改监听地址和证书。

**安全入口**：没有从安全入口进来的浏览器，访问任何页面（包括登录页）都只得到 404，扫描器看不出这里是 Violet。从入口进来一次后，这个浏览器就可以打开登录页（退出登录后也一样）。例外只有：`/healthz`（只回答“这里运行着 Violet”，安装和升级的健康检查要用）、聊天平台的签名回调地址，以及带正确管理令牌的脚本请求。

安装程序分别检查：端口在监听、本机能访问、局域网能访问、公网能访问。**公网是否可达从服务器内部无法确认**（云厂商安全组、NAT、运营商封端口都要从外面测），所以默认显示“未验证”；用手机流量打开地址确认，或者安装时加 `--public-url https://你的域名` 让安装程序试一次。

## 更新

后台“设置 → 版本更新”选择更新方式：

| 方式 | 效果 |
|---|---|
| 自动更新（默认） | 每 6 小时检查一次 GitHub Releases，有新版本就在服务器上自动升级：先备份，新版本起不来自动退回旧版本。同一个版本升级失败后 24 小时内不再自动重试 |

升级（以及重新运行安装做修复）前做的备份：成功并通过健康检查后**自动删除**，不占硬盘；失败时保留在 `backups/`，用来恢复。自己用 `violet backup` 做的备份不会被自动删除。想在升级后还能退回旧数据，升级前先执行一次 `/opt/violet/violet backup`。
| 自动检查，只提醒 | “开始”页的“版本与更新”显示有新版本，点“立即更新”才升级 |
| 手动检查 | 只在你点“检查更新”时才查询 |

自动更新靠安装时注册的 root 定时任务（`violet-update.timer`），只有 root + systemd 的安装才有（一行命令安装就是这种）。Violet 自己以普通用户 `violet` 运行，它只能“请求更新到官方最新版”，不能指定下载地址。其他安装方式在页面上给出命令：`/opt/violet/violet update`。

改用自己的分支发布：安装时加 `--update-repo 你的用户名/仓库名`。

发布新版本（维护者）：改 `violet/VERSION`，提交后推送标签 `v<版本>`（例如 `git tag v0.1.1 && git push origin v0.1.1`），GitHub Actions 会自动打包并发布到 Releases。

## 日常维护

安装目录里的 `violet`（Windows 是 `violet.cmd`）：

```bash
/opt/violet/violet status        # 服务状态和网络检查
/opt/violet/violet stop | start | restart
/opt/violet/violet backup        # 配置 + 数据（数据库在线一致备份）到 backups/
/opt/violet/violet restore backups/violet-backup-....tar.gz   # 恢复前会先备份当前状态
/opt/violet/violet update   # 升级到 GitHub 上的最新版本：先备份，失败自动退回旧版本（加 --check 只查询）
bash install.sh upgrade       # 或者：在新版本的程序目录里执行
/opt/violet/violet rollback      # 退回上一个版本的程序（数据不动）
/opt/violet/violet uninstall     # 删除服务和程序，保留配置、数据和备份
/opt/violet/violet uninstall --purge --yes   # 全部删除（不可恢复）：数据、备份、/opt/violet、violet 系统账号、放行的防火墙规则
# 或者不依赖已安装的文件（例如装坏了）：
# curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
/opt/violet/violet info             # 后台地址（带安全入口）和用户名；密码不能查看
/opt/violet/violet reset-password   # 生成新的初始密码（只显示一次），所有登录失效
/opt/violet/violet reset-entrance   # 换一个随机的安全入口（--entrance 自己指定；--no-entrance 取消）
/opt/violet/violet set-port 8443    # 换后台端口：检查端口空闲、重启、健康检查，失败自动改回（加 --open-firewall 放行防火墙）
```

重新运行 `install.sh` 就是修复：配置、数据、密钥、端口、用户名、安全入口和密码都保留。忘记地址或用户名：`/opt/violet/violet info`；忘记密码：`/opt/violet/violet reset-password`。

从 0.1.0 升级上来的安装没有用户名和安全入口（升级不会自动加，免得你不知道新地址）：在后台“账号安全”页设置，或执行 `/opt/violet/violet reset-entrance`。
