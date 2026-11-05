# 离线自动化测试

运行方式：

```powershell
cd violet
.\run_tests.ps1            # 全部（Windows；第一次会在上一级目录建 .venv 并装依赖）
.\run_tests.ps1 -k persona # 只跑名字里带 persona 的测试
```

Linux / macOS：

```bash
python -m pip install -r requirements-dev.txt && python -m pytest
```

一共 140 多个测试文件，约 3000 项，全部离线，几分钟跑完。

## 不会碰到真实的东西（`conftest.py`）

- **配置**：`VIOLET_ENV_FILE` 指向一个不存在的临时文件，所以真实的 `.env` 不会被读到。
- **数据库**：`VIOLET_DB_PATH` 指向临时的 SQLite 文件，项目里的 `violet.db` 不会被打开。每个测试开始前都会清空，各人设自己的数据库文件也一样。
- **密钥**：Key 和 Token 都是假的，代理相关的环境变量会被去掉。
- **网络**：`socket.connect` 和 `socket.getaddrinfo` 被替换掉了。任何连到非本机地址的连接或 DNS 查询都会抛出 `NetworkBlocked`，所以连不到微信、QQ、Telegram、模型接口和 MCP 中继。
- **假的外部服务**：
  - 微信换成 `FakeILinkClient`，模型换成 `FakeLLM`（返回真正的 `ChatCompletion` 对象）。
  - MCP 用的是绑定在 `127.0.0.1` 上的真实 FastMCP 服务器（`mcp_test_server.py`）。
  - 联网用 `web_fixtures.py` 里的本机“互联网”，画图用 `image_fixtures.py` 里的假接口。
- **手机状态**：默认关闭（`VIOLET_PHONE_CONTEXT=0`），需要它的测试会自己起一个本机的假手机。

## 文件怎么找

文件名就是它测的东西。主要的几组如下：

| 文件 | 测什么 |
|---|---|
| `test_database.py`、`test_runtime.py`、`test_llm_layer.py` | 数据库、收发消息的主循环、模型调用（超时、重试、备用模型） |
| `test_memory_*`、`test_profile*`、`test_context_engine.py` | 记忆、关于你的档案、每一轮给模型的上下文 |
| `test_persona*`、`test_phase5a.py`、`test_aliveness_violet.py` | 人设、关系、虚拟生活、心情、每个人设各自的记录 |
| `test_proactive*`、`test_holidays_violet.py`、`test_phone_*` | 主动联系、节日祝福、手机状态 |
| `test_channels_p10*`（含 Telegram）、`test_qq*`、`test_social*` | 聊天渠道、QQ 群 |
| `test_web*`、`test_images*`、`test_mcp*`、`test_voice*`、`test_schedule*` | 联网、看图画图、MCP 工具、语音、课表 |
| `test_control_center*`、`test_dashboard.py`、`test_eve_import_violet.py` | 网页后台的每个接口、登录和权限、从 Project Eve 导入 |
| `test_installer_p10.py`、`test_updates_violet.py`、`test_snowluma*`、`test_root_task*` | 安装、升级、卸载、一键安装的可选组件 |

`samples/` 里是给真实模型评估用的样例对话，离线测试只检查格式。
