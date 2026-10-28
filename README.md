# Violet

Violet 是一个自己部署的 AI 陪伴：一个有稳定性格、会慢慢熟起来的虚构女孩。你可以在微信（ClawBot）、QQ 或 Telegram 上和她聊天，她会记住你说过的事，有自己平淡的日常，也会在合适的时候主动找你。所有设置都在网页后台里完成。

> 灵感来源：[Lynnette177/Project-Eve](https://github.com/Lynnette177/Project-Eve)（作者 [Lynnette177](https://github.com/Lynnette177)）。Violet 在它的基础上重写和扩展了大部分功能，已经是一个独立的项目。感谢原作者的创意。

## 她能做什么

- **多个聊天软件，同一个她**：微信 ClawBot（iLink）、QQ 官方机器人、QQ（OneBot v11）、Telegram。几个账号绑定同一位主人，记忆和聊天记录是一份；陌生人发来的消息不回复，也不保存原文。
- **记忆**：分开记录你的长期情况、近期发生的事、此刻的状态、交流偏好和约定。会随时间淡化，不会把没说完的事当成任务反复追问；你可以在后台查看、纠正、删除，或者禁止她使用某条记忆。
- **稳定的人设和虚拟生活**：核心设定只能手动修改；日常生活慢慢推进，可以好几天什么都不发生。她不会编造生活记录里没有的经历。
- **主动联系**：会主动分享、想你、接着之前的话题聊，也可以选择不发。有静默时段、每日上限和"没回就先等等"的规则；你说"别老找我"，她就会少找你。
- **提醒**："明天八点提醒我交作业"会被真正记下，到点提醒。
- **可选功能**（默认关闭，在后台打开）：联网搜索和网页阅读（后台一键安装私有的 SearXNG，只在本机监听）、课表（上课时不打扰、考试前祝你顺利）、读图和画图、MCP 工具（只读工具和需要授权的写入工具分开管理）。
- **网页后台**：模型和各个 Agent、聊天渠道、MCP 工具、人设、记忆、主动消息、课表、运行状态和故障诊断。

## 安装

在一台 Linux 服务器上，一行命令：

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash
```

Debian、Ubuntu、Rocky 等都用这一条：root 直接装（Debian 最小安装没有 sudo 也可以）；普通用户会自动通过 sudo 运行；没有 sudo 的普通用户先 `su -` 切换到 root。

安装程序会自动装好 Python 和依赖、注册 systemd 服务、选端口，并像 1Panel 一样生成随机的安全入口、用户名和初始密码，最后显示后台地址（`https://服务器IP:端口/安全入口`）。忘记了用 `/opt/violet/violet info` 查看地址和用户名，`/opt/violet/violet reset-password` 重置密码。之后打开后台，按"开始"页填写模型服务的地址和 Key、在"渠道"页登录微信或添加机器人就可以聊天了。详细说明见 [violet/INSTALL.md](violet/INSTALL.md)。

访问方式：以 root 安装时默认开放公网访问，后台地址是 `https://服务器公网IP:端口/安全入口`（自签名证书，浏览器提示“不安全”时选择继续访问）。云服务器还要在控制台的“安全组 / 防火墙”里放行这个端口。只想在服务器本机访问（用 SSH 隧道管理）：安装命令后面加 `-s -- --access local`。已经装好的也可以随时切换，重新运行一次（数据、账号和端口都保留）：

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash -s -- --access public
```

更新：默认自动更新（有新版本就在服务器上自动升级，先备份、失败自动退回），也可以在后台“设置 → 版本更新”改成只提醒或手动检查；在后台“开始”页点“立即更新”会弹出进度条，手动升级也可以用 `/opt/violet/violet update`。手机 MCP 是可选的：安装命令不装它，需要时在后台“工具与手机”页点“一键安装中继”（同样有进度条）。所有日常设置都在网页后台完成，不需要编辑配置文件。

## 卸载

一行命令彻底卸载，不需要再执行别的命令：删除程序、服务、自动更新和后台任务服务、手机 MCP 中继（程序、服务和它的数据）、联网搜索（SearXNG 的服务、程序和缓存）、配置、数据（聊天记录和记忆）、备份、`violet` 系统账号、`/opt/violet` 目录，以及 Violet 放行的所有防火墙规则，**不可恢复**：

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
```

安装记录丢了、只装了一半，或者之前只卸载了程序，这条命令也会把剩下的清理掉。安装时用系统包管理器装的 Python、openssl 不会删除（其他程序可能在用）。

需要：一个 OpenAI 兼容的模型接口。已在 Ubuntu 22.04 / 24.04、Debian 12、Rocky Linux 9 上自动测试过安装（GitHub Actions）。

## 当前状态（请如实了解）

- 所有功能都有离线自动化测试（约 2500 项，用模拟的模型和聊天平台运行）。
- 一行命令安装、systemd 服务、崩溃自动重启、修复、备份、升级失败自动退回、卸载：每次修改安装器都会在 GitHub Actions 的真实 Linux 上自动测试。
- **以下内容还没有在真实环境里验证过**：Telegram、QQ 与真实平台联调；手机端 MCP 在真机上的表现。
- 这是一个陪伴类项目，Violet 清楚自己是虚构角色，不会冒充真人；AI 画的图不会说成照片。

## 目录

| 路径 | 内容 |
|---|---|
| `violet/` | 程序（Python / Flask / SQLite）、安装器、测试 |
| `docs/` | 设计决策、测试记录、开发交接 |
| `scripts/` | 开发用的检查脚本 |

配套的安卓只读手机状态服务 Violet MCP 在另一个仓库：[aileboeth-web/violet-mcp](https://github.com/aileboeth-web/violet-mcp)（可选，装在手机上，让 Violet 在你允许的范围内知道手机的状态）。

## 许可证

MIT，见 [LICENSE](LICENSE)。
