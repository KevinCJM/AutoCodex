# Feishu Codex Bridge

`feishu_codex` 是一个本机常驻的飞书机器人桥接服务。它通过飞书长连接接收消息，把消息转发给本机 Codex，并把结果再回复到飞书会话。

第一次启动时，如果还没有配置文件且当前终端是交互式 TTY，bridge 会自动进入引导式配置流程；配置完成后会把参数保存到本地文件，后续直接启动即可。

## 功能

- 支持 `exec` 后端：基于 [B01_codex_utils.py](/Users/chenjunming/Desktop/KevinGit/AutoCodex/B01_codex_utils.py) 的 `thread_id` 会话。
- 支持 `tmux` 后端：基于 [v1/tmux_cli_tools.py](/Users/chenjunming/Desktop/KevinGit/AutoCodex/v1/tmux_cli_tools.py) 的持续会话。
- 每个飞书 `chat_id` 维护一个当前激活会话。
- 单聊文本消息默认发送给当前会话。
- 群聊只有 `@机器人` 时才处理。
- 长结果自动分片发送。
- 如果结果发送失败，会缓存到 SQLite，等用户下次发 `/status` 或普通消息时补发。
- `tmux` 会话会注入固定的 Codex developer instructions，并写入 runtime marker，便于从本地 `~/.codex/sessions` 反查和恢复正确的会话。
- `/status` 会附带 `tmux` runtime 的真实元数据，例如 `agent_session_id`、`confirmed_status`、`current_command`、`log_path`、`state_path`。
- 当前会话会把飞书上下文注入给 Codex；在 `exec` 和 `tmux` 两种后端里，Codex 都可以按需调用本机 `lark-cli` 执行飞书侧操作。

## 环境变量

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- `FEISHU_BOT_OPEN_ID`
- `FEISHU_CODEX_RUNTIME_DIR`
- `FEISHU_CODEX_CONFIG_PATH`
- `FEISHU_CODEX_DB_PATH`
- `FEISHU_CODEX_LOG_DIR`
- `FEISHU_CODEX_ALLOW_ALL_SENDERS=true`
- `FEISHU_CODEX_ALLOWED_SENDERS`
- `FEISHU_CODEX_MAX_WORKERS`
- `FEISHU_CODEX_EXEC_TIMEOUT_SEC`
- `FEISHU_CODEX_TMUX_TIMEOUT_SEC`
- `FEISHU_CODEX_TEXT_CHUNK_CHARS`
- `FEISHU_CODEX_TMUX_USE_SYSTEM_PROXY`
- `FEISHU_CODEX_TMUX_ENV_FILE`
- `FEISHU_CODEX_DEFAULT_MODEL`
- `FEISHU_CODEX_DEFAULT_REASONING_EFFORT`
- `FEISHU_CODEX_AVAILABLE_MODELS`
- `FEISHU_CODEX_AVAILABLE_REASONING_EFFORTS`

说明：

- 不要把飞书密钥写进仓库。只通过环境变量注入。
- 默认配置文件路径是 [/.feishu_codex_config.json](/Users/chenjunming/Desktop/KevinGit/AutoCodex/feishu_codex/.feishu_codex_config.json)；可以用 `FEISHU_CODEX_CONFIG_PATH` 改成别的位置。
- 当前实现优先读取 `FEISHU_*`，同时兼容旧的 `DING_CODEX_*` 运行时变量名，方便从旧目录迁移。
- `FEISHU_VERIFICATION_TOKEN` 和 `FEISHU_ENCRYPT_KEY` 对长连接模式不是必填，但保留为兼容配置。
- 如果你想严格校验“群里必须 @ 当前机器人”，可以额外设置 `FEISHU_BOT_OPEN_ID`。
- `FEISHU_CODEX_TMUX_USE_SYSTEM_PROXY` 默认为 `true`。桥接进程本身可以不带代理启动，但 `tmux` 子会话会在启动 Codex 前自动读取 macOS 系统代理并导出到 shell。
- 如果你有更明确的代理/VPN 环境脚本，可以用 `FEISHU_CODEX_TMUX_ENV_FILE=/absolute/path/to/env.sh`，bridge 会在启动 Codex 前先 `source` 这个脚本。

