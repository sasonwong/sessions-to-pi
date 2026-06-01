#!/usr/bin/env python3
"""Migrate OpenCode conversations to Pi session format.

Design (following Pi's subagent extension pattern):
  - Subagent conversations embedded INLINE in the parent session
    (Pi's extension uses --no-session → no separate files)
  - Uses `custom_message` entries (Pi native, no extension needed)
    → Visible in /tree with distinct styling
    → Participates in LLM context
  - Subagent content interleaved chronologically with parent messages
  - No separate JSONL files for subagent children

Usage:
  python3 migrate.py --dry-run --limit 5
  python3 migrate.py --all
  python3 migrate.py --dir /home/sason/life-project
"""

import sqlite3
import json
import uuid
import os
import sys
from datetime import datetime, timezone

OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
PI_SESSION_DIR = os.path.expanduser("~/.pi/agent/sessions")
STATE_FILE = os.path.join(PI_SESSION_DIR, ".migration-state.json")
DEFAULT_THINKING_LEVEL = "off"


# ── Helpers ──────────────────────────────────────────────────────────

def to_iso(ms):
    return f"{datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.')}{ms % 1000:03d}Z"


def make_eid():
    return uuid.uuid4().hex[:8]


def session_dir_for_cwd(cwd):
    safe = cwd.rstrip("/").lstrip("/").replace("/", "-")
    return f"--{safe}--"


def pi_session_path(cwd, t_created, sess_uuid):
    d = os.path.join(PI_SESSION_DIR, session_dir_for_cwd(cwd))
    ts = to_iso(t_created).replace(":", "-").replace(".", "-")
    return os.path.join(d, f"{ts}_{sess_uuid}.jsonl")


def make_usage(tokens, cost_val):
    if not tokens:
        return None
    cache = tokens.get("cache", {}) if isinstance(tokens.get("cache"), dict) else {}
    c = cost_val if isinstance(cost_val, (int, float)) else 0
    return {
        "input": tokens.get("input", 0), "output": tokens.get("output", 0),
        "cacheRead": cache.get("read", 0), "cacheWrite": cache.get("write", 0),
        "totalTokens": tokens.get("total", 0),
        "cost": {"input": c, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": c},
    }


# ── Fetch child session data ───────────────────────────────────────-

def fetch_child_messages(cursor, parent_id):
    """Fetch all subagent child messages for a parent session.
    
    Returns list of (timestamp, agent_key, agent_display, title, role, text)
    sorted by timestamp. Each item represents one child message.
    """
    cursor.execute(
        "SELECT id, title, agent FROM session WHERE parent_id = ? ORDER BY time_created",
        (parent_id,)
    )
    children = cursor.fetchall()

    if not children:
        return []

    seen = {}
    items = []

    for cid, ctitle, cagent in children:
        agent_raw = (cagent or "subagent").strip().lower() or "subagent"
        if agent_raw in seen:
            seen[agent_raw] += 1
            key = f"{agent_raw}_{seen[agent_raw]}"
        else:
            seen[agent_raw] = 1
            key = agent_raw

        agent_display = cagent or "subagent"
        title = ctitle or "(untitled)"

        cursor.execute(
            "SELECT time_created, data FROM message WHERE session_id = ? ORDER BY time_created, id",
            (cid,)
        )
        for ts, djson in cursor:
            try:
                data = json.loads(djson)
            except:
                continue
            role = data.get("role")
            if role not in ("user", "assistant"):
                continue
            # Fetch message id, then text parts
            c2 = cursor.connection.cursor()
            c2.execute(
                "SELECT id FROM message WHERE session_id = ? AND time_created = ? AND data = ?",
                (cid, ts, djson)
            )
            row2 = c2.fetchone()
            if not row2:
                continue
            mid = row2[0]
            c2.execute(
                "SELECT data FROM part WHERE message_id = ? AND json_extract(data, '$.type') = 'text' "
                "ORDER BY time_created, id",
                (mid,)
            )
            texts = []
            for (pj,) in c2:
                try:
                    prt = json.loads(pj)
                    texts.append(prt.get("text", ""))
                except:
                    pass
            text = "\n".join(texts).strip()
            if text:
                items.append((ts, key, agent_display, title, role, text))

    items.sort(key=lambda x: x[0])
    return items


# ── Migration state (incremental) ──────────────────────────────────

STATE_VERSION = 1


def load_state():
    """Load migration state JSON. Returns dict or empty state."""
    if not os.path.exists(STATE_FILE):
        return {"version": STATE_VERSION, "migrated": {}}
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        if not isinstance(st.get("migrated"), dict):
            st["migrated"] = {}
        return st
    except (json.JSONDecodeError, OSError):
        return {"version": STATE_VERSION, "migrated": {}}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)


