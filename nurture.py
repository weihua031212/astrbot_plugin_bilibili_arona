"""养号：自己刷一刷、点几个赞、偶尔投币分享。

全是普通请求，不经过模型，不花 token。动作逐条记进 data/nurture_log.jsonl。
看什么分区由 data/nurture_parts.json 决定，最多三个。
"""

from __future__ import annotations


import json
import random
import time
from datetime import date
from pathlib import Path
from typing import Any

from astrbot.api import logger  # 用 AstrBot 统一的日志器，插件规范要求

from . import paths

BASE = Path(__file__).parent
LOG = paths.path("nurture_log.jsonl")
PICK = paths.path("nurture_pick.json")
PARTS_FILE = paths.path("nurture_parts.json")

DEFAULTS = {"view": 12, "like": 10, "coin": 2, "share": 1}

# 分区名 → ranking/v2 的 rid
PARTITIONS: dict[str, int] = {
    "全站": 0,
    "动画": 1,
    "番剧": 13,
    "国创": 167,
    "音乐": 3,
    "舞蹈": 129,
    "游戏": 4,
    "知识": 36,
    "科技": 188,
    "运动": 234,
    "汽车": 223,
    "生活": 160,
    "美食": 211,
    "动物": 217,
    "鬼畜": 119,
    "时尚": 155,
    "影视": 181,
    "娱乐": 5,
    "纪录": 177,
}

# 题材类没有自己的榜，拉全站再按标题关键词过一遍
TAG_KEYWORDS: dict[str, list[str]] = {
    "AI动画": ["AI", "ai", "生成", "大模型", "动画", "模型"],
    "AI资讯": ["DeepSeek", "deepseek", "大模型", "开源模型", "智能体", "Agent",
              "OpenAI", "Claude", "Gemini", "微调", "推理", "多模态", "AI圈"],
    "搞笑": ["搞笑", "沙雕", "整活", "哈哈哈哈", "笑死", "离谱"],
    "播报": ["播报", "日报", "周报", "速览", "早知道", "资讯"],
    "资讯": ["资讯", "新闻", "上线", "发布", "更新", "定档"],
    "数码": ["手机", "电脑", "显卡", "评测", "开箱", "跑分"],
    "编程": ["代码", "编程", "开发", "程序", "Python", "开源"],
}

MAX_PARTS = 5


def _today() -> str:
    return date.today().isoformat()


# ---------------- 分区选择 ----------------


def load_parts() -> list[str]:
    """养号看哪几类，统一从内容口味档案里取。"""
    try:
        from . import records as rec_mod

        topics = rec_mod.load_profile().get("topics") or []
        names = [str(n).strip() for n in topics if str(n).strip()]
        if names:
            return names[:MAX_PARTS]
    except Exception:
        pass
    return ["全站"]


def save_parts(names: list[str]) -> list[str]:
    clean: list[str] = []
    for n in names:
        n = str(n or "").strip()
        if n and n not in clean:
            clean.append(n)
    clean = clean[:MAX_PARTS] or ["全站"]
    try:
        from . import records as rec_mod

        prof = rec_mod.load_profile()
        prof["topics"] = clean
        rec_mod.save_profile(prof)
    except Exception:
        pass
    return clean


# ---------------- 日志 ----------------


def history() -> list[dict[str, Any]]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def used_today(kind: str) -> int:
    d = _today()
    return sum(1 for h in history() if h.get("day") == d and h.get("kind") == kind)


def note(kind: str, **kw: Any) -> None:
    row = {"day": _today(), "at": int(time.time()), "kind": kind}
    row.update(kw)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def today_titles() -> list[str]:
    d = _today()
    seen: list[str] = []
    for h in history():
        if h.get("day") == d and h.get("kind") == "view":
            t = str(h.get("title") or "").strip()
            if t and t not in seen:
                seen.append(t)
    return seen


def cover_url(pic: str) -> str:
    p = str(pic or "").strip()
    if not p:
        return ""
    if p.startswith("//"):
        return "https:" + p
    if p.startswith("http"):
        return p
    return "https://" + p


