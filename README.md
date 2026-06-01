# OpenCode → Pi 会话迁移工具

将 OpenCode 的所有对话历史迁移到 Pi 的 JSONL 会话格式。

- **子代理内联嵌入**：遵循 Pi 的 `--no-session` 设计，不生成独立文件
- **`/tree` 可见**：subagent 内容通过 `custom_message` 在树视图中显示
- **增量迁移**：`--incremental` 模式只转换新会话
- **项目目录隔离**：按源工作目录自动分文件夹

## 使用文档

详细用法见 [SKILL.md](SKILL.md)。

## License

MIT
