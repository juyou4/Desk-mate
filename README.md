# Deskmate

macOS agentic 桌面陪伴项目：一个 Python Agent Core，驱动两条用户界面通道：

- **桌宠**：负责陪伴、对话、情绪反馈、气泡输入、提醒和轻量互动。
- **灵动岛**：负责事务状态、agent 会话、approval、build/live activity、任务进度和系统通知。

当前分支重点是 `island-polish-enhancements`：让桌宠可以直接对话，让岛成为可观察的 agent cockpit，并把 agentic 工具调用接到真实 macOS 操作上。

## 最新能力快照

### 桌宠

- 宠物气泡可以直接输入消息，发送到 Python agent。
- 菜单栏仍保留快速输入框，适合调试或桌宠被隐藏时使用。
- 气泡支持拖拽调整大小，适合长回复和多轮对话。
- 支持角色包和状态动画：`idle`、`working`、`thinking`、`waiting`、`dozing`、`sleeping`、`failed` 等状态会映射到不同表现。
- pending reminder、approval、agent 回复、错误提示都会走统一气泡队列，避免不同通道互相覆盖。

### 灵动岛

- 展示 approvals、build/live activity、notification、agent sessions、active tasks。
- 识别并展示细粒度 agent phase：
  - `thinking`
  - `editing`
  - `running_tool`
  - `testing`
  - `completed`
  - `failed`
  - `waiting_for_approval`
  - `waiting_for_answer`
- compact 态会优先显示非普通 phase，比如 `PLAN`、`TOOL`、`TEST`、`DONE`、`FAIL`。
- phase 变化时会短暂触发类似 Dynamic Island 的 peek：岛会扩宽一小段时间显示完整状态和上下文。
- 展开态显示 session 列表、来源、工作目录、pid、phase source、跳回入口和需要用户回答的 inline 输入。
- 最近完成的 closed session 会短时间保留，避免 `completed` 一闪而过。
- passive runtime scanner 不再把 app-server/hook 观察到的 `thinking/completed/testing` 覆盖回默认 `running`。

### Agentic 能力

- OpenAI-compatible Chat Completions，支持 DeepSeek、OpenAI、OpenRouter、vLLM、Ollama `/v1` shim 等兼容端点。
- 支持 streaming 和 non-streaming 两条路径。
- 支持 tool calls，并有每轮工具调用上限和单工具超时。
- 支持多轮工具链路，例如先查记忆/任务，再创建提醒或日历事件。
- 支持本地持久记忆、任务、工具调用审计和工具经验回忆。
- 支持低风险 macOS 操作：
  - 打开 app：Terminal、Finder、Safari、Chrome、Cursor、Windsurf、VSCode、Xcode、iTerm、Ghostty、Weather 等。
  - 聚焦 app。
  - 打开 URL。
  - 打开本地文件夹/文件。
  - 用指定 app 打开路径。
  - Finder 中定位文件。
  - 打开 allowlisted System Settings 页面。
  - 网页搜索。
  - 音量控制：mute、unmute、set volume。
  - 打开 Weather app 看天气。
- 支持需要显式 approval 的操作：
  - 截图。
  - 写剪贴板。
  - 锁屏。
  - 睡眠。
  - 退出应用。
  - 记忆/任务的持久写入建议。
- 支持提醒和倒计时：
  - `remind me to stretch in 10 minutes`
  - `timer for 5 minutes`
  - `帮我设置一个 3 分钟倒计时`
  - `10分钟后提醒我喝水`
  - `what reminders do I have?`
  - `cancel reminder <reminder_id>`
- 支持日历事件：
  - LLM tool `deskmate_create_calendar_event` 会通过固定 AppleScript 写入 macOS Calendar。
  - 只有用户明确要求创建/安排日历事件时才允许调用。
- 支持本地任务/todo：
  - `add task Polish island`
  - `list tasks`
  - `search tasks island`
  - `continue current task`
  - `complete task <task_id>`
- 支持 agent/runtime 观察：
  - passive scanner 识别 CLI agent、IDE、terminal、pid、cwd、tty。
  - Codex app-server / hook / transcript observer 可以把更精确 phase 投影到岛上。
  - phase source 会保存在 session extras，便于区分 `unobserved`、hook、app-server 等来源。

## DeepSeek / LLM 配置

不要把真实 API key 写进仓库。推荐用环境变量配置 Python agent。

DeepSeek 官方 OpenAI-compatible base URL 是 `https://api.deepseek.com`，当前官方模型名包括 `deepseek-v4-flash` 和 `deepseek-v4-pro`。DeepSeek 文档说明旧模型名 `deepseek-chat` / `deepseek-reasoner` 会在 **2026-07-24 15:59 UTC** 停用；现在建议直接使用 V4 模型名。

参考：

- [DeepSeek API Docs](https://api-docs.deepseek.com/)
- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)

本地 shell 启动 agent 时：