def show_status(st, conn):
    """Print migration status summary."""
    migrated = st.get("migrated", {})
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM session")
    (total_oc,) = cursor.fetchone()
    total_pi = len(migrated)
    # Only count entries whose files still exist
    files_exist = sum(1 for s in migrated.values()
                      if os.path.exists(os.path.join(PI_SESSION_DIR, s.get("pi_file", ""))))
    remaining = total_oc - files_exist
    n_custom = sum(s.get("n_custom", 0) for s in migrated.values())
    n_msgs = sum(s.get("n_messages", 0) for s in migrated.values())
    total_size = 0
    for s in migrated.values():
        p = os.path.join(PI_SESSION_DIR, s.get("pi_file", ""))
        if os.path.exists(p):
            total_size += os.path.getsize(p)

    print()
    print(f"OpenCode DB  : {total_oc} sessions")
    print(f"Migrated     : {files_exist} sessions ({n_msgs} msgs, {n_custom} subagent inlines)")
    print(f"  (state has {total_pi} entries, {total_pi - files_exist} files missing)")
    print(f"Remaining    : {remaining} sessions")
    print(f"Pi files size: {total_size / 1024 / 1024:.1f} MB")

    # Orphaned Pi files (state references a file, but OpenCode session gone)
    orphaned = 0
    for ocid, s in migrated.items():
        p = os.path.join(PI_SESSION_DIR, s.get("pi_file", ""))
        if not os.path.exists(p):
            orphaned += 1
    if orphaned:
        print(f"Orphaned refs: {orphaned} (state file references deleted Pi files)")

    if remaining > 0:
        cursor.execute("SELECT id, title, time_created FROM session ORDER BY time_created DESC")
        all_sessions = cursor.fetchall()
        newest_not = None
        for srow in all_sessions:
            ocid = str(srow["id"])
            if ocid not in migrated:
                newest_not = srow["title"] or "(untitled)"
                break
        if newest_not:
            print(f'Newest unconverted: "{newest_not}"')


# ── Build Pi entries from OpenCode message ──────────────────────────

def msg_to_entries(msg_data, parts, ts, cur_provider, cur_model, last_eid):
    """Convert one OpenCode message to Pi entries.
    
    Returns (entries_list, new_last_eid, new_provider, new_model).
    """
    entries = []
    role = msg_data.get("role")

    # Model change
    new_provider = msg_data.get("providerID") or cur_provider
    new_model = msg_data.get("modelID") or cur_model
    if new_model != cur_model:
        eid = make_eid()
        entries.append({"type": "model_change", "id": eid, "parentId": last_eid,
                        "timestamp": to_iso(ts), "provider": new_provider, "modelId": new_model})
        last_eid = eid
        cur_model, cur_provider = new_model, new_provider

    if role == "user":
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        content = "\n".join(texts).strip()
        if content:
            eid = make_eid()
            entries.append({"type": "message", "id": eid, "parentId": last_eid,
                            "timestamp": to_iso(ts), "message": {
                                "role": "user", "content": [{"type": "text", "text": content}],
                                "timestamp": ts}})
            last_eid = eid

    elif role == "assistant":
        reasoning = [p for p in parts if p.get("type") == "reasoning"]
        texts = [p for p in parts if p.get("type") == "text"]
        tools = [p for p in parts if p.get("type") == "tool"]

        content = []
        for r in reasoning:
            content.append({"type": "thinking", "thinking": r.get("text", "")})
        for t in texts:
            content.append({"type": "text", "text": t.get("text", "")})
        call_ids = []
        for tl in tools:
            cid = tl.get("callID", f"call_{make_eid()}")
            call_ids.append(cid)
            inp = tl.get("state", {}).get("input", {})
            content.append({
                "type": "toolCall", "id": cid,
                "name": tl.get("tool", "unknown"),
                "arguments": inp if isinstance(inp, dict) else {"value": str(inp)},
            })

        if content:
            eid = make_eid()
            usage = make_usage(msg_data.get("tokens"), msg_data.get("cost"))
            stop_reason = "toolUse" if tools else "stop"
            entry = {"type": "message", "id": eid, "parentId": last_eid,
                     "timestamp": to_iso(ts), "message": {
                         "role": "assistant", "content": content,
                         "stopReason": stop_reason, "timestamp": ts,
                         "api": "openai-completions",
                         "provider": cur_provider, "model": cur_model}}
            if usage:
                entry["message"]["usage"] = usage
            if msg_data.get("responseId"):
                entry["message"]["responseId"] = msg_data["responseId"]
            if msg_data.get("responseModel"):
                entry["message"]["responseModel"] = msg_data["responseModel"]
            entries.append(entry)
            last_eid = eid

        # Tool results
        for tl in tools:
            teid = make_eid()
            entries.append({"type": "message", "id": teid, "parentId": last_eid,
                            "timestamp": to_iso(ts + 1), "message": {
                                "role": "toolResult",
                                "toolCallId": tl.get("callID", "call_unknown"),
                                "toolName": tl.get("tool", "unknown"),
                                "content": [{"type": "text", "text": str(tl.get("state", {}).get("output", "") or "")}],
                                "isError": tl.get("state", {}).get("status") == "error",
                                "timestamp": ts + 1}})
            last_eid = teid

    return entries, last_eid, cur_provider, cur_model