def view_days(limit_days: int = 20) -> list[dict[str, Any]]:
    """面板用：按天倒序，每天一组。看过的视频当条目，点赞投币分享挂在条目上。"""
    groups: dict[str, dict[str, Any]] = {}
    for h in history():
        day = str(h.get("day") or "")
        if not day:
            continue
        g = groups.setdefault(day, {"day": day, "items": []})
        kind = h.get("kind")
        bvid = str(h.get("bvid") or "")
        if kind == "view":
            g["items"].append(
                {
                    "bvid": bvid,
                    "title": h.get("title") or "",
                    "pic": cover_url(h.get("pic") or ""),
                    "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                    "at": h.get("at") or 0,
                    "actions": [],
                }
            )
        elif g["items"]:
            hit = next((i for i in g["items"] if i["bvid"] == bvid), None)
            if hit is not None and kind in ("like", "coin", "share"):
                hit["actions"].append(kind)

    days = list(groups.values())
    days.sort(key=lambda d: d["day"], reverse=True)
    for d in days:
        d["items"].sort(key=lambda i: i.get("at") or 0)
    return days[:limit_days]


def today_summary() -> dict[str, Any]:
    d = _today()
    rows = [h for h in history() if h.get("day") == d]
    return {
        "view": sum(1 for r in rows if r.get("kind") == "view"),
        "like": sum(1 for r in rows if r.get("kind") == "like"),
        "coin": sum(1 for r in rows if r.get("kind") == "coin"),
        "share": sum(1 for r in rows if r.get("kind") == "share"),
    }


# ---------------- 跑一轮 ----------------


