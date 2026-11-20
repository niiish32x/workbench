#!/usr/bin/env python3
"""Incrementally review Claude Code terminal transcripts and file durable
knowledge into the Obsidian vault.

Scans only the bytes appended since the last successful run, keeps human and
assistant text (tool calls/results are dropped), redacts obvious secrets, and
hands a digest to a headless Claude session that writes through the
`obsidian-knowledge` CLI. State advances only after a successful run.

Usage:
  review.py            # normal incremental run
  review.py --dry-run  # build the digest, print stats, write nothing
  review.py --init     # seed offsets for old transcripts (skip history)
"""

import argparse
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".claude", "scripts", "transcript-review")
PROJECTS = os.path.join(HOME, ".claude", "projects")
STATE_PATH = os.path.join(BASE, "state.json")
LOCK_PATH = os.path.join(BASE, ".lock")
DIGEST_PATH = os.path.join(BASE, "digest.md")
LOG_PATH = os.path.join(HOME, "Library", "Logs", "claude-transcript-review.log")
CLAUDE_BIN = "/opt/homebrew/bin/claude"
JOB_SETTINGS = os.path.join(BASE, "job-settings.json")

PER_SESSION_CHARS = 40000
TOTAL_CHARS = 150000
MAX_MESSAGE_CHARS = 3000
MIN_ASSISTANT_CHARS = 30
TIMEOUT_S = 1800

INJECTED_PREFIXES = (
    "<hook_prompt",
    "<environment_context>",
    "<task-notification>",
    "<teammate-message",
    "<command-name>",
    "<command-message>",
    "<local-command",
    "Another Claude session sent a message:",
    "# AGENTS.md instructions for ",
)

REDACTIONS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)(x-api-key\s*:\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|auth[_-]?token|access[_-]?token|secret|password)"
            r"\s*[:=]\s*[\"']?)[^\s\"',}]{8,}"
        ),
        r"\1[REDACTED]",
    ),
]


def log(message):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_vault_root():
    path = os.path.join(HOME, ".config", "obsidian-knowledge", "vaults.yaml")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        raw = stripped[2:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                return json.loads(raw)
            except ValueError:
                return raw.strip('"')
        return raw.strip("'")
    return None


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"files": {}}


def save_state(state):
    os.makedirs(BASE, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, STATE_PATH)


def discover_transcripts():
    found = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        if "transcript-review" in path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_size == 0:
            continue
        found.append((stat.st_mtime, path))
    found.sort()
    return found