```bash
export DESKMATE_LLM_API_KEY='你的 DeepSeek API key'
export DESKMATE_LLM_BASE_URL='https://api.deepseek.com'
export DESKMATE_LLM_MODEL='deepseek-v4-flash'
export DESKMATE_LLM_STREAMING=1
export DESKMATE_LLM_TOOL_TIMEOUT_S=15
export DESKMATE_LLM_TOOL_ROUND_LIMIT=3
```

如果用 `launchctl` 或 GUI 方式启动，需要把环境变量放到用户 launch session：

```bash
launchctl setenv DESKMATE_LLM_API_KEY '你的 DeepSeek API key'
launchctl setenv DESKMATE_LLM_BASE_URL 'https://api.deepseek.com'
launchctl setenv DESKMATE_LLM_MODEL 'deepseek-v4-flash'
launchctl setenv DESKMATE_LLM_STREAMING '1'
launchctl setenv DESKMATE_LLM_TOOL_TIMEOUT_S '15'
launchctl setenv DESKMATE_LLM_TOOL_ROUND_LIMIT '3'
```

可选模型分流：

```bash
export DESKMATE_LLM_MODEL_REACTIVE='deepseek-v4-flash'
export DESKMATE_LLM_MODEL_PROACTIVE='deepseek-v4-flash'
```

注意：

- 当前代码走 OpenAI `/chat/completions` 格式。
- DeepSeek thinking mode 支持 tool calls，但 reasoning 内容的多轮回传规则比普通模型更严格；如果遇到工具链路异常，优先用 `deepseek-v4-flash` 默认配置或关闭 thinking 相关自定义。
- 没配置 API key 时，本地 deterministic 能力仍可用，例如提醒、任务、部分电脑控制、runtime scan、hook ingest 等。

## 快速运行

### 1. 安装 Python agent

```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,runtime]'
```

### 2. 启动 Python agent

```bash
cd agent
source .venv/bin/activate
python -m deskmate_agent.main
```

Python agent 默认监听：

```text
~/Library/Application Support/Deskmate/ipc.sock
```

可以用 `DESKMATE_SOCKET_PATH` 覆盖 socket，用 `DESKMATE_DB_DIR` 覆盖 SQLite 目录。

### 3. 启动 Swift 桌宠/菜单栏/岛

另开一个终端：

```bash
cd DeskmateApp
swift run DeskmateMenuBarApp
```

也可以只跑 headless shell：

```bash
cd DeskmateApp
swift run DeskmateShellApp
```

### 4. 验证连接

```bash
cd agent
deskmate hook doctor
deskmate runtime scan
deskmate tail-status
```

如果 `deskmate` console script 不在 PATH，使用模块入口：

```bash
python -m deskmate_agent.cli hook doctor
python -m deskmate_agent.cli runtime scan
python -m deskmate_agent.cli tail-status
```

## 常用调试命令

### Build / test live activity

这些命令会写 `~/.deskmate/build-status.json`，running agent 会转成岛上的 build pill：

```bash
deskmate build-start "pytest"
deskmate build-progress "pytest" 0.42 --message "420/1000"
deskmate build-done "pytest" --message "992 passed"
deskmate build-failed "pytest" --message "1 failed"
deskmate build-dismiss
```

### Hook ingest

外部 agent hook 不应该直接写 Swift socket，而是写入 Python agent 监听的 file queue：

```bash
echo '{"session_id":"s1","event":"session.started","title":"Codex demo"}' \
  | deskmate hook ingest --source codex
```

常见事件会被归一化成 session phase：

```text
session.started          -> running
message/thinking event   -> thinking
tool.start               -> running_tool 或 testing
file/edit event          -> editing
turn.completed           -> completed
error/failed event       -> failed
approval/question        -> waiting_for_approval / waiting_for_answer
```

### Runtime scan

```bash
deskmate runtime scan
deskmate runtime scan --json
/bin/ps -axo pid=,ppid=,tty=,comm=,args= > /tmp/deskmate-ps.txt
deskmate runtime scan --ps-file /tmp/deskmate-ps.txt --json
```

### Memory / task diagnostics

```bash
deskmate memory summary
deskmate memory summary --json
deskmate memory task <task_id>
deskmate memory task-context <task_id>
deskmate memory tool-task <tool_task_id>
deskmate today
deskmate today --json
```

## 架构

```text
User
  ├─ pet bubble input
  ├─ menu bar input
  └─ island actions
       │
       ▼
Swift DeskmateMenuBarApp
  ├─ PetOverlay / BubbleView
  ├─ IslandOverlay / IslandWindowController
  ├─ DeskmateShell
  ├─ Live stores
  └─ BridgeClient
       │ newline-delimited JSON envelope over Unix socket
       ▼
Python deskmate_agent
  ├─ BridgeServer
  ├─ Dispatcher
  ├─ Reactive chat chain
  ├─ LLM composer + tool executor
  ├─ Session / Approval / Reminder / Task stores
  ├─ Runtime observers
  ├─ Hook queues
  └─ Memory/tool-action SQLite
```

