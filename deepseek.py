#!/usr/bin/env python3
"""DeepSeek 网页导出（export_format_version 3）→ Pi 会话 + markdown 档案。

产物：
  ~/.pi/agent/sessions/--<编码后的 ~/chat>--/<time>_<uuid>.jsonl   可续聊的 Pi 会话（完整树，含放弃的分支）
  ~/chat/archive/<date>-<slug>-<id8>.md                           检索视图（只跟活跃分支，含思考与来源）
  ~/chat/archive/INDEX.md                                         全量索引
  ~/.pi/agent/sessions/.deepseek-state.json                       id → 产物（重跑幂等）

用法：
  python3 deepseek.py [--zip PATH] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CHAT_CWD = os.path.join(HOME, "chat")
ARCHIVE_DIR = os.path.join(CHAT_CWD, "archive")
AGENT_DIR = os.path.join(HOME, ".pi", "agent")
STATE_FILE = os.path.join(AGENT_DIR, "sessions", ".deepseek-state.json")
DEFAULT_ZIP_DIRS = ("~/Downloads", "/mnt/c/Users/*/Downloads")

PROVIDER = "deepseek"
MODEL = "deepseek-web"
API = "openai-completions"
STATE_VERSION = 2


# ── helpers ──────────────────────────────────────────────────────────

def session_dir_for_cwd(cwd: str) -> str:
    return "--" + cwd.strip("/").replace("/", "-").replace(":", "-") + "--"


SESSION_DIR = os.path.join(AGENT_DIR, "sessions", session_dir_for_cwd(CHAT_CWD))


def parse_ts(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_ms(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def make_eid() -> str:
    return uuid.uuid4().hex[:8]


def slugify(title: str, sid: str) -> str:
    slug = re.sub(r'[\\/:*?"<>|\s]+', "-", title).strip("-")
    return (slug[:60].rstrip("-") or "untitled") + "-" + sid[:8]


def fmt_size(n) -> str:
    if not isinstance(n, (int, float)) or n <= 0:
        return "大小未知"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def zero_usage() -> dict:
    return {
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
            if isinstance(st.get("migrated"), dict):
                return st
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": STATE_VERSION, "migrated": {}}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ── DeepSeek → 中间表示 ──────────────────────────────────────────────

def all_messages(sess: dict) -> list[dict]:
    """全部消息，按 id 升序。DeepSeek 保证 parent_id < id，所以父消息一定先于子消息。"""
    return sorted(sess.get("messages") or [], key=lambda m: m["id"])


def active_messages(sess: dict) -> list[dict]:
    """只走 current_message_id 指出的活跃分支（markdown 档案用）。"""
    by_id = {m["id"]: m for m in sess.get("messages") or []}
    cur = sess.get("current_message_id")
    if cur not in by_id:
        cur = max(by_id) if by_id else None
    path, seen = [], set()
    while cur in by_id and cur not in seen:
        seen.add(cur)
        path.append(by_id[cur])
        cur = by_id[cur].get("parent_id")
    return list(reversed(path))


def user_text(msg: dict) -> str:
    parts = []
    for f in msg.get("fragments") or []:
        if f.get("type") == "REQUEST":
            text = (f.get("content") or "").strip()
            if text:
                parts.append(text)
        elif f.get("type") == "FILE":
            for x in f.get("files") or []:
                note = f'[附件] {x.get("file_name")}（{fmt_size(x.get("file_size"))}，导出未含内容）'
                parts.append(note)
    return "\n\n".join(parts)


def assistant_blocks(msg: dict) -> list[dict]:
    blocks = []
    for f in msg.get("fragments") or []:
        kind = f.get("type")
        text = (f.get("content") or "").strip()
        if not text:
            continue
        if kind == "THINK":
            blocks.append({"type": "thinking", "thinking": text})
        elif kind in ("RESPONSE", "TEMPLATE_RESPONSE"):
            blocks.append({"type": "text", "text": text})
    return blocks


def sources_of(msg: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """返回（搜索词，去重后的来源）。"""
    queries, links, seen = [], [], set()
    for f in msg.get("fragments") or []:
        if f.get("type") == "TOOL_SEARCH":
            queries += [q for q in f.get("queries") or [] if q]
            for r in f.get("results") or []:
                url = r.get("url")
                if url and url not in seen:
                    seen.add(url)
                    links.append((r.get("title") or url, url))
        elif f.get("type") == "TOOL_OPEN":
            url = f.get("url")
            if url and url not in seen:
                seen.add(url)
                links.append((f.get("title") or url, url))
        elif f.get("type") == "READ_LINK":
            for r in f.get("results") or []:
                url = r.get("url")
                if url and url not in seen:
                    seen.add(url)
                    links.append((r.get("title") or url, url))
    return queries, links


# ── 输出：Pi 会话 JSONL ─────────────────────────────────────────────

def write_jsonl(sess: dict, messages: list[dict], target: str) -> None:
    """按 DeepSeek 消息树写 Pi 会话树；session_info 挂在活跃叶子上，保证打开时停在当前分支。"""
    created = parse_ts(sess["inserted_at"])
    header = {
        "type": "session", "version": 3, "id": str(uuid.uuid4()),
        "timestamp": iso_ms(created), "cwd": CHAT_CWD,
    }
    lines = [json.dumps(header, ensure_ascii=False)]
    by_id = {m["id"]: m for m in messages}
    eid_by_msg: dict[int, str] = {}
    last_written = None

    def parent_eid(pid):
        # 被跳过的空消息（只有搜索/风险提示）向上找到最近一条写出的祖先
        while pid is not None and pid not in eid_by_msg:
            pid = (by_id.get(pid) or {}).get("parent_id")
        return eid_by_msg.get(pid)

    for msg in messages:
        ts = parse_ts(msg["inserted_at"])
        if msg["role"] == "USER":
            text = user_text(msg)
            if not text:
                continue
            message = {"role": "user", "content": text, "timestamp": ms(ts)}
        else:
            blocks = assistant_blocks(msg)
            if not blocks:
                continue
            message = {
                "role": "assistant", "content": blocks,
                "api": API, "provider": PROVIDER, "model": MODEL,
                "usage": zero_usage(), "stopReason": "stop", "timestamp": ms(ts),
            }
        eid = make_eid()
        lines.append(json.dumps({
            "type": "message", "id": eid, "parentId": parent_eid(msg["parent_id"]),
            "timestamp": iso_ms(ts), "message": message,
        }, ensure_ascii=False))
        eid_by_msg[msg["id"]] = eid
        last_written = eid

    leaf = sess.get("current_message_id")
    while leaf is not None and leaf not in eid_by_msg:
        leaf = (by_id.get(leaf) or {}).get("parent_id")
    lines.append(json.dumps({
        "type": "session_info", "id": make_eid(), "parentId": eid_by_msg.get(leaf) or last_written,
        "timestamp": iso_ms(parse_ts(sess["updated_at"])),
        "name": f"[DS] {sess.get('title') or '(无标题)'}",
    }, ensure_ascii=False))

    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── 输出：markdown 档案 ─────────────────────────────────────────────

def md_link_text(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def write_md(sess: dict, path: list[dict], target: str) -> None:
    title = sess.get("title") or "(无标题)"
    first, last = parse_ts(sess["inserted_at"]), parse_ts(sess["updated_at"])
    lines = [
        f"# {title}", "",
        f"- DeepSeek 会话: `{sess['id']}`",
        f"- 时间: {iso_ms(first)} → {iso_ms(last)}",
        f"- 消息: {len(path)}", "",
    ]
    for msg in path:
        if msg["role"] == "USER":
            text = user_text(msg)
            if text:
                lines += ["## 用户", "", text, ""]
            continue

        blocks = assistant_blocks(msg)
        queries, links = sources_of(msg)
        if not blocks and not links:
            continue
        lines += ["## DeepSeek", ""]
        if queries:
            lines += ["> 搜索：" + "；".join(queries), ""]
        for b in blocks:
            if b["type"] == "thinking":
                lines += ["<details><summary>思考</summary>", "", b["thinking"], "", "</details>", ""]
            else:
                lines += [b["text"], ""]
        if links:
            lines += ["**来源**", ""]
            lines += [f"- [{md_link_text(t)}]({u})" for t, u in links]
            lines += [""]

    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def write_index(state: dict) -> None:
    rows = sorted(state["migrated"].values(), key=lambda r: r["date"], reverse=True)
    lines = ["# DeepSeek 档案索引", "", f"共 {len(rows)} 个会话，按时间倒序。", ""]
    for r in rows:
        first = re.sub(r"\s+", " ", r.get("first") or "")[:60]
        lines.append(
            f"- {r['date'][:10]} · [{md_link_text(r['title'])}]({os.path.basename(r['md'])}) · {first}"
        )
    with open(os.path.join(ARCHIVE_DIR, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── main ─────────────────────────────────────────────────────────────

def pick_zip(explicit: str | None) -> str:
    if explicit:
        return explicit
    candidates = []
    for directory in DEFAULT_ZIP_DIRS:
        candidates += glob.glob(os.path.join(os.path.expanduser(directory), "deepseek_data-*.zip"))
    if not candidates:
        raise SystemExit("没找到 deepseek_data-*.zip，请用 --zip 指定")
    return max(candidates, key=os.path.getmtime)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", help="导出包路径（默认在 ~/Downloads 和 /mnt/c/Users/*/Downloads 里找最新的 deepseek_data-*.zip）")
    ap.add_argument("--limit", type=int, help="只导最近 N 个未导入的会话")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()

    zip_path = pick_zip(args.zip)
    state = load_state()
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    os.makedirs(SESSION_DIR, exist_ok=True)

    if state.get("version") != STATE_VERSION:
        removed = 0
        for rec in state.get("migrated", {}).values():
            for key in ("jsonl", "md"):
                old = rec.get(key)
                if old and os.path.isfile(old):
                    os.remove(old)
                    removed += 1
        print(f"状态版本 {state.get('version')} → {STATE_VERSION}：清掉旧产物 {removed} 个，重新导入")
        state = {"version": STATE_VERSION, "migrated": {}}
        save_state(state)

    with zipfile.ZipFile(zip_path) as zf:
        index = json.loads(zf.read("chat_session_index.json"))
        index.sort(key=lambda x: x["inserted_at"])
        pending = [x for x in index if x["id"] not in state["migrated"]]
        if args.limit:
            pending = pending[-args.limit:]

    print(f"导出包: {zip_path}")
    print(f"会话: {len(index)} 个，已导入 {len(state['migrated'])}，本次处理 {len(pending)}")
    if args.dry_run:
        for x in pending[:5]:
            print(f"  {x['inserted_at'][:10]}  {x['title']}")
        return

    done = skipped = 0
    with zipfile.ZipFile(zip_path) as zf:
        for i, item in enumerate(pending, 1):
            sid = item["id"]
            raw = zf.read(f"chat_sessions/{sid}.json")
            sess = json.loads(raw)
            messages, path = all_messages(sess), active_messages(sess)
            if not any(m["role"] == "USER" and user_text(m) for m in messages):
                skipped += 1
                continue

            created = parse_ts(sess["inserted_at"])
            jsonl = os.path.join(
                SESSION_DIR,
                f"{iso_ms(created).replace(':', '-').replace('.', '-')}_{uuid.uuid4()}.jsonl",
            )
            md = os.path.join(ARCHIVE_DIR, f"{created:%Y-%m-%d}-{slugify(sess.get('title') or '', sid)}.md")
            write_jsonl(sess, messages, jsonl)
            write_md(sess, path, md)

            first_user = next(
                user_text(m) for m in (path or messages) if m["role"] == "USER" and user_text(m)
            )
            state["migrated"][sid] = {
                "title": sess.get("title") or "(无标题)",
                "date": iso_ms(created),
                "first": first_user,
                "jsonl": jsonl,
                "md": md,
            }
            save_state(state)
            done += 1
            if i % 50 == 0:
                print(f"  ... {i}/{len(pending)}")

    write_index(state)
    print(f"完成: 写入 {done} 个会话，跳过空会话 {skipped} 个")
    print(f"  Pi   : {SESSION_DIR}")
    print(f"  档案 : {ARCHIVE_DIR}（INDEX.md 为索引）")


if __name__ == "__main__":
    main()
