# Linux 安装验收（自动化）

正式发布前，一键安装必须在真实 Linux 上通过这一套检查。它只用一次性的机器 / 虚拟机 / 容器，**不要在有真实 Violet 的服务器上运行**。

| 方式 | 命令 | 覆盖 |
|---|---|---|
| 一台全新的 Linux 虚拟机（root） | `bash "violet/deploy/linux-e2e/install_e2e.sh"` | 全部：一条命令安装、systemd（开机自启、kill -9 后自动重启）、初始密码锁定后台、修复保留端口和密码、备份校验、坏升级自动回退、卸载保留数据、purge 清理 |
| Docker（多个发行版，无 systemd） | `bash "violet/deploy/linux-e2e/run_in_docker.sh" debian:12 ubuntu:24.04 rockylinux:9` | 同上，服务用 Violet 自带的守护进程 |
| GitHub Actions | `.github/workflows/install-linux.yml`（仓库发布到 GitHub 后手动触发或改安装器时自动运行） | Ubuntu 22.04 / 24.04 虚拟机上的 systemd + 三个发行版的容器 |

脚本自己构建发布包（`tools/make_release.py --worktree`），在 127.0.0.1 上提供下载，然后执行和用户完全相同的命令 `curl -fsSL <地址>/install.sh | bash`。需要能访问 PyPI（安装依赖）。每一项输出 PASS / FAIL，第一个失败就停止。

不在这里自动验证、需要人工看一眼的：`--access public` 在真实公网上的访问（云安全组、域名解析）、浏览器里接受自签名证书、真实聊天平台的登录。
