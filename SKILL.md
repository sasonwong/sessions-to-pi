# OpenCode → Pi 会话迁移工具

将 OpenCode 的全部对话历史（SQLite）迁移到 Pi 的 JSONL 会话格式。

## 快速开始

```bash
cd /home/sason/life-project/opencode-migration
python3 migrate.py --all --incremental
```

## 前提

- OpenCode 数据库位于 `~/.local/share/opencode/opencode.db`（默认路径）
- Pi 会话目录为 `~/.pi/agent/sessions/`（默认路径）
- Python 3.10+

## 命令参考

### 全量迁移

```bash
# 转换所有会话
python3 migrate.py --all

# 只转换最近 10 个
python3 migrate.py --limit 10

# 跳过前 20 个
python3 migrate.py --start 20 --limit 10

# 只看某个项目目录的会话
python3 migrate.py --dir /home/sason/life-project --all
```

### 增量迁移（日常使用）

首次全量后，后续只需转新会话：

```bash
# 转换所有未迁移的会话
python3 migrate.py --all --incremental

# 转 5 个最新的未迁移会话
python3 migrate.py --incremental --limit 5

# 只转某项目的新会话
python3 migrate.py --dir /home/sason/work-project --all --incremental
```

增量模式会读取 `~/.pi/agent/sessions/.migration-state.json` 记录，跳过已迁移的会话，每成功转换一条就更新记录。

### 查看迁移状态

```bash
python3 migrate.py --status
```

输出示例：
```
OpenCode DB  : 382 sessions
Migrated     : 198 sessions (36010 msgs, 839 subagent inlines)
  (state has 200 entries, 2 files missing)
Remaining    : 182 sessions
Pi files size: 85.6 MB
Newest unconverted: "Code quality review Task 4 (@general subagent)"
```

### 预览（不写文件）

```bash
python3 migrate.py --dry-run --limit 10
python3 migrate.py --dry-run --all
```

预览会显示每个会话的消息数和 `custom_message` 条数。

## 输出产物

文件写入 `~/.pi/agent/sessions/`，按项目目录分文件夹：

```
~/.pi/agent/sessions/
├── --home-sason--/
│   └── 2026-05-30T02-57-28-301Z_xxx.jsonl
├── --home-sason-life-project-chat-history--/
│   └── 2026-05-30T02-58-26-201Z_xxx.jsonl
├── --home-sason-work-project-sgoa-visitor--/
│   └── ...
└── .migration-state.json
```

## 在 Pi 中使用

```bash
# 加载会话
pi --session ~/.pi/agent/sessions/--home-sason--/<文件名>.jsonl

# 或 fork 出来继续工作
pi --fork ~/.pi/agent/sessions/--home-sason-work-project-sgoa-visitor--/<文件名>.jsonl
```

迁移后的会话中，subagent（子代理）对话通过 `custom_message` 条目内联嵌入父会话，在 Pi 的 `/tree` 中可见，参与 LLM 上下文。

## 设计说明

- **无独立文件**：子代理内容不生成独立 JSONL，遵循 Pi 的 `--no-session` 设计
- **`/tree` 可见**：`custom_message` 在树视图中显示为分支
- **LLM 可读**：`role: custom` → `convertToLlm` → `role: user`
- **`_checkCompaction` 兼容**：所有 assistant 消息带 `usage` 字段
- **父会话引用**：子会话通过 `parentSession` 字段链接回父会话文件
