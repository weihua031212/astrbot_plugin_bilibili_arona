"""本地档案：看过哪些视频、发过什么、还有哪些草稿等你点头。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"


def _ensure() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load(name: str, default: Any) -> Any:
    _ensure()
    p = DATA_DIR / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(name: str, obj: Any) -> None:
    _ensure()
    p = DATA_DIR / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------- 待办 ----------


def add_task(task: dict[str, Any]) -> None:
    """把要我亲自看的一条视频排进待办。"""
    tasks = _load("pending.json", [])
    if not isinstance(tasks, list):
        tasks = []
    key = task.get("at_id") or 0
    if key:
        tasks = [t for t in tasks if t.get("at_id") != key]
    else:
        tasks = [t for t in tasks if t.get("bvid") != task.get("bvid")]
    tasks.append(task)
    _save("pending.json", tasks[-20:])


def list_tasks() -> list[dict[str, Any]]:
    tasks = _load("pending.json", [])
    return tasks if isinstance(tasks, list) else []


def drop_task(bvid: str) -> None:
    tasks = [t for t in list_tasks() if t.get("bvid") != bvid]
    _save("pending.json", tasks)


# ---------- 会话锚点 ----------


def set_last_umo(umo: str) -> None:
    """记住最后一次和我说话的会话，后台召唤时靠它找回人格和聊天记录。"""
    if umo:
        _save("last_umo.json", {"umo": umo, "ts": int(time.time())})


def get_last_umo() -> str:
    return str((_load("last_umo.json", {}) or {}).get("umo") or "")


# ---------- 档案 ----------


def add_audit(action: str, **fields: Any) -> None:
    """追加一行审计记录，问起来随时能倒出来。"""
    _ensure()
    row = {"ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action}
    row.update(fields)
    with (DATA_DIR / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_audit(limit: int = 20) -> list[dict[str, Any]]:
    p = DATA_DIR / "audit.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-limit:]


# ---------- 看过的视频 ----------


def remember_video(v: dict[str, Any], my_rpid: int | None = None) -> None:
    db = _load("videos.json", {})
    key = v["bvid"]
    old = db.get(key, {})
    db[key] = {
        "bvid": key,
        "aid": v["aid"],
        "title": v["title"],
        "owner": v["owner_name"],
        "cover": v.get("cover") or old.get("cover") or "",
        "duration": v["duration"],
        "watched_at": old.get("watched_at") or int(time.time()),
        "my_rpid": my_rpid or old.get("my_rpid"),
        "summary": v.get("summary") or old.get("summary") or "",
        "saw_frames": v.get("saw_frames", old.get("saw_frames", 0)),
        "saw_subtitle": v.get("saw_subtitle", old.get("saw_subtitle", False)),
        "saw_danmaku": v.get("saw_danmaku", old.get("saw_danmaku", 0)),
    }
    _save("videos.json", db)


def watched_videos() -> list[dict[str, Any]]:
    return sorted(_load("videos.json", {}).values(), key=lambda x: x.get("watched_at", 0), reverse=True)


# ---------- 待确认草稿 ----------


def add_draft(
    video: dict[str, Any], comment: dict[str, Any], text: str, inbox_key: str = ""
) -> int:
    drafts = _load("drafts.json", [])
    nid = (max([d["id"] for d in drafts], default=0)) + 1
    drafts.append(
        {
            "id": nid,
            "created": int(time.time()),
            "bvid": video["bvid"],
            "title": video["title"],
            "root": comment.get("root"),
            "rpid": comment.get("rpid"),
            "uname": comment.get("uname"),
            "mid": comment.get("mid"),
            "their_text": comment.get("message"),
            "reply_text": text,
            "inbox_key": inbox_key or "",
            "status": "pending",
        }
    )
    _save("drafts.json", drafts)
    return nid


def list_drafts(status: str = "pending") -> list[dict[str, Any]]:
    drafts = _load("drafts.json", [])
    return [d for d in drafts if d.get("status") == status]


def get_draft(draft_id: int) -> dict[str, Any] | None:
    for d in _load("drafts.json", []):
        if d["id"] == draft_id:
            return d
    return None


def set_draft_status(draft_id: int, status: str, extra: dict[str, Any] | None = None) -> None:
    drafts = _load("drafts.json", [])
    for d in drafts:
        if d["id"] == draft_id:
            d["status"] = status
            d["handled"] = int(time.time())
            if extra:
                d.update(extra)
    _save("drafts.json", drafts)


def update_draft(draft_id: int, **fields: Any) -> dict[str, Any] | None:
    """改草稿里的字段，比如回复正文或 @ 对象。"""
    drafts = _load("drafts.json", [])
    hit = None
    for d in drafts:
        if d["id"] == draft_id:
            d.update(fields)
            hit = d
    _save("drafts.json", drafts)
    return hit


def all_drafts() -> list[dict[str, Any]]:
    return _load("drafts.json", [])


# ---------- @ 消息去重 ----------


def seen_at(at_id: int) -> bool:
    if not at_id:
        return False
    return at_id in set(_load("seen_at.json", []))


def mark_at(at_id: int) -> None:
    if not at_id:
        return
    seen = _load("seen_at.json", [])
    if at_id not in seen:
        seen.append(at_id)
        _save("seen_at.json", seen[-2000:])


def seen_reply(rpid: int) -> bool:
    seen = set(_load("seen_replies.json", []))
    return rpid in seen


def mark_seen(rpid: int) -> None:
    seen = _load("seen_replies.json", [])
    if rpid not in seen:
        seen.append(rpid)
        _save("seen_replies.json", seen[-5000:])