def read_new_lines(path, offset):
    """Return [(line_start_offset, raw_line)] and the safe end offset."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    complete = data[: end + 1]
    out = []
    pos = 0
    for raw in complete.split(b"\n"):
        if raw:
            out.append((offset + pos, raw))
        pos += len(raw) + 1
    return out, offset + end + 1


def entry_text(entry):
    if not isinstance(entry, dict) or entry.get("isMeta"):
        return None
    typ = entry.get("type")
    message = entry.get("message")
    if typ not in ("user", "assistant") or not isinstance(message, dict):
        return None
    content = message.get("content")
    if typ == "user":
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            if any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                return None
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            return None
        role = "USER"
    else:
        if not isinstance(content, list):
            return None
        text = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        role = "ASSISTANT"
    text = text.strip()
    if not text:
        return None
    if text.startswith(INJECTED_PREFIXES):
        return None
    if role == "ASSISTANT" and len(text) < MIN_ASSISTANT_CHARS:
        return None
    return role, text


def redact(text):
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def session_meta(path, mtime):
    parts = path.split(os.sep)
    session_id = os.path.splitext(parts[-1])[0]
    project = parts[-2]
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    return session_id, project, stamp


def build_digest(files, state):
    chunks = []
    consumed = {}
    total = 0
    empty_files = 0

    for mtime, path in files:
        if total >= TOTAL_CHARS:
            break
        record = state.get("files", {}).get(path) or {}
        try:
            offset = int(record.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            size = os.stat(path).st_size
        except OSError:
            continue
        if size < offset:
            offset = 0
        lines, safe_end = read_new_lines(path, offset)
        if not lines:
            if safe_end != offset:
                consumed[path] = safe_end
            continue

        session_id, project, stamp = session_meta(path, mtime)
        accepted = []
        used = 0
        last_offset = offset

        for line_start, raw in lines:
            line_end = line_start + len(raw) + 1
            try:
                entry = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                last_offset = line_end
                continue
            got = entry_text(entry)
            if got is None:
                last_offset = line_end
                continue
            role, text = got
            text = redact(text)
            if len(text) > MAX_MESSAGE_CHARS:
                text = text[:MAX_MESSAGE_CHARS] + "\n…[截断]"
            block = "### %s [%s]\n%s\n\n" % (
                role,
                entry.get("timestamp") or "",
                text,
            )
            if used + len(block) > PER_SESSION_CHARS:
                break
            accepted.append(block)
            used += len(block)
            last_offset = line_end

        consumed[path] = last_offset
        if accepted:
            header = "## 会话 %s | 项目 %s | %s\n" % (session_id, project, stamp)
            chunk = header + "".join(accepted)
            chunks.append(chunk)
            total += len(chunk)
        else:
            empty_files += 1

    digest = (
        "# Claude 终端对话摘录\n"
        "生成时间: %s\n"
        "说明: 仅用户与助手的文本消息；工具调用/输出已省略；疑似密钥已脱敏。\n"
        "片段数: %d\n\n" % (time.strftime("%Y-%m-%d %H:%M"), len(chunks))
    ) + "".join(chunks)
    stats = {
        "sessions": len(chunks),
        "chars": len(digest),
        "empty_files": empty_files,
        "total_scanned": len(consumed),
    }
    return digest, consumed, stats


def build_prompt(digest_path, vault_root):
    return """你是 Obsidian 知识库的整理员。请阅读对话摘录文件：
{digest}

摘录来自用户在终端里与 Claude 的对话（已脱敏，工具输出已省略）。任务：只把真正值得长期复用的内容沉淀进 vault，其余一律忽略。

【必须遵守】
0. 第一步读 `{vault}/CLAUDE.md`。库的目录职责、命名、写法、边界以它为准；下面所有默认路径若与它冲突，一律听 CLAUDE.md。
1. 先查重、再合并、最后才新建。对每条候选内容：
   - 用 `obsidian-knowledge search "<关键词>"` 找主题相同或相近的已有笔记；
   - 找到后用 `obsidian-knowledge read` 读全文，把新结论**并入**原笔记，再用 `obsidian-knowledge write --replace` 写回；
   - 同一主题禁止另起新笔记。只有确实没有任何相关笔记时才新建；
   - 合并只增不删：保留原文的措辞、细节和既有结论；新结论与旧结论冲突时并列写出，并标注日期，不要静默覆盖；保留 created/updated 字段；补 [[wikilinks]]。
2. 只通过 `obsidian-knowledge` CLI 读写 vault，不要用 Write/Edit 直接改 vault 文件。
3. 笔记必须自包含，脱离本次对话也能读懂；遵循库里的写作模板（一句话摘要 + 问题 / 方法 / 结论 / 我的思考）。「我的思考」留空写「（留给你补充）」，绝不代写。
4. 不删除笔记、不合并改名、不新建未经确认的目录。不确定归属时追加到 `00-收集箱.md`。
5. 默认不写。满足以下之一才考虑写入：
   - 可复用的技术知识、操作步骤、踩坑与修复方法
   - 用户明确表达的长期偏好或研究结论
   - 系统/配置/插件/流程的结构性变更（需要留痕时写一条 changelog 片段）
   - 环境或机器上稳定的事实（写入宿主 memory 目录，每文件一条事实，200 字以内）
6. 明确排除：一次性新闻、闲聊、临时状态、测试结果、已在代码/仓库/现有笔记中的内容、单纯的时效性问答（例如某公司是否上市）。与代码仓库相关的知识不写本库（放项目自己的 docs/ 或 CLAUDE.md）。
7. 单轮最多写 5 个文件；宁可少写，不要凑数。
8. 不要修改 CLAUDE.md 或 AGENTS.md。

