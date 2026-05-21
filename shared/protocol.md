# Deskmate IPC 协议（Single Source of Truth）

版本：`spec_version = 1`  
传输：`~/Library/Application Support/Deskmate/ipc.sock`（Unix Domain Socket），长连接，**换行分隔 JSON**。  
编码：`UTF-8`，`JSONEncoder.outputFormatting = []`（compact），键名 `snake_case`。  
相关冻结条款：V10 L1-A/B/C/F，V10 L3-D（IPC 层性能）。

---

## 1. 统一信封（Envelope）

**所有消息**共享同一外层结构。Python 与 Swift 双向使用。

```json
{
  "spec_version": 1,
  "type": "<EnvelopeType>",
  "trace_id": "<hex32>",
  "ts_ms": 1713600000000,
  "payload": { ... }
}
```

字段说明：

- **`spec_version`** (int, 必填)：破坏性协议升级时递增。消费端遇到 `spec_version > 支持版本` 必须发出结构化告警但**不得崩溃**。
- **`type`** (string, 必填)：枚举值见 §2。未知 type 仅记录日志，不抛异常（forward-compat）。
- **`trace_id`** (string, 必填)：32 位小写十六进制。每条用户触发的事件生成一个新的 `trace_id`，沿 Swift→bridge→agent→skill→LLM→bridge→Swift 全链路传递。
- **`ts_ms`** (int, 可选)：发送端本地毫秒时间戳。
- **`payload`** (object, 必填)：具体业务体；允许包含**未知键**（必须原样透传，不得丢弃）。

---

## 2. 信封类型（EnvelopeType）

### 2.1 Swift → Python（感知与用户事件）

| type | 说明 |
|---|---|
| `perception` | 用户状态快照（差分发送，见 V10 L3-D1） |
| `user.message` | 用户主动消息（对应即时响应链，V10 L2-#6） |
| `user.click_pet` | 点击桌宠 |
| `interaction` | typed 用户动作；payload 形状见 §4 `InteractionAction` |

### 2.2 Python → Swift（UI 指令）

| type | 说明 |
|---|---|
| `intent` | `CompanionIntent` 容器；payload 形状见 §5 |

> V10 L1-C 要求：**Python 端不直接生成 `pet.state / island.show` 等 UI 消息**，只产出 `intent`。Swift 端由 `IntentDispatcher` 翻译成 view 操作。

### 2.3 双向（生命周期）

| type | 方向 | 说明 |
|---|---|---|
| `ping` / `pong` | 双向 | 30s 心跳（V10 L3-D3）；有业务消息期间自动视为 alive。`pong` 响应跳过 50ms 合并窗口直接 flush（V10 §3.1 row 9 IPC p99 < 10ms 契约） |
| `state.snapshot.request` | Swift → Python | Swift 重连后主动拉当前 `DomainState` |
| `state.snapshot` | Python → Swift | `DomainState` 快照 |
| `agent.ready` | Python → Swift | 启动完成，可接收用户输入（V10 L3-B11） |
| `agent.pause` | Python → Swift | 屏幕睡眠等场景进入静默（V10 L3-A7） |
| `perf.metrics` | Swift → Python | Swift 侧 §3.1 hard budget 上报：唤醒延迟 + 帧掉帧率 |

---

## 3. `perception` payload

```json
{
  "user_state": "coding",
  "focus": "focused",
  "app": "com.microsoft.VSCode",
  "title": "main.py",
  "idle_ms": 2000
}
```

- **差分语义**：Swift 端对上一帧 `perception` 做 `Equatable` 比较，完全相同时不发送。
- **无活动阈值**：V10 L3-E1，idle 自适应间隔。

---

## 4. `interaction` payload —— `InteractionAction`

统一 typed 用户动作。V10 L1-F / I8。

```json
{
  "source": "island",
  "target": "session",
  "kind": "permission.resolve",
  "payload": { "allow": true }
}
```

- **`source`**: `pet | island | menu_bar`
- **`target`**: `session | reminder | skill | system | bubble`
- **`kind`**（显式枚举）：
  - `permission.resolve`
  - `question.answer`
  - `session.jump`
  - `task.open_detail`
  - `surface.dismiss`
  - `demo.trigger`
  - `pet.interact`
  - `pet.drag`
  - `pet.nest`
