# Sessions → Pi

把 OpenCode 与 DeepSeek 网页版的对话历史迁移成 Pi 的 JSONL 会话。

## OpenCode → Pi

`migrate.py` 读取 OpenCode 的 SQLite 数据库，按源项目目录分文件夹写入 Pi 会话。

```bash
python3 migrate.py --all --incremental      # 日常：只转未迁移的会话
python3 migrate.py --all                    # 全量（默认只转 3 个）
python3 migrate.py --dir <目录前缀> --all
python3 migrate.py --status                 # 迁移进度
python3 migrate.py --dry-run --limit 10     # 预览，不写文件
```

- 数据库固定在 `~/.local/share/opencode/opencode.db`，会话目录 `~/.pi/agent/sessions/`。
- 增量状态在 `~/.pi/agent/sessions/.migration-state.json`，每成功一条即更新。
- subagent 对话不生成独立文件，用 `custom_message` 内联进父会话：`/tree` 可见，并且参与模型上下文。
- assistant 消息带 `usage`，兼容 Pi 的压缩检查；子会话通过 `parentSession` 指回父文件。

## DeepSeek 网页导出 → Pi

`deepseek.py` 读取 DeepSeek 官方的 `deepseek_data-<date>.zip`（`export_format_version: 3`）。

```bash
python3 deepseek.py                # 全量导入
python3 deepseek.py --zip <path>   # 指定导出包
python3 deepseek.py --limit 5      # 只导最近 5 个未导入会话
python3 deepseek.py --dry-run      # 只看数量，不写文件
```

导出包默认在 `~/Downloads` 和 WSL 的 `/mnt/c/Users/*/Downloads` 里查找。

输出：

| 路径 | 内容 |
|---|---|
| `~/.pi/agent/sessions/--<编码后的 ~/chat>--/*.jsonl` | Pi 会话，完整消息树：重生成/编辑留下的分支也导入，`/tree` 可见；打开时停在导出时的当前分支 |
| `~/chat/archive/*.md` | 检索视图：只跟活跃分支，含思考（折叠）与联网来源 |
| `~/chat/archive/INDEX.md` | 全量索引（日期 · 标题 · 首问） |
| `~/.pi/agent/sessions/.deepseek-state.json` | id → 产物，重复运行只导新增 |

映射：

- `REQUEST` → 用户文本；`FILE` → 附件文件名与大小（导出包不含内容）
- `THINK` → assistant 的 thinking block（本机可见；`openai-completions` 回放时不发送）
- `RESPONSE` / `TEMPLATE_RESPONSE` → 文本
- `TOOL_SEARCH` / `TOOL_OPEN` / `READ_LINK` → 只进 markdown 的来源列表，不进会话上下文
- `CONTENT_RISK` 丢弃；空回复（只有搜索步骤或被中断）跳过，子消息接到最近的非空祖先

`STATE_VERSION` 变化时先删除旧状态里记录的产物，再重新导入。

## License

MIT
