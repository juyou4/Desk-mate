# Deskmate

macOS 桌面陪伴 Agent —— 桌宠与灵动岛协同双通道。

一个 Agent 内核，驱动两条信息输出通道：**桌宠**（陪伴）与**灵动岛**（事务）。
当前实现基线为 `deskmate-mvp-v10-unified`（见 `/Users/tanshuheng/.windsurf/plans/deskmate-mvp-v10-unified-6c686f.md`）。

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

## 角色合规声明

本项目为独立粉丝创作，保留像素风 / 暖色系 / 小体型萌感等通用审美特征，但物种、配色、标志性道具与命名均原创，不与任何第三方 IP 关联。

> This project is an independent fan work inspired by pixel-art AI mascot aesthetics. It is not affiliated with, endorsed by, or associated with any third-party trademark holders.

## 许可

GPL-3.0。贡献须保留原作者署名与非商业条款。