- **`payload`**：与 `kind` 对应的业务体；未知键保留。

`demo.trigger` 固定由开发期 Demo 面板发出，`target` 为 `system`，payload:

```json
{ "scenario": "build" }
```

`scenario` 当前支持 `build | approval | reminder | codex_session | clear`。

> **禁止**使用旧式 `user.island_action { id, action: "join" }` 字符串动作（V10 L1-F）。

---

## 5. `intent` payload —— `CompanionIntent`

```json
{
  "kind": "present_island",
  "payload": {
    "surface": "notification_card",
    "session_id": "abc",
    "ttl_ms": 8000
  }
}
```

- **`kind`**:
  - `show_pet_bubble` → payload 为 `BubbleSpec`（见 §6）
  - `set_pet_animation` → `{ "state": "thinking" }`
  - `set_avatar_mood` → `{ "mood": "alert" }`
  - `present_island` → `{ "surface": "<IslandSurfaceKind>", "session_id"?: "...", "activity_id"?: "..." }`
  - `update_island` → `{ "id": "...", "progress"?: 0.8, "title"?: "..." }`
  - `dismiss_island` → `{ "id": "..." }`

### 5.1 `IslandSurfaceKind`（V10 L1-E / I5）

五态枚举：

- `compact`
- `notification_card`
- `session_list`
- `live_activity`
- `empty`

### 5.2 `BubbleSpec`（V10 I3）

```json
{
  "id": "bubble-xyz",
  "kind": "chat",
  "icon": null,
  "text": "嗨，你回来啦",
  "markdown": null,
  "actions": [],
  "start_audio": null,
  "end_audio": null,
  "ttl_ms": 8000,
  "priority": "P2",
  "source_event_id": null
}
```

`kind` 至少预留：`chat | status | approval_hint | reminder | random_reaction | system`。

---

## 6. 合并与批处理（V10 L3-D2）

Swift 与 Python 两端均实现 50ms 合并窗口：窗口内多条 envelope 合并为一次 socket write（`\n` 分隔多个 JSON 对象）。

> **例外**：`pong` 走 fast-path，收到 `ping` 后立即 flush，不进入合并窗口。理由是 V10 §3.1 row 9 要求 IPC 心跳 p99 < 10ms，而合并窗口本身就是 50ms。心跳是 lifecycle 流量，不应被业务批处理拖延。

---

## 6a. `perf.metrics` payload（V10 §3.1 row 6 + row 8）

Swift 侧周期上报 hard budget 测量值：

```json
{
  "last_wake_seconds": 0.42,
  "total_frames": 600,
  "dropped_frames": 0,
  "frame_drop_ratio": 0.0
}
```

- **`last_wake_seconds`** (float | null)：从 `NSWorkspace.didWakeNotification` 到 SwiftUI 第一帧的秒数；首次启动时为 `null`（无前置 wake）。Hard budget：< 0.5s。
- **`total_frames`** / **`dropped_frames`** (int)：自上次重置以来的累计帧数 / 掉帧数。第一帧不计入 total（无 baseline）。
- **`frame_drop_ratio`** (float)：`dropped_frames / total_frames`，clamp 到 `[0, 1]`。Hard budget：0%。
- 上报频率：默认每 5 秒一次；agent 端只记 structlog，不影响业务路径。

---

## 6b. HookEvent v1 文件队列

外部 agent 第一版不连接 Swift IPC socket，而是通过 CLI 写入文件队列：

```bash
echo '{"session_id":"s1","event":"session.started","title":"Codex demo"}' \
  | deskmate hook ingest --source codex
```

归一化后的最小字段：

- `source`
- `event`
- `session_id`
- `title`
- `summary`
- `cwd`
- `jump_url`
- `phase`
- `approval_id`
- `prompt`
- `ts_ms`
- `raw`

默认队列目录为 `~/.deskmate/hook-events/`，可用 `DESKMATE_HOOK_EVENTS_DIR` 覆盖。未知 Codex payload 会保留到 `raw`，并生成一个可见 session，避免 hook 格式变化导致链路失败。

