# 安装 Violet

## 一条命令安装

在一台全新的 Linux 服务器上（不需要先下载源码、装 git 或配置 Python）：

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | sudo bash
```

装的是 GitHub Releases 上最新发布的版本。选项放在 `-s --` 后面，例如 `| sudo bash -s -- --access public`。自己发布到别的地址时：`tools/make_release.py --base <发布地址>` 生成发布文件，`release.json` 里的 `install_command` 就是对应的命令。

安装程序会依次：找到或自动安装 Python 3.11+（root 时用系统包管理器；系统自带的版本太旧时，例如 Ubuntu 22.04，再装 python3.11）→ 下载程序并核对 SHA-256 → 检查系统（venv、磁盘、内存、目录权限）→ 复制程序到 `/opt/violet/app` → 创建虚拟环境并安装依赖 → 生成配置 `config/violet.env` 和数据目录 → 选择端口（默认 7100，被占用就自动选下一个空闲的）→ 生成一次性的管理员初始密码 → 创建系统用户 `violet`、注册 systemd 服务（崩溃自动重启、开机自启）→ 启动并做健康检查 → 输出后台地址、初始密码和网络检查结果。

已经有程序目录时也可以在目录里运行：`sudo bash install.sh`。不用 sudo：装到 `~/.local/share/violet`，用 systemd 用户服务（没有 systemd 时用 Violet 自带的守护进程）。Windows / macOS：`python tools/violet_installer.py install`。

同一份数据只能运行一个 Violet：已经有 Violet 在运行时，再启动一个会直接退出并说明原因（避免同一条消息被回复两次）。

## 安装完以后

1. 打开输出里的后台地址，用初始密码登录，先改成自己的密码（改之前其他页面都打不开）。
2. “开始”页：填写模型服务的 API 地址和 Key，点“保存并测试”；给各个 Agent 选模型。
3. “渠道”页：扫码登录微信，或者添加 QQ 官方机器人（AppID + AppSecret，不需要 NapCat）/ Telegram / QQ（OneBot v11，可选），用绑定码绑定你自己的账号。
4. “工具”页（可选）：添加 MCP 服务器，给工具分类（只读 / 写入 / 高风险 / 拒绝）、授权给 Agent、点“测试”。
5. “诊断”页：哪里不正常、为什么、怎么处理；能安全自动修复的有按钮。

其余（人设、记忆、主动消息、虚拟生活、联网搜索、课表）都可以以后在后台调整，不需要编辑任何文件。

## 访问方式

| 安装参数 | 后台在哪里能打开 |
|---|---|
| 默认 `--access local` | 只有服务器本机。远程管理：`ssh -L 7100:127.0.0.1:7100 用户@服务器`，再在自己电脑打开 `http://127.0.0.1:7100/control` |
| `--access lan` / `--access public` | 其他机器也能打开，强制 HTTPS（自签名证书，浏览器会提示，核对安装输出里的证书指纹）。需要 openssl |
| `--access proxy` | 你自己的 HTTPS 反向代理（Caddy / Nginx，同一台机器）转发到 127.0.0.1 |

安装程序分别检查：端口在监听、本机能访问、局域网能访问、公网能访问。**公网是否可达从服务器内部无法确认**（云厂商安全组、NAT、运营商封端口都要从外面测），所以默认显示“未验证”；用手机流量打开地址确认，或者安装时加 `--public-url https://你的域名` 让安装程序试一次。本机防火墙开着时只给出放行命令，加 `--open-firewall` 才会自动放行。

## 更新

后台“开始”页的“版本与更新”会显示当前版本和 GitHub 上的最新版本（最多每 6 小时查一次，只查询不下载；可在“设置 → 功能开关”关闭）。有新版本时，在服务器上执行页面给出的命令：`sudo /opt/violet/violet update`。

发布新版本（维护者）：改 `violet/VERSION`，提交后推送标签 `v<版本>`（例如 `git tag v0.1.1 && git push origin v0.1.1`），GitHub Actions 会自动打包并发布到 Releases。

## 日常维护

安装目录里的 `violet`（Windows 是 `violet.cmd`）：

```bash
/opt/violet/violet status        # 服务状态和网络检查
/opt/violet/violet stop | start | restart
/opt/violet/violet backup        # 配置 + 数据（数据库在线一致备份）到 backups/
/opt/violet/violet restore backups/violet-backup-....tar.gz   # 恢复前会先备份当前状态
sudo /opt/violet/violet update   # 升级到 GitHub 上的最新版本：先备份，失败自动退回旧版本（加 --check 只查询）
sudo bash install.sh upgrade       # 或者：在新版本的程序目录里执行
/opt/violet/violet rollback      # 退回上一个版本的程序（数据不动）
/opt/violet/violet uninstall     # 删除服务和程序，保留配置、数据和备份
/opt/violet/violet uninstall --purge --yes   # 连数据一起删除（不可恢复）
```

重新运行 `install.sh` 就是修复：配置、数据、密钥、端口和管理员密码都保留。忘记管理员密码：`sudo bash install.sh --reset-admin`（会显示新的初始密码）。
