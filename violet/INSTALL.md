# Installing Violet

**English** | [简体中文](INSTALL.zh-CN.md)

## One-command install

On a fresh Linux server (no need to download the source, install git or set up Python first):

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash
```

The same command works on every Linux: logged in as root (for example a minimal Debian without sudo) it installs directly; as a normal user it re-runs itself through sudo (enter your password); a normal user without sudo is told to run `su -` first to switch to root. The same goes for the `/opt/violet/violet …` commands below: you never need to add sudo yourself.

It installs the latest version published on GitHub Releases. Options go after `-s --`, for example `| bash -s -- --access public`. To publish to a different address yourself: `tools/make_release.py --base <release address>` builds the release files, and `install_command` in `release.json` is the matching command.

The installer, in order: finds or installs Python 3.11+ (as root through the system package manager; when the system's version is too old, e.g. Ubuntu 22.04, it also installs python3.11) → downloads the program and checks its SHA-256 → checks the system (venv, disk, memory, directory permissions) → copies the program to `/opt/violet/app` → creates a virtual environment and installs the dependencies → creates the configuration `config/violet.env` and the data directory → picks a port (a free random port in 10000–29999 on a new install; a reinstall / upgrade keeps the old one; `--port` sets it) → generates a random security entrance, username and one-time initial password → creates the system user `violet` and registers a systemd service (restart on crash, start on boot) → starts it and runs a health check → prints the panel address, username, initial password and the network check results.

When it finishes it shows something like this (as in 1Panel, the last part of the address is the security entrance):

```
Control panel address:
  https://203.0.113.10:21473/k3x9q2m7wz8a
  Security entrance: /k3x9q2m7wz8a (must be in the address, otherwise every page shows 404)