【vault 根目录】
{vault}

【目录摘要（以 CLAUDE.md 为准）】
- 学习类：01-学习/论文、01-学习/新技术、01-学习/概念笔记、01-学习/文章
- 面试类：02-面试/<子目录>；长期内容在另一个库 ~/interview，不复制
- 生活类：03-生活/财务、03-生活/健康、03-生活/成长反思
- 索引：90-索引 下对应的索引页，新笔记创建后顺手更新
- 模板：99-模板（只读，不写正式内容）
- 插件自身状态：wiki/systems/knowledge-base/PAPERCUTS.md、Utility/obsidian-knowledge/changelog/
- 普通笔记命名：YYYY-MM-DD-标题.md

【输出】
结束时只输出一行：
- 写了文件：WROTE: <路径1>, <路径2>
- 没写：NO_CAPTURE: <一句话原因>
""".format(digest=digest_path, vault=vault_root)


def run_claude(prompt):
    command = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format",
        "text",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Glob,Grep,Skill,Bash(obsidian-knowledge *),Bash(mkdir -p *)",
        "--settings",
        JOB_SETTINGS,
        "--no-session-persistence",
        "--effort",
        "medium",
    ]
    env = os.environ.copy()
    env["PATH"] = ":".join(
        [
            "/opt/homebrew/bin",
            os.path.join(HOME, ".local", "bin"),
            env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        ]
    )
    return subprocess.run(
        command,
        cwd=BASE,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )


def init_state(files, recent_hours):
    now = time.time()
    state = {"files": {}}
    seeded = 0
    pending = 0
    for mtime, path in files:
        try:
            size = os.stat(path).st_size
        except OSError:
            continue
        if now - mtime <= recent_hours * 3600:
            pending += 1
            continue
        state["files"][path] = {"offset": size}
        seeded += 1
    save_state(state)
    log(
        "init: seeded %d old transcripts, left %d recent transcript(s) for review"
        % (seeded, pending)
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--recent-hours", type=float, default=24.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.makedirs(BASE, exist_ok=True)
    lock_handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another review is already running; exiting")
        return 0

    files = discover_transcripts()
    if args.init:
        init_state(files, args.recent_hours)
        return 0

    vault_root = load_vault_root()
    if not vault_root or not os.path.isdir(vault_root):
        log("vault root not found in ~/.config/obsidian-knowledge/vaults.yaml")
        return 1

    state = load_state()
    digest, consumed, stats = build_digest(files, state)
    log(
        "scanned %d changed transcript(s): %d session(s), %d chars, %d without text"
        % (
            stats["total_scanned"],
            stats["sessions"],
            stats["chars"],
            stats["empty_files"],
        )
    )

    if stats["sessions"] == 0:
        if not args.dry_run:
            for path, offset in consumed.items():
                state.setdefault("files", {})[path] = {"offset": offset}
            state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
        log("no new conversation text; nothing to review")
        return 0

    if args.dry_run:
        with open(DIGEST_PATH, "w", encoding="utf-8") as fh:
            fh.write(digest)
        log("dry run: digest written to %s" % DIGEST_PATH)
        return 0

    with open(DIGEST_PATH, "w", encoding="utf-8") as fh:
        fh.write(digest)

    prompt = build_prompt(DIGEST_PATH, vault_root)
    started = time.time()
    try:
        result = run_claude(prompt)
    except subprocess.TimeoutExpired:
        log("claude timed out after %ds; state not advanced" % TIMEOUT_S)
        return 1

    duration = int(time.time() - started)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-5:]
        log("claude failed (exit %d) in %ds: %s" % (result.returncode, duration, " | ".join(tail)))
        return 1

    answer = (result.stdout or "").strip()
    summary = answer.splitlines()[-1] if answer else "(no output)"
    log("claude finished in %ds: %s" % (duration, summary))

    for path, offset in consumed.items():
        state.setdefault("files", {})[path] = {"offset": offset}
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    try:
        os.remove(DIGEST_PATH)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
