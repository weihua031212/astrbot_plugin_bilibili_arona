"""面板要看的几本账：养号目录、小日记、留言箱来往、召唤记录。

都是纯本地文件，写失败不影响主流程。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "data"

DIARY = "diary.jsonl"
SUMMON = "summons.jsonl"
INBOX_REPLY = "inbox_replies.jsonl"


def _ensure() -> None:
    DATA.mkdir(parents=True, exist_ok=True)


def _read(name: str) -> list[dict[str, Any]]:
    _ensure()
    p = DATA / name
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except Exception:
            continue
    return out


def _write(name: str, rows: list[dict[str, Any]]) -> None:
    _ensure()
    p = DATA / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    tmp.replace(p)


def _append(name: str, row: dict[str, Any]) -> None:
    _ensure()
    try:
        with (DATA / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def today() -> str:
    return time.strftime("%Y-%m-%d")


def cover_url(pic: str) -> str:
    """B 站封面有时是 //i0.hdslb.com 开头，补成完整地址。"""
    p = str(pic or "").strip()
    if not p:
        return ""
    if p.startswith("//"):
        return "https:" + p
    if p.startswith("http"):
        return p
    return "https://" + p


def video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}" if bvid else ""


# ---------------- 小日记 ----------------


def diary_upsert(day: str, text: str, posted: bool = False, **extra: Any) -> dict[str, Any]:
    """同一天只留一条，再写就覆盖正文。"""
    rows = [r for r in _read(DIARY) if r.get("day") != day]
    row = {
        "day": day or today(),
        "text": text,
        "posted": bool(posted),
        "at": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    row.update(extra)
    rows.append(row)
    rows.sort(key=lambda r: r.get("day", ""))
    _write(DIARY, rows[-400:])
    return row


def diary_list(limit: int = 60) -> list[dict[str, Any]]:
    rows = _read(DIARY)
    rows.sort(key=lambda r: r.get("day", ""), reverse=True)
    return rows[:limit]


def diary_get(day: str) -> dict[str, Any] | None:
    for r in _read(DIARY):
        if r.get("day") == day:
            return r
    return None


def diary_set_posted(day: str, posted: bool = True, dyn_id: str = "") -> bool:
    rows = _read(DIARY)
    hit = False
    for r in rows:
        if r.get("day") == day:
            r["posted"] = bool(posted)
            if dyn_id:
                r["dyn_id"] = dyn_id
            r["posted_at"] = int(time.time())
            hit = True
    if hit:
        _write(DIARY, rows)
    return hit


# ---------------- 召唤记录 ----------------


def summon_add(
    bvid: str,
    title: str,
    pic: str = "",
    uname: str = "",
    said: str = "",
    at_id: int = 0,
    mid: int = 0,
) -> dict[str, Any]:
    """记一条「账号主人召唤我看片」。同一次召唤只留一条。"""
    rows = _read(SUMMON)
    key = f"at:{at_id}" if at_id else f"bv:{bvid}:{int(time.time()) // 60}"
    if any(r.get("key") == key for r in rows):
        return next(r for r in rows if r.get("key") == key)
    row = {
        "key": key,
        "at_id": at_id,
        "bvid": bvid,
        "title": title,
        "pic": cover_url(pic),
        "url": video_url(bvid),
        "uname": uname,
        "mid": mid,
        "said": said,
        "my_text": "",
        "my_rpid": 0,
        "at": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    rows.append(row)
    _write(SUMMON, rows[-400:])
    return row


def summon_comment(bvid: str, text: str, rpid: int = 0) -> bool:
    """我评论发出去了，把它补到最近一条同视频的召唤记录上。"""
    rows = _read(SUMMON)
    for r in reversed(rows):
        if r.get("bvid") == bvid:
            r["my_text"] = text
            r["my_rpid"] = int(rpid or 0)
            r["replied_at"] = int(time.time())
            _write(SUMMON, rows)
            return True
    return False


def summon_list(limit: int = 100) -> list[dict[str, Any]]:
    rows = _read(SUMMON)
    rows.sort(key=lambda r: r.get("at", 0), reverse=True)
    return rows[:limit]


# ---------------- 留言箱来往 ----------------


def inbox_reply_add(
    key: str,
    kind: str,
    uname: str,
    said: str,
    my_text: str = "",
    mid: int = 0,
    bvid: str = "",
    title: str = "",
    pic: str = "",
    at: int = 0,
    blocked: bool = False,
    reason: str = "",
    root: int = 0,
    oid: int = 0,
) -> dict[str, Any]:
    rows = _read(INBOX_REPLY)
    if any(r.get("key") == key for r in rows):
        return next(r for r in rows if r.get("key") == key)
    row = {
        "key": key,
        "kind": kind,
        "uname": uname or "（没留名字）",
        "mid": mid,
        "said": said,
        "my_text": my_text,
        "bvid": bvid,
        "title": title,
        "pic": cover_url(pic),
        "url": video_url(bvid),
        "at": int(at or time.time()),
        "handled": int(time.time()),
        "blocked": bool(blocked),
        "reason": reason,
    }
    rows.append(row)
    _write(INBOX_REPLY, rows[-600:])
    return row


def inbox_reply_list(limit: int = 100) -> list[dict[str, Any]]:
    rows = _read(INBOX_REPLY)
    rows.sort(key=lambda r: r.get("handled", 0), reverse=True)
    return rows[:limit]


def inbox_set_reply(key: str, text: str) -> bool:
    """补上我实际是怎么回那条消息的。"""
    rows = _read(INBOX_REPLY)
    hit = False
    for r in rows:
        if r.get("key") == key:
            r["my_text"] = text
            r["replied_at"] = int(time.time())
            hit = True
    if hit:
        _write(INBOX_REPLY, rows)
    return hit

# ---------------- 统一内容口味 ----------------

PROFILE_FILE = "profile.json"

# 分区类：B 站有对应榜单，能直接拉
TOPIC_PARTS = [
    "全站", "动画", "游戏", "知识", "科技", "生活", "美食", "动物",
    "鬼畜", "音乐", "舞蹈", "影视", "娱乐", "运动", "汽车", "时尚", "纪录",
]

# 题材类：没有单独榜单，靠标题关键词筛
TOPIC_TAGS = ["AI动画", "AI资讯", "搞笑", "播报", "资讯", "数码", "编程"]

ALL_TOPICS = TOPIC_PARTS + TOPIC_TAGS

DEFAULT_PROFILE: dict[str, Any] = {
    "topics": ["游戏", "科技"],
    "max_minutes": 3,
    "per_round": 1,
    "roam_max_minutes": 3.5,
    "rewrite": "像聊天那样写：第一人称，挑一两个真有感觉的细节说，可以吐槽可以夸，别复述剧情、别念标题",
}


def clean_topics(raw: Any, limit: int = 5) -> list[str]:
    """口味名单：官方分区、关键词题材都行，不在名单里的当自定义名字存下来。"""
    out: list[str] = []
    for t in (raw or []):
        name = str(t or "").strip()
        if not name or len(name) > 16:
            continue
        if name in out:
            continue
        out.append(name)
    return out[:limit] or ["全站"]


def load_profile() -> dict[str, Any]:
    """养号、召唤、自主游览共用的口味：只看哪几类、多长、一轮几条。"""
    _ensure()
    prof = dict(DEFAULT_PROFILE)
    p = DATA / PROFILE_FILE
    if not p.exists():
        # 从旧的两个文件里迁一次，别让账号主人白选
        legacy_parts = _read_json(DATA / "nurture_parts.json")
        legacy_plan = _read_json(DATA / "watch_plan.json")
        topics = []
        if isinstance(legacy_parts, dict):
            topics = [str(x) for x in (legacy_parts.get("parts") or [])]
        elif isinstance(legacy_parts, list):
            topics = [str(x) for x in legacy_parts]
        if isinstance(legacy_plan, dict):
            old = [str(x) for x in (legacy_plan.get("genres") or [])]
            for name in old:
                if name in ALL_TOPICS and name not in topics:
                    topics.append(name)
            if legacy_plan.get("max_minutes"):
                prof["max_minutes"] = int(legacy_plan["max_minutes"])
            if legacy_plan.get("per_round"):
                prof["per_round"] = int(legacy_plan["per_round"])
        topics = [t for t in topics if t in ALL_TOPICS]
        if topics:
            prof["topics"] = topics[:5]
        p.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    prof[k] = v
        except Exception:
            pass
    prof["topics"] = clean_topics(prof.get("topics"))
    prof["custom_topics"] = [str(x).strip() for x in (prof.get("custom_topics") or []) if str(x).strip()][:20]
    prof["all_topics"] = ALL_TOPICS
    return prof


def _read_json(path: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def save_profile(data: dict[str, Any]) -> dict[str, Any]:
    _ensure()
    prof = {
        "topics": clean_topics(data.get("topics")),
        "custom_topics": [str(x).strip() for x in (data.get("custom_topics") or []) if str(x).strip()][:20],
        "max_minutes": max(1, min(30, int(data.get("max_minutes") or 3))),
        "per_round": max(1, min(5, int(data.get("per_round") or 1))),
        "roam_max_minutes": max(1, min(30, float(data.get("roam_max_minutes") or 3.5))),
        "rewrite": str(data.get("rewrite") or DEFAULT_PROFILE["rewrite"])[:120],
    }
    (DATA / PROFILE_FILE).write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    out = dict(prof)
    out["all_topics"] = ALL_TOPICS
    return out


# ---------------- 审核记录 ----------------


def review_add(
    kind: str,
    uname: str,
    text: str,
    passed: bool,
    reason: str = "",
    bvid: str = "",
    title: str = "",
    mid: int = 0,
) -> None:
    """谁说了什么、我放没放行，两条都要留痕。"""
    _append(
        "review.jsonl",
        {
            "kind": kind,
            "uname": uname or "（没留名字）",
            "mid": int(mid or 0),
            "text": str(text or "")[:600],
            "passed": bool(passed),
            "reason": reason,
            "bvid": bvid,
            "title": title,
            "at": int(time.time()),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def review_list(limit: int = 80) -> list[dict[str, Any]]:
    rows = _read("review.jsonl")
    rows.sort(key=lambda r: r.get("at", 0), reverse=True)
    return rows[:limit]


# ---------------- 自主游览 ----------------


def roam_add(
    bvid: str,
    title: str,
    pic: str = "",
    owner: str = "",
    action: str = "viewed",
    said: str = "",
    talked_to: str = "",
    my_rpid: int = 0,
    noted: str = "",
    mood: str = "",
) -> dict[str, Any]:
    """自主游览的一次动作：看了、评了、跟谁说了、有没有叫账号主人。"""
    rows = _read("roam.jsonl")
    key = f"{bvid}:{action}:{int(time.time()) // 30}"
    row = {
        "key": key,
        "bvid": bvid,
        "title": title,
        "pic": cover_url(pic),
        "url": video_url(bvid),
        "owner": owner,
        "action": action,
        "said": said,
        "talked_to": talked_to,
        "my_rpid": int(my_rpid or 0),
        "noted": noted,
        "mood": mood,
        "at": int(time.time()),
        "time": time.strftime("%m-%d %H:%M"),
        "day": today(),
    }
    rows.append(row)
    _write("roam.jsonl", rows[-800:])
    return row


def roam_list(limit: int = 120, day: str = "") -> list[dict[str, Any]]:
    rows = _read("roam.jsonl")
    if day:
        rows = [r for r in rows if r.get("day") == day]
    rows.sort(key=lambda r: r.get("at", 0), reverse=True)
    return rows[:limit]


def roam_days(limit_days: int = 20) -> list[dict[str, Any]]:
    """按天分组，面板按天展示。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in _read("roam.jsonl"):
        groups.setdefault(str(r.get("day") or ""), []).append(r)
    out = []
    for day, items in groups.items():
        items.sort(key=lambda r: r.get("at", 0))
        seen, vids = set(), []
        for it in items:
            bv = it.get("bvid") or ""
            if bv not in seen:
                seen.add(bv)
                vids.append(
                    {
                        "bvid": bv,
                        "title": it.get("title") or "",
                        "pic": it.get("pic") or "",
                        "url": it.get("url") or "",
                        "owner": it.get("owner") or "",
                        "actions": [],
                    }
                )
            for v in vids:
                if v["bvid"] == bv:
                    v["actions"].append(
                        {
                            "action": it.get("action"),
                            "said": it.get("said") or "",
                            "talked_to": it.get("talked_to") or "",
                            "noted": it.get("noted") or "",
                            "time": it.get("time") or "",
                            "at": int(it.get("at") or 0),
                        }
                    )
        out.append({"day": day, "items": vids})
    out.sort(key=lambda d: d["day"], reverse=True)
    return out[:limit_days]