Username: violet_q7m2kx
Password: Xb7… (a random 20-character password, shown only this once; keep it, or change it under Account security in the panel)
```

(The installer prints these lines in Chinese.)

If you already have the program directory, you can also run `bash install.sh` inside it. Without root: add `--user-install` to install into `~/.local/share/violet` with a systemd user service (Violet's own supervisor when there is no systemd; this way has no automatic updates). Windows / macOS: `python tools/violet_installer.py install`.

Only one Violet can run on the same data: if one is already running, starting another exits right away and says why (so the same message is never answered twice).

## After installing

1. Open the panel address from the output (with the security entrance) and log in with the username and password. The password is a random 20 characters; keep using it or change it on the Account security page, where the username and security entrance can be changed too.
2. The Start page lists what is still to be done; click an item to jump straight to where it is filled in.
3. Model services page: in step 1 enter the API address and key and click Save and test connection (it only fetches the model list once and does not call a model); in step 2 pick models from the list ("use for all agents" fills them all at once) and click Save models and test a chat.
4. Chat channels page: WeChat, QQ and Telegram cards side by side; use whichever you want. WeChat: scan the QR code. QQ: click Use QQ and follow the steps to create a bot at [q.qq.com](https://q.qq.com/#/) and enter the AppID and AppSecret. Telegram: click Use Telegram and get a token from [@BotFather](https://t.me/BotFather). Once QQ / Telegram is connected, generate a binding code in the card and send it to the bot from your own account. A personal QQ account (SnowLuma) is installed with one click in the QQ card; log in by scanning the QR code in the QQ window (the install downloads an image of about 740 MB; the page shows how many MB are done and which mirror they come from, and switches to the next mirror when one is too slow).
   - Personas & life page (optional): choose who she is (several built-in personas, or write your own) and what your relationship is. Different chat channels can use different personas, and each persona has its own chat history and memory.
   - Moving over from Project Eve (optional): at the bottom of the Chat history page, Import chat history and memory. Copy the packing command and run it on the Eve server, upload the file it creates, choose the channel, persona and relationship to import into, preview, then confirm.
5. Tools & phone page (optional):
   - Phone MCP (optional; the one-line install does not install it): click Install relay. A dialog shows a progress bar (download, verify, register the service, open the port). When it is done the page lists the relay address, device key and certificate fingerprint to enter in the phone app (each with a copy button); then paste the access token from the phone's Security page back here and you are connected. The relay's phone port is picked at random during install (20000–29999; the page shows the actual port), and on a cloud server it must be opened in the security group. If the phone cannot connect, click Change port (also with a progress bar): the device key and certificate fingerprint stay the same, and only the relay address changes on the phone. These two buttons are run by the root `violet-task` service, which exists only on root + systemd installs; other installs show the commands on the page (`/opt/violet/violet mcp-relay install`, `mcp-relay set-port [port]`). Reinstalling / upgrading Violet keeps an installed relay; a complete uninstall removes it too.
   - Test web access for web search: runs one real search and opens one web page, and shows the results and the search engines that did not respond. On a server in mainland China, SearXNG's default Google, DuckDuckGo, Brave and Wikipedia are usually unreachable: if the install finds no results it switches to Bing, Baidu, Sogou and 360 automatically (and if that still fails, tries IPv4 only). You can also click Choose search engines to tick the engines yourself, turn on IPv4 only (when the server has IPv6 configured but it does not work, every engine times out), or click Choose automatically inside it. Test web access shows how many results each engine returned and why the unresponsive ones failed (timeout, access denied, captcha required…).
   - Web search (optional): under Web tools click Install web search. A dialog shows the progress: it downloads the source of the open-source SearXNG search from GitHub, installs it into its own Python environment, registers it as the system service `violet-searxng` (listening only on 127.0.0.1, not exposed, no firewall port opened, no Docker needed), runs one test search, and then turns web search on. Uninstall web search removes the service, program and cache and turns web search off; a complete uninstall of Violet removes it too. On the server: `/opt/violet/violet searxng install | uninstall | status`. Dependencies are installed from the same pip index as Violet (a mirror set with `PIP_INDEX_URL` or pip.conf on the server is used too). If you prefer Docker, you can still deploy it by hand following `deploy/searxng/README.md`.
   - Other MCP servers: after adding one, classify its tools (read-only / write / high risk / denied), grant them to agents, and click Test.
6. Images page (optional): turn on images. For seeing images, choose a vision model (it must accept image input) and click Test vision. For drawing, choose the drawing model, the API style and the daily limit, and click Test drawing (draws one real picture, which may cost money). Violet only draws in chats after the test passes. The address and key default to those of Model services and can be set separately. Recent images can be viewed and deleted here.
7. Diagnostics page: what is wrong, why, and what to do; problems that can be fixed safely and automatically have a button.
8. When troubleshooting: under Settings → Logs & troubleshooting, turn on the troubleshooting log (off by default). Every step of every chat round (what was received, intent, which tools were offered, model and tool calls, web search, reply, sending) is written to the log; on the Logs page tick Troubleshooting log only and click a conversation number to see just that round. Remember to turn it off afterwards.

Everything else (personas, memory, proactive messages, virtual life, web search, images, reply checks, time zone, logs, update mode…) can be adjusted later on the panel's Settings page; no files need editing. For settings marked "takes effect after restart", click Restart Violet after saving. Only what the installer manages (directories, port, HTTPS certificate) is changed with commands on the server.

## Access modes

| Install option | Where the panel can be opened |
|---|---|
| `--access public` (default for a root install) | From the internet and the LAN: `https://SERVER_PUBLIC_IP:PORT/ENTRANCE`, HTTPS only (self-signed certificate; the browser says it is not secure — compare the certificate fingerprint in the install output, then continue). The install also installs openssl, looks up the public IP (asking an outside service, as 1Panel does), and opens the port in every firewall on the machine (add `--keep-firewall` to leave the firewall alone, see below). On a cloud server the port must also be opened in the console's security group |
| `--access lan` | LAN only, also HTTPS; for the local firewall it only prints the commands to open the port |
| `--access local` (default for a normal user's `--user-install`) | The server itself only. Remote management: `ssh -L PORT:127.0.0.1:PORT user@server`, then open `http://127.0.0.1:PORT/ENTRANCE` on your own computer |
| `--access proxy` | Your own HTTPS reverse proxy (Caddy / Nginx, on the same machine) forwarding to 127.0.0.1 |

**Local firewall** (handled automatically for public access):

| Detected | How the port is opened | After a reboot |
|---|---|---|
| ufw (Ubuntu / Debian; common with BT Panel / aaPanel) | `ufw allow PORT/tcp` | ufw keeps it |
| firewalld (Rocky / CentOS / Alma; common with BT Panel) | `firewall-cmd --permanent --add-port` | firewalld keeps it |
| Blocking rules in iptables (1Panel, CSF, hand-written rules, default DROP) | An allow rule tagged `violet` at the top of INPUT (IPv4 / IPv6) | Violet adds it back before every start |
| A blocking chain of nftables itself | An allow rule tagged `violet` at the top of that chain | Same as above |

The firewall pages of panels such as 1Panel and BT Panel may not show this rule; if you want to see it there, add one by hand as well. Uninstalling (`--purge`) removes only rules tagged `violet` and the rules recorded during install. A cloud provider's security group is outside the server and can only be opened in its console.

An existing install can switch at any time: run the install command again with `-s -- --access public` (or local / lan / proxy). Data, account and port are kept; only the listen address and certificate change.

**Security entrance**: a browser that did not come in through the security entrance gets only 404 for every page (the login page included), so scanners cannot tell that Violet is here. After coming in through the entrance once, that browser can open the login page (also after logging out). The only exceptions: `/healthz` (it only answers "Violet is running here"; the install and upgrade health checks need it), the signed callback addresses of chat platforms, and script requests with the correct admin token.

The installer checks separately: the port is listening, reachable from the machine itself, from the LAN, and from the internet. **Whether the internet can reach it cannot be confirmed from inside the server** (cloud security groups, NAT and ISP port blocking all have to be tested from outside), so it shows "not verified" by default. Open the address over mobile data to confirm, or add `--public-url https://your-domain` during install so the installer tries it once.

## Updates

Choose the update mode under Settings → Version updates in the panel:

| Mode | Effect |
|---|---|
| Automatic (default) | Checks GitHub Releases every 6 hours and upgrades the server automatically when there is a new version: backup first, and automatic rollback to the old version if the new one does not come up. After a failed upgrade, the same version is not retried automatically for 24 hours |
| Check automatically, notify only | Version & updates on the Start page shows that a new version is out; it upgrades only when you click Update now |
| Check manually | Only checks when you click Check for updates |

The backup made before an upgrade (and before re-running the install as a repair) is **deleted automatically** after success and a passing health check, so it takes no disk space; after a failure it is kept in `backups/` for restoring. Backups you make yourself with `violet backup` are never deleted automatically. To be able to go back to the old data after an upgrade, run `/opt/violet/violet backup` once before upgrading.

Automatic updates rely on the root timer registered during install (`violet-update.timer`), which only root + systemd installs have (the one-line install is one). Violet itself runs as the normal user `violet` and can only "request an update to the latest official version"; it cannot choose a download address. Clicking Update now in the panel shows a progress bar (check, download and verify, backup, install dependencies, restart, health check); Violet restarts once during the upgrade, and the dialog waits for it to come back and shows the result. Other installs show the command on the page: `/opt/violet/violet update`.

To publish from your own fork instead: add `--update-repo your-username/repo-name` during install.

Releasing a new version (maintainers): change `violet/VERSION`, commit, then push the tag `v<version>` (for example `git tag v0.1.1 && git push origin v0.1.1`); GitHub Actions builds it and publishes it to Releases.

## Maintenance

`violet` in the install directory (`violet.cmd` on Windows):

```bash
/opt/violet/violet status        # service status and network checks
/opt/violet/violet stop | start | restart
/opt/violet/violet backup        # configuration + data (consistent online database backup) into backups/
/opt/violet/violet restore backups/violet-backup-....tar.gz   # backs up the current state before restoring
/opt/violet/violet update   # upgrade to the latest version on GitHub: backup first, automatic rollback on failure (--check only checks)
bash install.sh upgrade       # or: run this in the program directory of the new version
/opt/violet/violet rollback      # go back to the previous version of the program (data untouched)
/opt/violet/violet uninstall     # remove the service and program, keep configuration, data and backups
/opt/violet/violet uninstall --purge --yes   # remove everything (cannot be undone): data, backups, /opt/violet, phone relay, all violet services, the violet system account, firewall rules tagged violet
# or, without relying on the installed files (e.g. a broken install or a lost install record) -- this one is enough, nothing else needed:
# curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
/opt/violet/violet info             # panel address (with the security entrance) and username; the password cannot be shown
/opt/violet/violet reset-password   # generate a new password (shown only once); all logins are ended
/opt/violet/violet reset-entrance   # switch to a new random security entrance (--entrance sets your own; --no-entrance removes it)
/opt/violet/violet set-port 7300    # change the panel port: checks the port is free, restarts, health check, reverts on failure (--open-firewall opens the firewall)
/opt/violet/violet mcp-relay install    # relay for the phone MCP: download from the Violet MCP GitHub releases and verify, register the service, open the phone port
/opt/violet/violet mcp-relay status     # whether the relay runs and whether the phone is connected
/opt/violet/violet mcp-relay uninstall  # remove the relay installed by Violet (a hand-deployed one is left alone)
```

The phone MCP relay: if you already installed the `violet-mcp-relay` service by hand following Violet MCP's `DEPLOY_RELAY.md`, `mcp-relay install` uses it as it is and does not reinstall. The phone port is picked at random in 20000–29999 (`mcp-relay install PORT` sets it), and the local port for Violet is 8787 (the next one when it is taken); the panel page shows the actual ports. `mcp-relay set-port [port]` only works on a relay that Violet installed. The device key is stored only in the relay's own directory and in Violet's `data/control/mcp-relay.json` (readable only by the violet account), and can be viewed and copied on the panel's Tools & phone page.

Running `install.sh` again is a repair: configuration, data, keys, port, username, security entrance and password are all kept. Forgot the address or username: `/opt/violet/violet info`; forgot the password: `/opt/violet/violet reset-password`.

Installs upgraded from 0.1.0 have no username and security entrance (an upgrade does not add them, so you are not left without knowing the new address): set them on the panel's Account security page, or run `/opt/violet/violet reset-entrance`.