V1 提供 opt-in hook 管理命令，不会自动修改第三方配置：

- `deskmate hook install --source codex`
- `deskmate hook status --source codex`
- `deskmate hook uninstall --source codex`
- `--source claude | cursor` 同理

Codex 安装器管理 `~/.codex/config.toml` 的 `[features].codex_hooks = true` 和
`~/.codex/hooks.json` 中带 `Managed by Deskmate` 标记的 hook 条目。Claude 管理
`~/.claude/settings.json`，Cursor 管理 `~/.cursor/hooks.json`。卸载只移除
Deskmate 管理的条目，保留用户自定义 hooks。

---

## 6c. AgentRuntimeStatus 被动进程状态

Python agent 会只读扫描本机进程，把 IDE / agent CLI 映射成轻量 session。不会自动修改 Codex、Claude、Cursor、Windsurf 配置。

`active_sessions` 可选字段：

- `source`: `codex | claude_code | cursor | windsurf | vscode | xcode | jetbrains | terminal | opencode | unknown`
- `kind`: `gui_ide | cli_agent | hook_session`
- `process_id`: 进程 id；hook session 可为空
- `cwd`: 本地工作目录；只有能安全获得时填写
- `jump_url`: hook session 的跳转 URL；纯进程扫描通常为空

同一来源同时有 hook session 和进程 fallback 时，hook session 优先展示。

细粒度 `phase` 由 hook payload 归一化：

- `thinking`
- `editing`
- `running_tool`
- `testing`
- `waiting_for_approval`
- `waiting_for_answer`
- `running`
- `failed`
- `completed`

Hook payload 的处理链路为：

```text
source payload -> HookEvent -> AgentEvent -> AgentEventReducer -> SessionStore / ApprovalStore
```

`AgentEvent` 是后续 Codex app-server、Claude/Cursor 阻塞式审批、transcript discovery 共享的 reducer 输入层。当前事件类型：

- `SessionStarted`
- `SessionActivityUpdated`
- `PermissionRequested`
- `QuestionAsked`
- `SessionCompleted`
- `JumpTargetUpdated`

---

## 6d. Codex.app app-server

`python -m deskmate_agent` 默认尝试接入 Codex.app 的本地 app-server：

```bash
DESKMATE_CODEX_APP_SERVER=0 python -m deskmate_agent
```

上面的环境变量可关闭该接入。默认路径探测顺序：

- `DESKMATE_CODEX_APP_SERVER_PATH`
- `/Applications/Codex.app/Contents/Resources/codex`
- `~/Applications/Codex.app/Contents/Resources/codex`
- `PATH` 中的 `codex`

接入方式是独立 stdio JSON-RPC 进程，不复用 Swift IPC socket。事件处理链路：

```text
Codex app-server notification -> AgentEvent -> AgentEventReducer -> SessionStore / ApprovalStore
```

当前映射：

- `thread/started`: 非 ephemeral thread 生成 `SessionStarted`，带 `cwd` 和 `codex://threads/<id>`。
- `thread/status/changed`: `waitingOnApproval` 生成 `PermissionRequested`；`waitingOnUserInput` 生成 `QuestionAsked`；普通 active 生成 running update；system error 生成 failed completion。
- `thread/closed`: 生成 `SessionCompleted`。
- `turn/started`: 生成 running update。
- `turn/completed`: 根据 turn status 生成 completed / failed。

跳转规则继续由 Python session router 统一控制。允许的 URL scheme 包括
`codex`、`file`、`vscode`、`cursor`、`windsurf`；没有可用 `jump_url` 时才回退到本地 `cwd`。

---

## 7. 向前兼容规则（不可违反）

1. 新增字段**只能**加到 `payload` 内部或作为新的可选顶层字段。
2. 消费端遇到**未知字段**必须保留（pass-through），不得丢弃。
3. 消费端遇到**未知 `type`** 只记录结构化日志，不抛异常。
4. 所有枚举扩展必须单调追加，**禁止重命名或复用已有枚举值**。
5. `spec_version` 破坏性升级时必须在本文件附加"变更摘要"小节，并保留至少一个 release 的兼容读取。