# ── Convert one session ─────────────────────────────────────────────

def convert_session(cursor, session_row):
    """Convert OpenCode session → Pi JSONL lines + target path."""
    sid = session_row["id"]
    directory = session_row["directory"] or ""
    path_field = session_row["path"] or ""
    title = session_row["title"] or ""
    t_created = session_row["time_created"]
    try:
        model = json.loads(session_row["model"] or "{}")
    except:
        model = {}
    model_id = model.get("id", "unknown")
    provider_id = model.get("providerID", "unknown")
    cwd = directory or path_field or os.path.expanduser("~")
    sess_uuid = str(uuid.uuid4())
    target_path = pi_session_path(cwd, t_created, sess_uuid)

    header = {"type": "session", "version": 3, "id": sess_uuid,
              "timestamp": to_iso(t_created), "cwd": cwd}
    lines = [json.dumps(header)]

    # Build initial entries (session_info, model_change, thinking_level_change)
    led = None
    head_entries = []
    if title:
        e = make_eid()
        head_entries.append({"type": "session_info", "id": e, "parentId": None,
                             "timestamp": to_iso(t_created), "name": f"[OC] {title}"})
        led = e
    e = make_eid()
    head_entries.append({"type": "model_change", "id": e, "parentId": led,
                         "timestamp": to_iso(t_created), "provider": provider_id, "modelId": model_id})
    led = e
    e = make_eid()
    head_entries.append({"type": "thinking_level_change", "id": e, "parentId": led,
                         "timestamp": to_iso(t_created), "thinkingLevel": DEFAULT_THINKING_LEVEL})
    led = e

    for ent in head_entries:
        lines.append(json.dumps(ent))

    # ── Fetch parent messages ────────────────────────────────────
    cursor.execute(
        "SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created, id",
        (sid,)
    )
    parent_msgs = []
    for mid, ts, djson in cursor:
        try:
            data = json.loads(djson)
        except:
            continue
        if data.get("role") not in ("user", "assistant"):
            continue
        c2 = cursor.connection.cursor()
        c2.execute("SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id", (mid,))
        parts = []
        for (pj,) in c2:
            try:
                parts.append(json.loads(pj))
            except:
                pass
        parent_msgs.append((mid, ts, data, parts))

    # ── Fetch child messages ─────────────────────────────────────
    child_msgs = fetch_child_messages(cursor, sid)

    # ── Merge: interleave child messages between parent messages ──
    cur_provider = provider_id
    cur_model = model_id

    # We iterate through parent messages. After each parent message,
    # we flush any child messages whose timestamp falls between this
    # parent message and the next one.
    for i, (mid, ts, data, parts) in enumerate(parent_msgs):
        # Convert parent message
        conv, led, cur_provider, cur_model = msg_to_entries(
            data, parts, ts, cur_provider, cur_model, led)
        for ent in conv:
            lines.append(json.dumps(ent))

        # Determine next parent timestamp (or INF)
        next_ts = parent_msgs[i + 1][1] if i + 1 < len(parent_msgs) else float("inf")

        # Flush child messages that fit between this parent msg and next
        remaining = []
        for child in child_msgs:
            c_ts = child[0]
            if c_ts >= ts and c_ts < next_ts:
                # Insert as custom_message
                c_key, c_agent, c_title, c_role, c_text = child[1], child[2], child[3], child[4], child[5]
                role_label = "User" if c_role == "user" else "Assistant"
                display_text = (
                    f"[Subagent: {c_agent}] Task: {c_title}\n"
                    f"--- Begin subagent conversation ---\n"
                    f"{role_label}: {c_text}\n"
                    f"--- End subagent conversation ---"
                )
                ceid = make_eid()
                lines.append(json.dumps({
                    "type": "custom_message",
                    "id": ceid, "parentId": led,
                    "timestamp": to_iso(c_ts),
                    "customType": f"subagent/{c_key}",
                    "content": display_text,
                    "display": True,
                }))
                led = ceid
            else:
                remaining.append(child)
        child_msgs = remaining

    return lines, target_path