## 命令

- `/help`
- `/status`
- `/model <model_id>`
- `/think <low|medium|high|xhigh>`
- `/new exec <absolute_workdir>`
- `/new tmux <absolute_workdir>`
- `/resume exec <thread_id> <absolute_workdir>`
- `/resume tmux <tmux_session_name> <absolute_workdir>`
- `/stop`

普通消息行为：

- 单聊里直接转发给当前激活会话。
- 群聊里只有 `@机器人` 时才转发。
- 当前没有激活会话时，会提示先执行 `/new ...` 或 `/resume ...`。
- `/model` 和 `/think` 会按当前会话串行入队；`tmux` 模式会重启并 resume 到同一个 Codex 会话，`exec` 模式会在下一次请求时使用新配置。
- 第一版只支持文本消息；图片、文件、语音会直接返回“不支持”。

## Codex 调用 lark-cli

桥接层会把当前飞书会话上下文写入 `FEISHU_CODEX_RUNTIME_DIR/feishu_context/<conversation>.json`，并把以下环境变量注入到 Codex 运行环境：

- `FEISHU_CODEX_CONTEXT_FILE`
- `LARK_CLI_BIN`

同时会提供一个辅助脚本：

- [lark_cli_helper.py](/Users/chenjunming/Desktop/KevinGit/AutoCodex/feishu_codex/src/feishu_codex_bridge/lark_cli_helper.py)

Codex 在需要执行飞书侧动作时，可以直接调用这些命令：

- `python .../lark_cli_helper.py show-context`
- `python .../lark_cli_helper.py send-text --text "..."`
- `python .../lark_cli_helper.py reply-text --text "..."`
- `cat reply.md | python .../lark_cli_helper.py send-markdown --stdin`
- `cat reply.md | python .../lark_cli_helper.py reply-markdown --stdin`
- `cat doc.md | python .../lark_cli_helper.py docs-create --title "..." --stdin-markdown`
- `cat patch.md | python .../lark_cli_helper.py docs-update --doc "DOC_URL_OR_TOKEN" --mode append --stdin-markdown`
- `python .../lark_cli_helper.py drive-upload --file /absolute/path/to/file`

设计约束：

- 普通聊天回复仍然优先走 bridge 自身的发送链路。
- `lark-cli` 主要用于显式飞书动作，例如额外发消息、回复线程、创建/更新文档、上传文件。
- `lark-cli` 本身需要你先在本机完成登录授权。

## 运行

```bash
cd /Users/chenjunming/Desktop/KevinGit/AutoCodex/feishu_codex
uv run --python 3.9 --project . feishu-codex-bridge
```

如果你想先显式初始化配置：

```bash
cd /Users/chenjunming/Desktop/KevinGit/AutoCodex/feishu_codex
uv run --python 3.9 --project . feishu-codex-init
```

说明：

- `tmux` 后端依赖仓库里的旧 runtime 实现，这部分当前应使用 Python `3.9` 或 `3.10` 运行。
- 飞书接入层基于官方 `lark-oapi` SDK 的长连接模式。
- `tmux` runtime 文件默认落在 `FEISHU_CODEX_RUNTIME_DIR/tmux` 下，和桥接自身的 SQLite、日志分开管理。
- 如果没有找到配置且当前终端不是交互式模式，bridge 会直接退出，并提示先执行 `feishu-codex-init` 或设置 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。

如果要跑测试：

```bash
cd /Users/chenjunming/Desktop/KevinGit/AutoCodex/feishu_codex
uv run --python 3.9 --project . --extra dev pytest
```
