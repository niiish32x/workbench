# transcript-review

每天定时增量复盘 Claude Code 的终端对话：只提取人类和助手的文本，判断哪些值得长期保留，再通过 `obsidian-knowledge` CLI 写进 Obsidian vault。它不保存原始对话，也不做全量归档。

## 工作方式

1. 扫描 `~/.claude/projects/*/*.jsonl`，按每个文件的字节偏移只读**新增**内容；已看过的部分不会重复处理。
2. 只保留用户与助手的文本消息，工具调用和输出丢弃；本地做基础脱敏（`sk-`、`x-api-key`、Authorization、password 等）。
3. 拼成摘要后交给 headless `claude -p`，由它判断价值并写入 vault。
4. 写入规则：先 `search` 查重，再 `read` 旧笔记，同一主题合并进原笔记（只增不删），没有覆盖才新建；库的目录和写作规范以 vault 根目录的 `CLAUDE.md` 为准。
5. 单会话每轮最多 4 万字符，单轮总计 15 万字符，超出部分留到下一次。
6. 只有 Claude 成功退出后才推进偏移；失败不会丢内容，下次重试。

## 文件

- `review.py` — 主脚本，Python 3.9+，仅用标准库。
- `job-settings.json` — 传给 headless 会话的附加设置，目前是 `disableAllHooks`，避免无人值守时触发其它 hook。
- `launchd/com.shenghaoni.claude-transcript-review.plist` — macOS 定时任务（每天 23:47）。
- `state.json`、`digest.md`、`.lock` — 运行时生成，已在 `.gitignore` 中排除。

## 安装

```bash
# 1. 让运行时路径指向仓库（供 launchd 和旧路径使用）
ln -s ~/workbench/transcript-review ~/.claude/scripts/transcript-review

# 2. 安装定时任务（路径按实际用户名/位置调整）
cp transcript-review/launchd/com.shenghaoni.claude-transcript-review.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.shenghaoni.claude-transcript-review.plist
```

首次使用可选 `--init --recent-hours 24`：把历史 transcript 标记为已读，只复盘最近 24 小时的内容。

## 常用命令

```bash
/usr/bin/python3 ~/.claude/scripts/transcript-review/review.py            # 手动跑一次
/usr/bin/python3 ~/.claude/scripts/transcript-review/review.py --dry-run  # 只生成摘要，不调模型
launchctl bootout gui/$(id -u)/com.shenghaoni.claude-transcript-review    # 暂停
```

- 日志：`~/Library/Logs/claude-transcript-review.log`
- 调整判断标准：改 `review.py` 里的 `build_prompt()`，不用动调度。

## 注意

- Mac 在 23:47 睡眠时，launchd 会在唤醒后补跑。
- 脱敏是尽力而为；transcript 本身仍是明文，敏感凭据应改 key 并清理旧文件。
- vault 路径从 `~/.config/obsidian-knowledge/vaults.yaml` 读取，不写死在本仓库。
- 这一步依赖 `claude` 和 `obsidian-knowledge` 两个命令在 `PATH` 中（launchd 的 plist 里已显式设置）。