# ── Main ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Migrate OpenCode conversations to Pi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--all", action="store_true", help="Migrate all sessions (overrides --limit)")
    parser.add_argument("--incremental", action="store_true",
                        help="Skip sessions already recorded in migration state")
    parser.add_argument("--dry-run", action="store_true", help="Stats only")
    parser.add_argument("--status", action="store_true", help="Show migration status and exit")
    parser.add_argument("--dir", type=str, help="Only sessions from this dir prefix")
    parser.add_argument("--limit", type=int, default=3, help="Max sessions")
    parser.add_argument("--start", type=int, default=0, help="Skip N")
    args = parser.parse_args()

    if not os.path.exists(OPENCODE_DB):
        print(f"Error: DB not found at {OPENCODE_DB}")
        sys.exit(1)

    conn = sqlite3.connect(OPENCODE_DB)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if args.status:
        show_status(load_state(), conn)
        conn.close()
        return

    cursor.execute("SELECT * FROM session ORDER BY time_created DESC")
    all_sessions = cursor.fetchall()
    print(f"Found {len(all_sessions)} sessions")

    cursor.execute("SELECT parent_id, COUNT(*) FROM session WHERE parent_id IS NOT NULL GROUP BY parent_id")
    pc = {r["parent_id"]: r["COUNT(*)"] for r in cursor.fetchall()}
    print(f"  {len(pc)} parents, {sum(pc.values())} children")

    if args.dir:
        all_sessions = [s for s in all_sessions if s["directory"] and s["directory"].startswith(args.dir)]
        print(f"  Filtered by dir: {len(all_sessions)}")

    # Incremental: skip already-migrated sessions (before limit, so --limit applies to remaining)
    state = load_state()
    if args.incremental:
        migrated_ids = set(state.get("migrated", {}).keys())
        before = len(all_sessions)
        all_sessions = [s for s in all_sessions if str(s["id"]) not in migrated_ids]
        skipped = before - len(all_sessions)
        if skipped:
            print(f"  Skipped (already migrated): {skipped}")
    else:
        state = None  # don't update state in non-incremental mode

    if args.start:
        all_sessions = all_sessions[args.start:]
    if args.all:
        pass  # no limit
    elif args.limit:
        all_sessions = all_sessions[:args.limit]

    # Top-level only (children inlined)
    ids = {s["id"] for s in all_sessions}
    top = [s for s in all_sessions if not s["parent_id"]]
    orphans = [s for s in all_sessions if s["parent_id"] and s["parent_id"] not in ids]
    to_process = top + orphans
    skipped = len(all_sessions) - len(to_process)
    print(f"  To convert: {len(to_process)} ({skipped} inlined)")

    total_conv = total_err = 0
    for idx, row in enumerate(to_process, 1):
        title = (row["title"] or "(untitled)")[:60]
        has_kids = row["id"] in pc
        tag = f"({pc[row['id']]} children)" if has_kids else ""
        print(f"[{idx}/{len(to_process)}] {title} {tag}...", end="", flush=True)
        try:
            lines, target_path = convert_session(cursor, row)
            if args.dry_run:
                n_msg = sum(1 for l in lines[1:] if json.loads(l).get("type") == "message")
                n_cst = sum(1 for l in lines[1:] if json.loads(l).get("type") == "custom_message")
                print(f" ✓ ({n_msg}+{n_cst})")
            else:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "w") as f:
                    f.write("\n".join(lines) + "\n")
                # Record in state (relative path for portability)
                if state is not None:
                    rel = os.path.relpath(target_path, PI_SESSION_DIR)
                    n_msg = sum(1 for l in lines[1:] if json.loads(l).get("type") == "message")
                    n_cst = sum(1 for l in lines[1:] if json.loads(l).get("type") == "custom_message")
                    state.setdefault("migrated", {})[str(row["id"])] = {
                        "title": row["title"] or "",
                        "time_created": row["time_created"],
                        "time_migrated": int(datetime.now().timestamp() * 1000),
                        "pi_file": rel,
                        "n_messages": n_msg,
                        "n_custom": n_cst,
                    }
                    save_state(state)
                print(f" ✓")
            total_conv += 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f" ✗ {e}")
            total_err += 1

    conn.close()
    if state and not args.dry_run:
        summary = state.get("migrated", {})
        print(f"\nMigration state: {len(summary)} sessions recorded")
    print(f"\nDone: {total_conv} ok, {total_err} err")


if __name__ == "__main__":
    main()
