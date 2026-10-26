"""收件箱：自己动态下的评论、私信。

只负责把话说收下来，不做判断。收下来之后直接叫我，由我自己决定怎么回。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).parent
INBOX = BASE / "data" / "inbox.json"
SEEN = BASE / "data" / "inbox_seen.json"


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def pending() -> list[dict[str, Any]]:
    data = _load(INBOX, [])
    return data if isinstance(data, list) else []


def mark_done(key: str) -> None:
    _save(INBOX, [i for i in pending() if i.get("key") != key])
    seen = _load(SEEN, [])
    if not isinstance(seen, list):
        seen = []
    seen.append(key)
    _save(SEEN, seen[-500:])


async def fetch_new(client: Any, vr: Any, owner_mid: int) -> list[dict[str, Any]]:
    """抓一遍动态评论和私信，返回这次新出现的条目。"""
    seen = _load(SEEN, [])
    seen = set(seen) if isinstance(seen, list) else set()
    inbox = pending()
    known = {i.get("key") for i in inbox}
    added: list[dict[str, Any]] = []

    me = await client.whoami()
    my_mid = int(me.get("mid") or 0)

    # ---------- 1 自己动态下的评论 ----------
    try:
        signed = await vr._sign(
            {
                "host_mid": str(my_mid),
                "offset": "",
                "timezone_offset": -480,
                "platform": "web",
                "features": "itemOpusStyle,opusBigPic",
            }
        )
        r = await client.client.get(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            params=signed,
            headers={"Referer": f"https://space.bilibili.com/{my_mid}/dynamic"},
        )
        items = ((r.json().get("data") or {}).get("items") or [])[:5]
        for it in items:
            dyn_id = it.get("id_str")
            if not dyn_id:
                continue
            basic = it.get("basic") or {}
            oid = basic.get("comment_id_str") or dyn_id
            ctype = int(basic.get("comment_type") or 11)
            rr = await client.client.get(
                "https://api.bilibili.com/x/v2/reply",
                params={"type": ctype, "oid": oid, "ps": 20, "pn": 1, "sort": 0},
                headers={"Referer": f"https://t.bilibili.com/{dyn_id}"},
            )
            for rep in ((rr.json().get("data") or {}).get("replies") or []):
                mem = rep.get("member") or {}
                key = f"dyn:{dyn_id}:{rep.get('rpid')}"
                if key in seen or key in known:
                    continue
                added.append(
                    {
                        "key": key,
                        "kind": "dyn",
                        "dyn_id": dyn_id,
                        "oid": oid,
                        "ctype": ctype,
                        "root": rep.get("rpid"),
                        "mid": mem.get("mid"),
                        "uname": mem.get("uname") or mem.get("nickname") or "",
                        "text": str((rep.get("content") or {}).get("message") or ""),
                        "at": int(rep.get("ctime") or 0),
                        "from_owner": int(mem.get("mid") or 0) == int(owner_mid or 0),
                    }
                )
    except Exception:
        pass

    # ---------- 2 私信 ----------
    try:
        rs = await client.client.get(
            "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
            params={
                "session_type": 1,
                "group_fold": 1,
                "unfollow_fold": 0,
                "sort_rule": 2,
                "build": 0,
                "mobi_app": "web",
            },
            headers={"Referer": "https://message.bilibili.com/"},
        )
        for s in (((rs.json().get("data") or {}).get("session_list") or [])[:10]):
            talker = s.get("talker_id")
            if not talker or str(talker) == str(my_mid):
                continue
            rm = await client.client.get(
                "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
                params={"talker_id": talker, "session_type": 1, "size": 5, "build": 0, "mobi_app": "web"},
                headers={"Referer": "https://message.bilibili.com/"},
            )
            for m in (((rm.json().get("data") or {}).get("messages") or [])):
                if int(m.get("sender_uid") or 0) == my_mid:
                    continue
                key = f"dm:{talker}:{m.get('msg_key')}"
                if key in seen or key in known:
                    continue
                raw = m.get("content") or ""
                try:
                    txt = json.loads(raw).get("content") if isinstance(raw, str) else str(raw)
                except Exception:
                    txt = str(raw)
                if not txt:
                    continue
                added.append(
                    {
                        "key": key,
                        "kind": "dm",
                        "talker": talker,
                        "mid": m.get("sender_uid"),
                        "uname": "",
                        "text": str(txt)[:500],
                        "at": int(m.get("timestamp") or 0),
                        "from_owner": int(m.get("sender_uid") or 0) == int(owner_mid or 0),
                    }
                )
    except Exception:
        pass

    if added:
        inbox.extend(added)
        _save(INBOX, inbox[-100:])
        # 抓到的那一刻就写进面板留言箱，别等别人再来补一遍
        try:
            import records as _rec

            for a in added:
                _rec.inbox_reply_add(
                    key=str(a.get("key") or ""),
                    kind=str(a.get("kind") or ""),
                    uname=str(a.get("uname") or ""),
                    mid=int(a.get("mid") or 0),
                    said=str(a.get("text") or ""),
                    bvid=str(a.get("bvid") or ""),
                    title=str(a.get("title") or ""),
                )
        except Exception:
            pass
    return added


def stamp(item: dict[str, Any]) -> str:
    ts = int(item.get("at") or 0)
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))

MY_COMMENTS = BASE / "data" / "my_comments.json"


def my_comments() -> list[dict[str, Any]]:
    data = _load(MY_COMMENTS, [])
    return data if isinstance(data, list) else []


def save_my_comments(items: list[dict[str, Any]]) -> None:
    _save(MY_COMMENTS, items[-200:])


def remember_comment(bvid: str, aid: Any, rpid: Any, oid: Any = None, ctype: int = 1, title: str = "") -> None:
    """记下我发过的评论，之后好回头查有没有人回我。编号暂时拿不到也先记着，回头补。"""
    items = my_comments()
    key = f"{bvid}"
    rid = int(rpid or 0)
    if rid and any(str(i.get("rpid")) == str(rid) for i in items):
        return
    if not rid and any(str(i.get("bvid")) == key and not i.get("rpid") for i in items):
        return
    items.append(
        {
            "bvid": bvid,
            "aid": aid,
            "oid": oid if oid else aid,
            "type": int(ctype or 1),
            "rpid": rid,
            "title": title,
            "at": int(time.time()),
        }
    )
    save_my_comments(items)


async def refind_root(client: Any, oid: Any, ctype: int, my_mid: int, bvid: str = "") -> int:
    """编号丢了就去评论区把我最新那条评论认回来，认到就返回。"""
    if not oid or not my_mid:
        return 0
    try:
        if int(ctype or 1) == 1:
            url = "https://api.bilibili.com/x/v2/reply"
            params = {"type": 1, "oid": oid, "sort": 0, "ps": 20, "pn": 1}
        else:
            url = "https://api.bilibili.com/x/v2/reply"
            params = {"type": int(ctype), "oid": oid, "sort": 0, "ps": 20, "pn": 1}
        r = await client.client.get(
            url, params=params, headers={"Referer": "https://www.bilibili.com"}
        )
        reps = ((r.json() or {}).get("data") or {}).get("replies") or []
        for x in reps:
            mem = x.get("member") or {}
            if str(mem.get("mid")) == str(my_mid):
                rid = int(x.get("rpid") or 0)
                if rid:
                    return rid
    except Exception:
        pass
    return 0


async def fetch_replies(client: Any, records: list[dict[str, Any]], my_mid: int) -> list[dict[str, Any]]:
    """查我发过的评论下面有没有人回我。视频评论区、动态评论区都走这里。"""
    seen = _load(SEEN, [])
    seen = set(seen) if isinstance(seen, list) else set()
    inbox = pending()
    known = {i.get("key") for i in inbox}
    added: list[dict[str, Any]] = []

    repaired = False
    for rec in records:
        oid = rec.get("oid") or rec.get("aid")
        root = int(rec.get("rpid") or 0)
        ctype = int(rec.get("type") or 1)
        if not oid:
            continue
        if not root:
            # 编号丢了，去评论区认回来，认到就顺手把档案补上
            root = await refind_root(client, oid, ctype, int(my_mid or 0), rec.get("bvid") or "")
            if not root:
                continue
            rec["rpid"] = root
            repaired = True
        try:
            r = await client.client.get(
                "https://api.bilibili.com/x/v2/reply/detail",
                params={"type": ctype, "oid": oid, "root": root},
                headers={"Referer": "https://www.bilibili.com"},
            )
            data = (r.json() or {}).get("data") or {}
        except Exception:
            continue
        # 楼中楼在 data.root.replies 里，顶层 replies 一直是空的
        root_obj = data.get("root") or {}
        pool = (data.get("replies") or []) + (root_obj.get("replies") or [])
        # 先认出这楼里哪些话是我说的：只有挂在这些话下面的回复，才算「回我」。
        # 不这么筛的话，别人在楼里互相搭话也会被当成找我的留言。
        mine_rpids = {int(root)}
        for s in pool:
            if int((s.get("member") or {}).get("mid") or 0) == int(my_mid or 0):
                mine_rpids.add(int(s.get("rpid") or 0))
        for s in pool:
            mem = s.get("member") or {}
            mid = int(mem.get("mid") or 0)
            if not mid or mid == int(my_mid or 0):
                continue
            if int(s.get("parent") or 0) not in mine_rpids:
                continue
            key = f"rep:{oid}:{s.get('rpid')}"
            if key in seen or key in known:
                continue
            added.append(
                {
                    "key": key,
                    "kind": "reply",
                    "bvid": rec.get("bvid") or "",
                    "title": rec.get("title") or "",
                    "oid": oid,
                    "ctype": ctype,
                    "root": root,
                    "mid": mid,
                    "uname": mem.get("uname") or mem.get("nickname") or "",
                    "text": str((s.get("content") or {}).get("message") or ""),
                    "at": int(s.get("ctime") or 0),
                    "from_owner": mid == int(my_mid or 0),
                }
            )

    if repaired:
        save_my_comments(records)
    if added:
        inbox.extend(added)
        _save(INBOX, inbox[-100:])
    return added
