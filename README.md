# Claw Remote

## 本次修改说明

- **【修改位置 1】** **门户与三个后端仅监听服务器本机；远程访问统一通过 SSH 隧道。**
- **【修改位置 2】** **补充 Windows 10 与 macOS 的 SSH 隧道配置和访问方式。**

自研的远程管控面板——把服务器上所有 tmux 会话、任务状态、服务器指标收进一个网页，手机上点一下就能切进任意会话接管。

## 架构

三个后端服务（其实是同一个 `claw-openclaw.py`，靠 `--port` / `--tmux-socket` 区分），各盯一个 tmux socket：

| 后端 | 端口 | tmux socket |
|------|------|-------------|
| Claude  | 18084 | claude-code.sock |
| Codex   | 18085 | codex-code.sock |
| Default | 18086 | default-code.sock |

**【修改位置 1】** **前面由仅监听 `127.0.0.1:8080` 的 Caddy 门户做统一入口，按路径分发到三个后端；不要将此控制台直接暴露到公网。**

## 目录

- `backend/claw-openclaw.py` — 三合一后端（HTTP API + tmux 控制 + 内嵌前端）
- `frontend/claw-remote/` — 独立前端资源
- `portal/` — Caddyfile + docker-compose（门户容器）+ 落地页
- `scripts/` — clear-port / restore-sessions / snapshot 等配套脚本
- `deploy/systemd/` — 5 个 systemd unit（claude / codex / default / portal / restore）

## 后端 API

- GET：`/api/data`（主面板数据 + 服务器指标）、`/api/pane`（会话终端快照）、`/api/git`、`/api/prompts` …
- POST：`/api/send`（投递指令）、`/api/control`、`/api/session/create`、`/api/session/delete` …

路径带 `/claude` / `/codex` / `/default` 前缀区分后端。

## 部署

1. 前置条件：安装 `tmux`、Python 3，以及需要从面板启动的 CLI。使用 `+ Codex` 前先确认 `codex --version` 可用并完成 `codex login`；使用 `+ Claude` 前先完成 Claude Code 的安装和登录。
2. **【修改位置 1】** **三个后端：把 `deploy/systemd/openclaw-claw-{claude,codex,default}.service` 里的脚本路径改成你的安装路径，保留 `--host 127.0.0.1`，再用 `systemctl enable --now` 启动。**
3. **【修改位置 1】** **门户：`cd portal && docker compose up -d`。`portal/Caddyfile` 保持 `http://127.0.0.1:8080` 与 `bind 127.0.0.1`，不要改成公网监听。**
4. 后端会读模型路由配置 `~/.claude/runtime-routing.json`（结构见 dispatch 仓库的 `config/runtime-routing.example.json`）；面板本身的会话/指标展示不依赖它。

## 远程访问（SSH 隧道）

**【修改位置 2】** **控制台只监听服务器本机。请先建立 SSH 隧道，再在本机浏览器访问 `http://127.0.0.1:8080/codex/`（也可将路径改为 `/claude/` 或 `/default/`）。不要使用 `http://服务器IP:8080/`。**

**【修改位置 2】** **Windows 10（PowerShell）和 macOS（终端）均执行：**

```bash
ssh -N -L 8080:127.0.0.1:8080 root@服务器IP
```

**【修改位置 2】** **保持该终端窗口打开；如本机 `8080` 已被占用，将命令开头的本机端口改为 `18080`，并访问 `http://127.0.0.1:18080/codex/`。**

点击 `+ Codex` 后，后端会通过登录 Shell 解析 npm/NVM 安装的 `codex`，并在返回成功前确认 Codex 进程确实在 tmux 中运行。若 CLI 未安装、未登录或启动后立即退出，界面会显示真实失败，而不会留下一个空会话。

> tmux socket 统一挂 `/tmp/clawdbot-tmux-sockets/`，三个后端 + 任何往里挂会话的脚本都要用这个目录，否则面板看不到会话。
