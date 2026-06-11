# Deskmate

macOS 桌面陪伴 Agent —— 桌宠与灵动岛协同双通道。

一个 Agent 内核，驱动两条信息输出通道：**桌宠**（陪伴）与**灵动岛**（事务）。
当前实现基线为 `deskmate-mvp-v10-unified`（见 `/Users/tanshuheng/.windsurf/plans/deskmate-mvp-v10-unified-6c686f.md`）。

## 当前能力快照

- **桌宠对话入口**：宠物气泡内可直接输入消息并发送给 Python agent；菜单栏也保留快速输入框。
- **Agentic 工具调用**：支持 OpenAI-compatible Chat Completions，包括 DeepSeek 等兼容端点；工具调用可流式执行，并带超时与多轮工具调用上限。
- **低风险电脑控制**：可通过自然语言打开应用、URL、文件夹、Finder 定位、系统设置、网页搜索、调节音量、截图、锁屏/睡眠、退出应用等；高风险动作走显式 approval。
- **天气 / 倒计时 / 提醒**：可识别 `帮我看天气` 并打开 Weather app；可识别中文倒计时/提醒，例如 `帮我设置一个 3 分钟倒计时`。
- **记忆与任务**：持久化 chat/profile/task/tool action，上下文可跨 agent 重启恢复；任务和记忆写入默认需要明确用户意图或 approval。
- **灵动岛事务视图**：展示 approvals、build/live activity、notification、agent sessions、active tasks；`thinking` 和最近 `completed` 的 session 会在岛上保留展示。
- **角色包与状态动画**：像素桌宠支持 idle/running/thinking/waiting/dozing/sleeping/failed 等状态，内置多套 OpenPets 风格变体资源。

## LLM / DeepSeek 配置

Python agent 使用 OpenAI-compatible `/chat/completions` 接口。不要把真实 API key 写入仓库；本地用环境变量或 `launchctl setenv` 配置：

```bash
export DESKMATE_LLM_API_KEY='...'
export DESKMATE_LLM_BASE_URL='https://api.deepseek.com'
export DESKMATE_LLM_MODEL='deepseek-v4-flash'
export DESKMATE_LLM_STREAMING=1
export DESKMATE_LLM_TOOL_TIMEOUT_S=15
export DESKMATE_LLM_TOOL_ROUND_LIMIT=3
```

## 仓库结构

```
deskmate/
├── DeskmateApp/        # Swift 壳层（Pet / Island / MenuBar / Perception / IPC）
├── agent/              # Python Agent Core
├── shared/             # IPC 协议 / JSON Schema 的单一事实源
├── assets/             # 角色人设、示例资源
└── scripts/            # 构建 / 安装 / 性能冒烟
```

## 架构不变量（V10 L1）

1. **单一状态源**：Pet / Island / MenuBar 不维护独立真值，只订阅 `CompanionStateStore`。
2. **Domain vs Surface 分层**：`DomainState` 与 `PetPresentationState / IslandSurfaceState / MenuBarState` 分离。
3. **事件路由输出中间态**：路由只产出 `CompanionIntent`，不直接渲染视图。
4. **角色包 manifest-first**：以 `character.json` 为入口，图片目录只是资源层。
5. **岛 Surface/Module 分层**：`IslandSurface` 枚举页面，`IslandModule` 协议插拔部件。
6. **所有用户动作 typed**：统一 `InteractionAction`，不留字符串 action。

所有 Codable / Pydantic 结构均 **forward-compatible**：未知字段忽略，`spec_version` 必存。

## 本地开发

Python：

```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Swift：

```bash
cd DeskmateApp
# Xcode-equipped machines:
swift test
# CLI-only machines (Command Line Tools don't ship XCTest):
swift run DeskmateCoreSmoke
```

`DeskmateCoreSmoke` 是一个可执行的 Phase 0 验收程序，断言与 `Tests/DeskmateCoreTests/` 对齐，覆盖 envelope round-trip / forward-compat / IslandSurface L1-E / 角色包 manifest / trace_id 任务本地传递。

常用端到端验证：

```bash
cd agent
pytest
ruff check .

cd ../DeskmateApp
swift run DeskmateCoreSmoke
swift build --product DeskmateMenuBarApp
```

## 角色合规声明

本项目为独立粉丝创作，保留像素风 / 暖色系 / 小体型萌感等通用审美特征，但物种、配色、标志性道具与命名均原创，不与任何第三方 IP 关联。

> This project is an independent fan work inspired by pixel-art AI mascot aesthetics. It is not affiliated with, endorsed by, or associated with any third-party trademark holders.

## 许可

GPL-3.0。贡献须保留原作者署名与非商业条款。
