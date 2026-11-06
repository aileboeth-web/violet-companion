# Violet

**English** | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Русский](README.ru.md) | [Português](README.pt-BR.md)

Violet is an AI companion that runs on your own server. She has a steady personality and a daily life of her own, remembers what you tell her, reaches out to you when the moment is right, and you can chat with her on WeChat, QQ and Telegram. Everything is set up in a web control panel; there are no config files to edit.

> Inspired by [Lynnette177/Project-Eve](https://github.com/Lynnette177/Project-Eve) (by [Lynnette177](https://github.com/Lynnette177)). Violet rewrote and extended most of it and is now an independent project. Thanks to the original author for the idea.

The control panel's interface is currently in Chinese.

## What she can do

**Chat**

- WeChat (ClawBot / iLink, scan a QR code), a personal QQ account (SnowLuma, scan to log in), an official QQ bot, and Telegram. Several can be connected at once.
- She only replies to you, the person she is bound to. Private messages from strangers get no reply, and their text is not stored.
- With a personal QQ account she can join groups: she joins in when someone says her name, @-mentions her or pokes her, and now and then starts a topic when the group goes quiet. She gets to know the people in the group over time. She can also look at images in the group, collect and send stickers, and post to QQ Zone (she shows you the post first).
- She can send several messages in a row, voice messages (optional: clone a voice from one recording, runs on the server CPU), and pictures she draws.

**Memory**

- She keeps separate notes on your long-term situation, recent events, how you like her to talk, and the promises you made each other. Old things slowly fade, and an unfinished remark is not turned into a task she keeps asking about.
- On the Memory page of the panel you can search (keywords highlighted), view, correct and delete memories, or stop her from using one.
- "Remind me to hand in my homework at eight tomorrow" really gets written down, and she reminds you on time.

**Personas and relationships**

- Built-in personas: Violet (default), gentle companion, cheerful classmate, calm and reliable, one who knows she is an AI, Big Fat Fish, the original Eve persona, and a blank one. You can also write your own.
- Relationship options: close, friend, partner, daughter, developer.
- Each chat channel can use a different persona. Every persona has its own chat history, memory, life and mood, so switching back and forth loses nothing and nothing gets mixed up.
- She has a virtual daily life and moods of her own: her days move on quietly, and several days may pass with nothing happening; if you are harsh with her she gets a little upset. She never makes up experiences that are not in her life record.

**Reaching out**

- She shares things, says she misses you, picks up an earlier topic, or decides not to send anything at all.
- There are quiet hours, a daily limit and a "wait if you haven't replied" rule. Tell her "stop messaging me so much" and she will reach out less.
- Optional: holiday greetings that fit your relationship (Spring Festival, Mid-Autumn, your birthday; for partners also Valentine's Day, Qixi and more).
- Optional: with your phone connected (see Violet MCP below), she knows whether your screen is on, which app you are using, your battery level and today's screen time, and can say good morning the first time you unlock your phone in the morning.

**Tools** (all optional, off by default, turned on in the panel)

- Web search and page reading: one-click install of a private SearXNG that listens only on the server itself. No Docker needed.
- Timetable: she does not disturb you during class and cheers you on before exams. Import from CSV / JSON.
- Seeing and drawing images: her drawings are casual "phone snapshot" life photos, and she never claims they are real photos.
- MCP tools: read-only tools are used directly; tools that change anything ask you first every time and only run after you approve that request in the chat.

**Web control panel**

- Start (what is still missing), Overview, Flow (which prompts, tools and models each reply used, including the full content sent to the model).
- Model services (a different model per agent and per chat platform), Chat channels, Tools & phone, Images, Personas & life, Timetable, Settings.
- Chat history, Memory, Diagnostics, Logs, Change history, Account security.

**Moving over from Project Eve**

Under Chat history → Import chat history and memory, the panel gives you a packing command. Run it on your Eve server, then upload the file it creates to the panel. When importing you choose the chat channel (one you have not added yet works too), the persona and the relationship; both chat history and memory come along.

## Install

On a Linux server, one command:

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash
```

- The same command works on Debian, Ubuntu, Rocky and others. As root it installs directly (fine on a minimal Debian without sudo); as a normal user it re-runs itself through sudo; a normal user without sudo should switch to root with `su -` first.
- The installer sets up Python and the dependencies, registers a systemd service, picks a random port and, like 1Panel, generates a random security entrance, username and initial password. At the end it prints the panel address in the form `https://SERVER_IP:PORT/ENTRANCE`.
  - The certificate is self-signed. When the browser says the page is not secure, choose to continue.
  - On a cloud server, also open this port in the provider's security group / firewall.
- After installing:
  1. Open the panel and fill in your model service address and key on the Start page.
  2. Connect any chat platform on the Chat channels page and start chatting.
- Forgot the address or username: `/opt/violet/violet info`. Forgot the password: `/opt/violet/violet reset-password`.

**Requirements**:

- An OpenAI-compatible model API.
- At least 512 MB of RAM is recommended. Voice needs 8 GB of RAM and 12 GB of disk.

**Panel reachable only on the server itself** (managed through an SSH tunnel):

- New install: add `-s -- --access local` after the install command.
- Already installed: run the install command again with the access mode you want (`local` / `lan` / `public` / `proxy`). Data, account and port are kept.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- --access local
```

More details (access modes, firewall, maintenance commands) are in [violet/INSTALL.md](violet/INSTALL.md).

## Updates

- Automatic by default: when a new version is out, the server upgrades itself. It makes a backup first and rolls back to the old version if the upgrade fails.
- You can switch to "notify only" or "check manually" under Settings → Version updates.
- Clicking Update now on the Start page shows the progress.
- Or upgrade by hand on the server:

```bash
/opt/violet/violet update
```

## Uninstall

The command below removes Violet completely. **This cannot be undone.** It deletes:

- the program, services, automatic updates and the background task service;
- the phone MCP relay, web search, the QQ account, voice;
- configuration, data (chat history and memory) and backups;
- the `violet` system account, the `/opt/violet` directory, and every firewall rule Violet opened.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
```

It also cleans up what is left after a half-finished install, a lost install record, or an earlier uninstall that removed only the program. Python and openssl installed through the system package manager are kept, because other programs may use them.

## Status

- **Automated tests**:
  - Every feature has offline automated tests, about 3000 in total, run against simulated models and chat platforms with no network.
  - Every change to the installer runs the full install, auto-restart, backup, failed-upgrade rollback and uninstall on real Linux in GitHub Actions (Ubuntu 22.04 / 24.04, Debian 12, Rocky Linux 9).
- **Real-world use**:
  - All four chat channels (WeChat, personal QQ, official QQ bot, Telegram) have been tested on the real platforms.
  - Importing chat history and memory from Project Eve has been done with real data.
  - The one-click install of the personal QQ account has also succeeded on a network in mainland China.
- **About who she is**: this is a companion project. Violet knows she is a fictional character and never pretends to be a real person; images drawn by AI are never passed off as real photos.

## What is in the repository

| Path | Contents |
|---|---|
| `violet/ai/` | Violet herself: chat, memory, personas, mood, virtual life, reaching out, group chat, tools, drawing, timetable |
| `violet/ai/channels/` | Connections to WeChat, QQ and Telegram |
| `violet/wx_ilink_py/` | The web control panel (Flask) and the WeChat iLink client |
| `violet/persona/` | Persona templates and prompts |
| `violet/tools/` | Installer, release, install scripts for the optional parts (web search, QQ account, voice, phone relay), and test tools for development |
| `violet/tests/` | Offline automated tests |
| `violet/deploy/` | Install acceptance scripts, and an alternative way to deploy SearXNG with Docker |

## Related project

[Violet MCP](https://github.com/aileboeth/violet-mcp) is an optional Android app. Once it is on your phone, Violet can know the phone's state within the limits you allow, such as whether the screen is on, the app you are using, battery, steps, calendar and the music that is playing. Each item can be switched on or off separately on the phone.

## License

MIT, see [LICENSE](LICENSE).