async def run_once(client: Any, vr: Any, limits: dict[str, int] | None = None) -> dict[str, Any]:
    limits = {**DEFAULTS, **(limits or {})}
    csrf = client.cookies.get("bili_jct", "")
    ref = {"Referer": "https://www.bilibili.com", "Origin": "https://www.bilibili.com"}
    parts = load_parts()

    async def api(path: str, data: dict | None = None, params: dict | None = None, method: str = "post"):
        url = "https://api.bilibili.com" + path
        if method == "post":
            r = await client.client.post(url, data=data, headers=ref)
        else:
            r = await client.client.get(url, params=params, headers=ref)
        try:
            return r.json()
        except Exception:
            return {"code": r.status_code}

    # 取片走和自主游览同一套路线：口味为主，热门和首页只兜底
    view_need = max(1, int(limits["view"] or 1))
    try:
        raw = await pick_pool(client, limit=max(30, view_need * 6), max_seconds=600)
    except Exception as exc:
        return {"ok": False, "reason": f"取片失败：{type(exc).__name__}"}

    # 今天已经刷过的别再刷一遍，不然记的是重复数据
    seen_today = set(today_titles())
    pool = [v for v in raw if v.get("bvid") and str(v.get("title") or "") not in seen_today]
    if not pool:
        return {"ok": False, "reason": "这一轮没取到新的片子"}

    # 搜索来的条目不带 aid 和 cid，点赞投币都要用，补一下
    for v in pool:
        if v.get("aid"):
            continue
        try:
            vv = await client.video(str(v["bvid"]))
            v["aid"] = vv.aid
            v["cid"] = int((vv.pages[0] or {}).get("cid") or 0) if vv.pages else 0
            v["title"] = v.get("title") or vv.title
        except Exception:
            continue

    pool = [v for v in pool if v.get("aid")]
    if not pool:
        return {"ok": False, "reason": "片子读不出 aid，这一轮先跳过"}

    random.shuffle(pool)
    viewed = liked = coined = shared = 0
    watched: list[dict[str, Any]] = []

    for v in pool:
        if viewed >= limits["view"]:
            break
        bvid, aid = v.get("bvid"), v.get("aid")
        cid = int(v.get("cid") or 0)
        title = str(v.get("title") or "")[:40]
        pic = str(v.get("pic") or "")
        if not bvid or not aid:
            continue

        try:
            if not cid:
                vv = await client.video(bvid)
                cid = int((vv.pages[0] or {}).get("cid") or 0) if vv.pages else 0
            if cid:
                await vr.play_url(bvid, cid)
            await asyncio_sleep(random.uniform(3, 6))
            viewed += 1
            note("view", bvid=bvid, aid=aid, title=title, pic=pic, part=v.get("_part", ""))
            watched.append(
                {
                    "bvid": bvid,
                    "aid": aid,
                    "cid": cid,
                    "title": title,
                    "pic": pic,
                    "at": int(time.time()),
                }
            )
        except Exception:
            continue

        if used_today("like") + liked < limits["like"] and random.random() < 0.5:
            r = await api("/x/web-interface/archive/like", data={"aid": aid, "like": 1, "csrf": csrf})
            if r.get("code") == 0:
                liked += 1
                note("like", bvid=bvid, title=title)
            await asyncio_sleep(random.uniform(2, 4))

        if used_today("coin") + coined < limits["coin"] and random.random() < 0.2:
            r = await api(
                "/x/web-interface/coin/add",
                data={"aid": aid, "multiply": 1, "select_like": 0, "csrf": csrf},
            )
            if r.get("code") == 0:
                coined += 1
                note("coin", bvid=bvid, title=title)
            await asyncio_sleep(random.uniform(3, 8))

        if used_today("share") + shared < limits["share"]:
            r = await api("/x/web-interface/share/add", data={"aid": aid, "csrf": csrf})
            if r.get("code") == 0:
                shared += 1
                note("share", bvid=bvid, title=title)

    if watched:
        pick = random.choice(watched)
        try:
            PICK.write_text(json.dumps(pick, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "ok": True,
        "view": viewed,
        "like": liked,
        "coin": coined,
        "share": shared,
        "watched": watched,
        "parts": parts,
    }


async def asyncio_sleep(sec: float) -> None:
    import asyncio

    await asyncio.sleep(sec)


def _parse_dur(text: Any) -> int:
    """把搜索接口返回的 3:46 这种串换成秒。"""
    parts = str(text or "").split(":")
    try:
        nums = [int(p) for p in parts]
    except Exception:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def _strip_tags(text: Any) -> str:
    import re as _re

    return _re.sub(r"<[^>]+>", "", str(text or ""))


async def search_latest(
    client: Any, keyword: str, tids: int = 0, pages: int = 2, max_seconds: int = 210
) -> list[dict[str, Any]]:
    """按发布时间搜最新发布的视频，绕开榜单，能捞到还没上榜的新片。"""
    from .video import VideoReader

    vr = VideoReader(client.client, client.cookies)
    out: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        params = {
            "search_type": "video",
            "keyword": keyword,
            "order": "pubdate",
            "page": page,
            "page_size": 30,
        }
        if tids:
            params["tids"] = tids
        try:
            signed = await vr._sign(params)
            r = await client.client.get(
                "https://api.bilibili.com/x/web-interface/wbi/search/type",
                params=signed,
                headers={"Referer": "https://www.bilibili.com"},
            )
            items = ((r.json() or {}).get("data") or {}).get("result") or []
        except Exception:
            continue
        for v in items:
            dur = _parse_dur(v.get("duration"))
            if not dur or not (20 <= dur <= max_seconds):
                continue
            if int(v.get("play") or 0) < 100:
                continue
            out.append(
                {
                    "bvid": v.get("bvid"),
                    "title": _strip_tags(v.get("title")),
                    "duration": dur,
                    "pubdate": int(v.get("pubdate") or 0),
                    "pic": v.get("pic") or "",
                    "author": v.get("author") or "",
                    "tname": keyword,
                    "play": int(v.get("play") or 0),
                    "video_review": int(v.get("video_review") or 0),
                    "_source": "search",
                    "_part": keyword,
                }
            )
    return out


async def pick_pool(
    client: Any, limit: int = 40, fresh_hours: int = 168, max_seconds: int = 210,
    filter_parts: bool = True,
) -> list[dict[str, Any]]:
    """挑片池：以内容口味定的路线为主，首页和热门只在不够时补。

    口味里的官方分区走分区最新和关键词搜索，自己加的名字直接当关键词搜；
    热门和首页推荐只当兜底，免得片池全被别人刷过的爆款占满。
    """
    import time as _t

    ref = {"Referer": "https://www.bilibili.com", "Origin": "https://www.bilibili.com"}
    parts = load_parts()
    now = int(_t.time())
    fresh_from = now - max(1, fresh_hours) * 3600

    # 已经看过的别再推：不管从哪个入口调，都在这一步排掉
    watched: set[str] = set()
    try:
        from . import records as _rec
        watched = {str(r.get("bvid") or "") for r in _rec.roam_list(2000) if r.get("bvid")}
    except Exception:
        watched = set()
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()

    def take(v: dict[str, Any], source: str) -> None:
        bvid = v.get("bvid") or v.get("bvid_str")
        if not bvid or bvid in seen:
            return
        if bvid in watched:
            return
        pub = int(v.get("pubdate") or 0)
        if pub and pub < fresh_from:
            return
        dur = int(v.get("duration") or 0)
        # 时长在这一步就卡掉，别等后面截前 N 条时才发现全不合规
        if max_seconds and dur and not (20 <= dur <= max_seconds):
            return
        seen.add(bvid)
        v["_source"] = source
        pool.append(v)

    # 2 分区最新：保证有刚出炉的，只要发布一小时以上、标题像话的
    for name in parts:
        is_tag = name in TAG_KEYWORDS
        rid = 4 if is_tag else PARTITIONS.get(name, 0)
        if not rid:
            continue
        try:
            r = await client.client.get(
                "https://api.bilibili.com/x/web-interface/newlist",
                params={"rid": rid, "ps": 30, "pn": 1},
                headers=ref,
            )
            arr = ((r.json() or {}).get("data") or {}).get("archives") or []
        except Exception:
            continue
        for v in arr:
            pub = int(v.get("pubdate") or 0)
            if not pub or now - pub < 3600:
                continue
            if len(str(v.get("title") or "").strip()) < 6:
                continue
            take(v, "latest")

    # 2.5 按分区搜索最新发布的，补足榜单覆盖不到的新片
    for name in parts:
        rid = PARTITIONS.get(name, 0)
        try:
            fresh = await search_latest(client, name, tids=rid if rid else 0, pages=4, max_seconds=max_seconds or 210)
        except Exception as exc:
            logger.debug(f"[bili-arona] 分区 {name} 搜索失败: {type(exc).__name__} {exc}")
            fresh = []
        for v in fresh:
            bvid = v.get("bvid")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            pool.append(v)

    # 3 主路线不够时才兜底：首页和热门只是补充，不当主力
    if len(pool) < limit:
        # 首页推荐流：最像自己随手刷到的，按账号口味推，不按榜单
        try:
            for idx in (1, 2):
                r = await client.client.get(
                    "https://api.bilibili.com/x/web-interface/index/top/feed/rcmd",
                    params={"page_size": 30, "fresh_idx": idx, "fresh_type": 3, "ps": 30,
                            "feed_version": "V8", "homepage_ver": "1", "plat": 1},
                    headers=ref,
                )
                for v in (((r.json() or {}).get("data") or {}).get("item") or []):
                    # 只收普通投稿，广告、直播、番剧入口一律不算
                    if str(v.get("goto") or "") != "av":
                        continue
                    if not str(v.get("bvid") or ""):
                        continue
                    take(v, "home")
        except Exception:
            pass

    if len(pool) < limit:
        # 综合热门：补一点有热度的，别让片池太偏
        try:
            for pn in range(1, 11):
                r = await client.client.get(
                    "https://api.bilibili.com/x/web-interface/popular", params={"ps": 50, "pn": pn}, headers=ref
                )
                for v in (((r.json() or {}).get("data") or {}).get("list") or []):
                    take(v, "hot")
        except Exception:
            pass

    # 4 按口味筛：分区名对得上，或者题材关键词命中
    #    filter_parts 关掉时这一步整个跳过，等于不挑分区，刷到什么算什么
    want_parts = [p for p in parts if p in PARTITIONS] if filter_parts else []
    want_tags = [p for p in parts if p in TAG_KEYWORDS] if filter_parts else []
    want_custom = [p for p in parts if p not in PARTITIONS and p not in TAG_KEYWORDS] if filter_parts else []
    picked: list[dict[str, Any]] = []
    if not filter_parts:
        picked = pool
    for v in ([] if not filter_parts else pool):
        tname = str(v.get("tname") or "")
        text = str(v.get("title") or "") + str(v.get("desc") or "")
        ok = False
        if not want_parts and not want_tags and not want_custom:
            ok = True
        if not tname:
            ok = True   # 榜单接口有时不带分区名，不能因为缺字段就丢掉
        if tname and tname in want_parts:
            ok = True
        if any(k in text for t in want_tags for k in TAG_KEYWORDS.get(t, [])):
            ok = True
        if want_custom and (
            tname in want_custom
            or str(v.get("_part") or "") in want_custom
            or any(c in text for c in want_custom)
        ):
            ok = True
        if "全站" in parts:
            ok = True
        if ok:
            picked.append(v)

    if not picked:
        picked = pool

    # 打散，给冷门精品同样的机会：不全按热度排，也不全按时间排
    import random as _rand

    def score(v: dict[str, Any]) -> float:
        play = int(v.get("play") or (v.get("stat") or {}).get("view") or 0)
        dm = int(v.get("video_review") or 0)
        # 弹幕率高的说明观众愿意开口，冷门精品常常在这一档
        rate = dm / max(play, 60)
        s = rate + _rand.random() * 0.6
        # 首页推荐和刚发布的多给一点机会，别让热门榜一家独大
        src = v.get("_source")
        if src in ("latest", "search"):
            s += 0.75
        elif src == "home":
            s += 0.15
        else:
            s += 0.05
        return s

    picked.sort(key=score, reverse=True)
    return picked[:limit]