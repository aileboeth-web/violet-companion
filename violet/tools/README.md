# tools/

这个目录放两类东西：服务器上用的安装和管理脚本，以及开发时用的测试工具。开发测试的完整过程记录（Phase 7–9 的验收、真实模型评估）在 [TESTING-HISTORY.md](TESTING-HISTORY.md)。

## 服务器上用的

用户平时只用 `/opt/violet/violet <命令>`，它会调用下面这些脚本。网页后台的“一键安装”按钮调用的也是它们。

| 文件 | 对应命令 | 作用 |
|---|---|---|
| `violet_installer.py` | `install.sh`、`violet update / backup / restore / uninstall / info / reset-password / set-port …` | 安装、修复、升级（失败自动退回）、备份、卸载、访问方式和防火墙。只用 Python 标准库 |
| `violet_setup.py` | （安装时自动运行） | 生成配置和数据目录，可以重复运行 |
| `violet_service.py` | （没有 systemd 时） | Violet 自带的守护进程，用于 Windows、macOS、容器和精简版 Linux |
| `violet_admin.py` | `python -m tools.violet_admin set-password` | 后台管理员密码 |
| `violet_searxng.py` | `violet searxng install / uninstall / status` | 私有的 SearXNG 联网搜索，只在本机监听，不需要 Docker |
| `violet_snowluma.py` | `violet qq install / uninstall / status / restart` | QQ 小号（SnowLuma，Docker 运行）。拉镜像时会自动换镜像站 |
| `violet_voice.py`、`voice_server.py` | `violet voice install / uninstall / status` | 她的声音：CosyVoice 在服务器 CPU 上运行，用一段录音克隆声音 |
| `violet_relay.py` | `violet mcp-relay install / status / uninstall / set-port` | 手机 App Violet MCP 的中继 |
| `make_release.py` | （维护者） | 打包发布文件（`install.sh`、`violet.tar.gz`、`SHA256SUMS`）。GitHub Actions 发布时也用它 |

## 开发时用的

下面这些工具都在隔离的沙盒里跑真实的 Violet 流程：有自己的数据库，不连微信等聊天平台，也不碰正式数据。默认用模拟模型，不花钱；要用真实模型时会先问你要 Key，Key 不保存。

| 文件 | 作用 |
|---|---|
| `violet_test_chat.py`、`free_chat.py` | 自己和她聊天。`free_chat` 的会话可以下次接着聊，还能带上假的课表、假的 MCP 工具 |
| `model_compat.py` | 检查一个 OpenAI 兼容接口能不能用：JSON 输出、工具调用、超时 |
| `chat_eval.py`、`chat_ab.py`、`chat_dialogue.py`、`dialogue_eval.py`、`scenario_eval.py`、`decision_eval.py` | 聊天体验的评估和 A/B 对比：固定场景、多轮对话、该不该用某个能力、同样效果能不能少花 token |
| `memory_conflicts.py`、`memory_restore_decayed.py` | 只读地列出互相矛盾的记忆；找回被旧版本误删的事件记忆 |
| `schedule_rollback.py` | 从数据库里删掉课表的表（回滚课表功能时用） |
| `fake_mcp_server.py` | 一个只有虚构只读工具的 MCP 服务器，测试用 |
| `harness.py`、`replay.py`、`legacy.py`、`synthetic_*.py`、`reassess_tail.py`、`memory_query_audit.py` | 上面这些工具共用的部分，以及早期验收用的专项脚本 |

运行方式：在 `violet/` 目录里执行 `python -m tools.<名字> --help`。