## 仓库结构

```text
deskmate/
├── DeskmateApp/        # Swift 壳层：Pet / Island / MenuBar / Perception / IPC
├── agent/              # Python Agent Core
├── shared/             # IPC 协议 / JSON Schema 的单一事实源
├── assets/             # 角色人设、示例资源
└── scripts/            # 构建 / 安装 / 性能冒烟
```

## 核心状态与通道

### Domain vs Surface

`DomainState` 是业务状态，不直接等于 UI。Swift 和 Python 都遵循：

- domain：用户状态、前台 app、活跃 session、degradation level。
- pet surface：桌宠动画、气泡、情绪、attention。
- island surface：compact、session list、notification card、live activity。
- menu bar：调试入口、列表、配置。

### Session phase

Python `SessionInfo.phase` 是岛判断 agent 进度的关键字段。当前支持：

```text
waiting_for_approval
waiting_for_answer
thinking
editing
running_tool
testing
running
completed
failed
unknown
```

Passive scanner 只能知道“某个 agent 进程还在运行”，因此默认 phase 是 `running`。Hook、Codex app-server、transcript observer 才能给出更精确 phase。现在 scanner 会保留这些精确 phase，避免下一次进程扫描把它覆盖回 `running`。

### Island priority

岛内容优先级大致为：

```text
approval > build live activity > notification > visible sessions > active task > idle
```

Session 列表内部再按 phase 排序，等待用户动作优先，进行中其次，最近 completed/failed 可短暂保留。

## 安全边界

Deskmate 当前不是一个任意 shell runner。

- 不提供任意命令执行 tool。
- 不自动点击 UI、不自动输入到第三方 app。
- 不自动改文件。
- 高风险本地动作必须 approval。
- 记忆和任务的持久写入建议必须 approval，除非用户用明确命令表达。
- 工具调用参数会做审计记录，secret-like 字段会在持久化前 redaction。
- 旧的 interrupted `running` tool task 在重启后只会标记失败，不会自动重放。

## 测试与验证

Python：

```bash
cd agent
.venv/bin/pytest
.venv/bin/ruff check .
```

Swift：

```bash
cd DeskmateApp
swift run DeskmateCoreSmoke
swift build --product DeskmateMenuBarApp
```

完整常用验证：

```bash
cd agent
.venv/bin/pytest
.venv/bin/ruff check .

cd ../DeskmateApp
swift run DeskmateCoreSmoke
swift build --product DeskmateMenuBarApp
```

说明：

- `DeskmateCoreSmoke` 是 CLI 可跑的 Swift acceptance probe，不依赖完整 Xcode XCTest。
- `swift test` 也可用，但需要完整 Xcode toolchain。
- 从仓库根目录直接跑 `agent/.venv/bin/pytest` 会绕过 `agent/pyproject.toml` 的 pytest 配置，async 测试可能被错误处理；推荐在 `agent/` 目录里跑。

## 常见问题

### 岛只显示 running

先确认 Python agent 收到了 hook/app-server/transcript 事件。只靠 passive process scan 无法知道 agent 正在 thinking 还是 testing。

```bash
deskmate tail-status
deskmate runtime scan --json
deskmate hook doctor
```

如果 session 的 `phase_source` 是 `unobserved`，说明只看到了进程，没有看到精确事件。

### 桌宠不回复或回复一半

检查：

- `DESKMATE_LLM_API_KEY` 是否在 Python agent 进程环境里。
- `DESKMATE_LLM_BASE_URL` 是否正确。
- `DESKMATE_LLM_MODEL` 是否是当前服务端支持的模型。
- tool timeout 是否过短。
- `deskmate tail-status` 是否有 error intent。

### Swift 连接不上 Python

检查两边 socket 是否一致：

```bash
echo "$DESKMATE_SOCKET_PATH"
python -m deskmate_agent.cli hook doctor
```

默认 socket 是：

```text
~/Library/Application Support/Deskmate/ipc.sock
```

### DeepSeek 不走工具调用

确认使用支持 tool calls 的模型，例如 `deepseek-v4-flash` 或 `deepseek-v4-pro`。DeepSeek 官方文档显示 V4 Flash/Pro 支持 Tool Calls；某些临时或 speciale 模型可能不支持工具调用。

## 角色合规声明

本项目为独立粉丝创作，保留像素风 / 暖色系 / 小体型萌感等通用审美特征，但物种、配色、标志性道具与命名均原创，不与任何第三方 IP 关联。

> This project is an independent fan work inspired by pixel-art AI mascot aesthetics. It is not affiliated with, endorsed by, or associated with any third-party trademark holders.

## 许可

GPL-3.0。贡献须保留原作者署名与非商业条款。
