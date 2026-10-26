"""B 站接口客户端：只做三件事，读视频、读评论、按指令发评论。

全部走公开网页接口，用账号主人自己的登录 Cookie，不碰任何自动化点按。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import paths

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
API = "https://api.bilibili.com"

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)


class BiliError(Exception):
    """接口返回 error 或 Cookie 不可用时抛出。"""


_EMOTES: set[str] | None = None
_EMOTES_AT = 0.0


def known_emotes() -> set[str]:
    """能发的表情名，从 data/emotes.json 读；文件没了就当不知道，一律不删。"""
    global _EMOTES, _EMOTES_AT
    import time
    from pathlib import Path

    if _EMOTES is not None and time.time() - _EMOTES_AT < 86400:
        return _EMOTES
    doc: dict[str, Any] = {}
    try:
        f = paths.path("emotes.json")
        doc = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        doc = {}
    _EMOTES = {str(n) for n in (doc.get("names") or [])}
    _EMOTES_AT = time.time()
    return _EMOTES


def scrub_emotes(body: str) -> str:
    """收拾正文里的表情：方头括号改成方括号，B 站认不出来的名字直接删掉。

    不删的话发出去只会变成一串干巴巴的方括号文字，看着像出故障。
    """
    text = re.sub(r"【([^】]{1,24})】", r"[\1]", str(body or ""))
    allowed = known_emotes()
    if not allowed:
        return text.strip()

    def pick(m: re.Match[str]) -> str:
        token = m.group(0)
        return token if token in allowed else ""

    return re.sub(r"\[[^\[\]]{1,24}\]", pick, text).strip()


@dataclass
class Video:
    bvid: str
    aid: int
    title: str
    desc: str
    duration: int
    owner_mid: int
    owner_name: str
    view: int
    like: int
    danmaku: int
    cover: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    tname: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def mmss(self) -> str:
        m, s = divmod(max(0, self.duration), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Comment:
    rpid: int
    mid: int
    uname: str
    message: str
    like: int
    ctime: int
    rcount: int = 0


def extract_bvid(text: str) -> str | None:
    """从链接、短链文案或裸 BV 号里取出 BV 号。"""
    if not text:
        return None
    m = BV_RE.search(text)
    return m.group(1) if m else None


def parse_cookie_string(raw: str) -> dict[str, str]:
    """把浏览器里复制的一整行 Cookie 拆成字典。"""
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]", raw or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _walk(obj: Any, want_keys: set[str], depth: int = 0) -> list[tuple[str, Any]]:
    """在任意嵌套结构里找指定键，避免写死接口的层级。"""
    found: list[tuple[str, Any]] = []
    if depth > 8 or obj is None:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in want_keys and not isinstance(v, (dict, list)):
                found.append((k, v))
            found.extend(_walk(v, want_keys, depth + 1))
    elif isinstance(obj, list):
        for v in obj[:40]:
            found.extend(_walk(v, want_keys, depth + 1))
    return found


def _flatten_at(item: dict[str, Any]) -> dict[str, Any]:
    """把一条 @ 通知压成好用的扁平字典。"""
    user = item.get("user") or {}
    mid = int(user.get("mid") or 0)
    uname = str(user.get("nickname") or "")

    # 说话内容：真实字段是 item.source_content，兜底再深搜 message/content
    message = ""
    it = item.get("item") if isinstance(item.get("item"), dict) else {}
    for k in ("source_content", "content", "message"):
        v = it.get(k) if isinstance(it.get(k), str) else item.get(k)
        if isinstance(v, str) and v.strip():
            message = v.strip()
            break
    if not message:
        msgs = [
            v
            for k, v in _walk(item, {"source_content", "message", "content"})
            if isinstance(v, str) and v.strip()
        ]
        message = max(msgs, key=len).strip() if msgs else ""

    # 视频号：优先 bvid，其次 aid / business_id / 各种 oid
    bvid = ""
    for _, v in _walk(item, {"bvid"}):
        if isinstance(v, str) and v.startswith("BV"):
            bvid = v
            break
    if not bvid:
        for _, v in _walk(item, {"uri"}):
            if isinstance(v, str):
                got = extract_bvid(v)
                if got:
                    bvid = got
                    break
    aid = 0
    # subject_id 才是视频 aid；business_id 是业务枚举（1 = 评论），绝不能当 aid
    for key in ("subject_id", "aid", "oid"):
        for k, v in _walk(item, {key}):
            try:
                n = int(v)
            except Exception:
                continue
            if n > 0:
                aid = n
                break
        if aid:
            break

    # 我自己那条被 @ 的评论 rpid，用来把回复挂在同一条楼下。
    # 只认明确字段，躲开外层通知自带的 id，否则会挂错楼。
    rpid = 0
    # source_id 是对方那条评论的 id，优先拿它；没有再看 rpid
    for key in ("source_id", "rpid"):
        for _, v in _walk(item, {key}):
            try:
                n = int(v)
            except Exception:
                continue
            if n > 0:
                rpid = n
                break
        if rpid:
            break
    if not rpid:
        reply = item.get("reply") or {}
        if isinstance(reply, dict):
            try:
                n = int(reply.get("id") or 0)
                rpid = n if n > 0 else 0
            except Exception:
                rpid = 0

    return {
        "at_id": int(item.get("id") or 0),
        "mid": mid,
        "uname": uname,
        "message": message,
        "bvid": bvid,
        "aid": aid,
        "rpid": rpid,
        "at_time": int(item.get("at_time") or 0),
    }


class BiliClient:
    def __init__(self, cookies: dict[str, str], timeout: float = 15.0) -> None:
        self.cookies = {k: v for k, v in (cookies or {}).items() if v}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            cookies=self.cookies,
        )

    async def close(self) -> None:
        await self.client.aclose()

    # ---------- 内部 ----------

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        r = await self.client.get(API + path, params=params)
        r.raise_for_status()
        data = r.json()
        code = data.get("code")
        if code != 0:
            raise BiliError(f"接口 {path} 返回 {code}: {data.get('message')}")
        return data.get("data") or {}

    # ---------- 账号 ----------

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA")) and bool(self.cookies.get("bili_jct"))

    async def whoami(self) -> dict[str, Any]:
        """校验 Cookie 并返回登录账号信息。"""
        data = await self._get("/x/web-interface/nav", {})
        if not data.get("isLogin"):
            raise BiliError("Cookie 无效或已过期，请重新复制 SESSDATA 和 bili_jct")
        return {
            "mid": data.get("mid"),
            "uname": data.get("uname"),
            "vip": (data.get("vipStatus") or 0) == 1,
            "level": data.get("level_info", {}).get("current_level"),
        }

    # ---------- 读视频 ----------

    async def video(self, bvid: str) -> Video:
        data = await self._get("/x/web-interface/view", {"bvid": bvid})
        stat = data.get("stat") or {}
        owner = data.get("owner") or {}
        return Video(
            bvid=data.get("bvid") or bvid,
            aid=int(data.get("aid") or 0),
            title=data.get("title") or "",
            desc=data.get("desc") or "",
            duration=int(data.get("duration") or 0),
            owner_mid=int(owner.get("mid") or 0),
            owner_name=owner.get("name") or "",
            view=int(stat.get("view") or 0),
            like=int(stat.get("like") or 0),
            danmaku=int(stat.get("danmaku") or 0),
            cover=data.get("pic") or "",
            tname=str(data.get("tname") or ""),
            pages=data.get("pages") or [],
        )

    async def video_by_aid(self, aid: int) -> Video:
        data = await self._get("/x/web-interface/view", {"aid": aid})
        stat = data.get("stat") or {}
        owner = data.get("owner") or {}
        return Video(
            bvid=data.get("bvid") or "",
            aid=int(data.get("aid") or aid),
            title=data.get("title") or "",
            desc=data.get("desc") or "",
            duration=int(data.get("duration") or 0),
            owner_mid=int(owner.get("mid") or 0),
            owner_name=owner.get("name") or "",
            view=int(stat.get("view") or 0),
            like=int(stat.get("like") or 0),
            danmaku=int(stat.get("danmaku") or 0),
            cover=data.get("pic") or "",
            tname=str(data.get("tname") or ""),
            pages=data.get("pages") or [],
        )

    async def comments(self, aid: int, limit: int = 20) -> list[Comment]:
        data = await self._get(
            "/x/v2/reply",
            {"type": 1, "oid": aid, "sort": 2, "ps": min(49, max(1, limit)), "pn": 1},
        )
        out: list[Comment] = []
        for it in (data.get("replies") or [])[:limit]:
            member = it.get("member") or {}
            content = it.get("content") or {}
            out.append(
                Comment(
                    rpid=int(it.get("rpid") or 0),
                    mid=int(member.get("mid") or 0),
                    uname=member.get("uname") or "",
                    message=(content.get("message") or "").strip(),
                    like=int(it.get("like") or 0),
                    ctime=int(it.get("ctime") or 0),
                    rcount=int(it.get("rcount") or 0),
                )
            )
        return out

    async def replies_to(self, aid: int, root: int, limit: int = 20) -> list[Comment]:
        """读某条主评论下面的楼中楼，用来找谁在跟助手说话。"""
        data = await self._get(
            "/x/v2/reply/reply",
            {"type": 1, "oid": aid, "root": root, "ps": min(49, max(1, limit)), "pn": 1},
        )
        out: list[Comment] = []
        for it in (data.get("replies") or [])[:limit]:
            member = it.get("member") or {}
            content = it.get("content") or {}
            out.append(
                Comment(
                    rpid=int(it.get("rpid") or 0),
                    mid=int(member.get("mid") or 0),
                    uname=member.get("uname") or "",
                    message=(content.get("message") or "").strip(),
                    like=int(it.get("like") or 0),
                    ctime=int(it.get("ctime") or 0),
                )
            )
        return out

    # ---------- @ 我的消息 ----------

    async def at_feed(self, limit: int = 20) -> list[dict[str, Any]]:
        """读「@我的」消息流。没登录会抛 BiliError。

        返回的每一项都尽量拍平：谁在哪个视频下 @ 了我、说了什么、那条评论的 rpid。
        字段可能因接口版本不同而缺，所以用深搜兜底，不硬编码层级。
        """
        data = await self._get("/x/msgfeed/at", {"pn": 1, "ps": min(30, max(1, limit))})
        items = data.get("items") or []
        out: list[dict[str, Any]] = []
        for it in items[:limit]:
            flat = _flatten_at(it)
            if flat.get("mid") or flat.get("message"):
                out.append(flat)
        return out

    # ---------- 写评论 ----------

    async def post_comment(
        self,
        aid: int,
        message: str,
        *,
        root: int | None = None,
        parent: int | None = None,
        at_mid: int | None = None,
        at_name: str | None = None,
        ctype: int = 1,
    ) -> tuple[int, str]:
        """发一条评论或回复，返回 (rpid, 服务端回显的正文)。

        ctype 是评论所在区域的类型：视频 1，动态图文 11，视频动态 17。
        """
        csrf = self.cookies.get("bili_jct")
        if not csrf:
            raise BiliError("缺少 bili_jct，无法发评论")

        body = scrub_emotes(message)
        form: dict[str, Any] = {
            "oid": aid,
            "type": ctype,
            "message": body,
            "plat": 1,
            "csrf": csrf,
        }
        if root:
            form["root"] = root
            form["parent"] = parent or root
        if at_mid and at_name:
            # 2 级小号带 <a> 链接容易被审核吞掉，退回纯文本 @：能显示，但不会弹提醒
            prefix = f"@{at_name}"
            if body.startswith(prefix):
                body = body[len(prefix):].lstrip()
            form["at_name_to_mid"] = json.dumps({at_name: str(at_mid)}, ensure_ascii=False)
            form["message"] = f"{prefix} {body}"

        # B 站评论接口现在也要 wbi 签名，不签名会被当成机器请求直接吞掉
        # 签名器在 video.py 里，函数内导入避免循环依赖
        try:
            import video as _video

            _vr = _video.VideoReader(self.client, self.cookies)
            form = await _vr._sign(form)
            _signed = True
        except Exception:
            _signed = False

        r = await self.client.post(
            API + "/x/v2/reply/add",
            **({"params": form} if _signed else {"data": form}),
            headers={
                "User-Agent": UA,
                "Referer": f"https://www.bilibili.com/video/av{aid}/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise BiliError(f"发评论失败 {data.get('code')}: {data.get('message')}")
        payload = data.get("data") or {}
        reply_obj = payload.get("reply") or {}
        rpid = int(
            payload.get("rpid")
            or payload.get("rpid_str")
            or reply_obj.get("rpid")
            or reply_obj.get("rpid_str")
            or 0
        )
        echoed = (reply_obj.get("content") or {}).get("message") or body
        if not rpid:
            # 接口没回声编号，回评论区把刚发的那条找回来，不然以后查不到谁回复了我
            try:
                r2 = await self.client.get(
                    API + "/x/v2/reply",
                    params={"type": 1, "oid": aid, "sort": 0, "ps": 20, "pn": 1},
                    headers={
                        "User-Agent": UA,
                        "Referer": f"https://www.bilibili.com/video/av{aid}/",
                    },
                )
                reps = ((r2.json() or {}).get("data") or {}).get("replies") or []
                me = str(self.cookies.get("DedeUserID") or "")
                for x in reps:
                    mem = x.get("member") or {}
                    if me and str(mem.get("mid")) == me:
                        rpid = int(x.get("rpid") or 0)
                        if rpid:
                            echoed = (x.get("content") or {}).get("message") or echoed
                        break
            except Exception:
                pass

        # 记进档案：不管是游览、草稿还是手动评的，发出去就得留底，
        # 否则回头查不到有没有人回复这条。
        if rpid:
            try:
                import inbox as _inbox

                _inbox.remember_comment(
                    bvid="", aid=aid, rpid=rpid, oid=aid, ctype=1, title=""
                )
            except Exception:
                pass

        # 楼中楼发出去以后，顺手把留言箱里那条标成「已回」。
        # 不标的话下次再看还是「还没回」，自己都会误判成漏回。
        if echoed and root and parent:
            try:
                import records as _records

                _records.inbox_set_reply(f"rep:{aid}:{parent}", echoed)
            except Exception:
                pass
        return rpid, echoed

    # ---------- 写动态 ----------

    async def post_dynamic(self, text: str) -> tuple[str, str]:
        """发一条 B 站动态。和发评论一样要 wbi 签名，csrf 取 bili_jct。

        原来这块是靠工作区里一个独立脚本拼接口的，换个机器就找不到文件，
        所以挪进插件自己身上，谁绑了号都能用。
        """
        csrf = self.cookies.get("bili_jct")
        if not csrf:
            raise BiliError("缺少 bili_jct，没法发动态，先绑定账号")

        body_text = scrub_emotes(text)
        if not body_text.strip():
            raise BiliError("内容是空的")

        body = {
            "dyn_req": {
                "content": {
                    "contents": [{"raw_text": body_text, "type": 1, "biz_id": ""}]
                },
                "scene": 1,
            }
        }
        params: dict[str, Any] = {"platform": "web", "csrf": csrf}

        # 签名器在 video.py 里，函数内导入避免循环依赖
        try:
            import video as _video

            _vr = _video.VideoReader(self.client, self.cookies)
            params = await _vr._sign(params)
        except Exception:
            pass

        r = await self.client.post(
            API + "/x/dynamic/feed/create/dyn",
            params=params,
            json=body,
            headers={
                "User-Agent": UA,
                "Referer": "https://t.bilibili.com/",
                "Origin": "https://t.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise BiliError(f"发动态失败 {data.get('code')}: {data.get('message')}")
        payload = data.get("data") or {}
        dyn_id = str(payload.get("dyn_id_str") or payload.get("dyn_id") or "")
        return dyn_id, body_text

    async def send_dm(self, receiver_id: int, text: str) -> tuple[bool, str]:
        """发一条私信。dev_id 必须是 UUID4、还要带 msg[timestamp]，缺了会被打回 -400。"""
        import json as _json
        import time as _time
        import uuid as _uuid

        if not receiver_id or not text.strip():
            return False, "收件人或正文是空的"
        csrf = self.cookies.get("bili_jct", "")
        mid = self.cookies.get("DedeUserID", "")
        if not csrf or not mid:
            return False, "没登录，先绑定账号"
        data = {
            "msg[sender_uid]": str(mid),
            "msg[receiver_id]": str(receiver_id),
            "msg[receiver_type]": "1",
            "msg[msg_type]": "1",
            "msg[msg_status]": "0",
            "msg[dev_id]": str(_uuid.uuid4()).upper(),
            "msg[timestamp]": str(int(_time.time())),
            "msg[content]": _json.dumps({"content": scrub_emotes(text)[:2000]}, ensure_ascii=False),
            "csrf": csrf,
            "csrf_token": csrf,
            "build": "0",
            "mobi_app": "web",
        }
        try:
            r = await self.client.post(
                "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
                data=data,
                headers={
                    "Referer": "https://message.bilibili.com/",
                    "Origin": "https://message.bilibili.com",
                },
            )
            j = r.json()
        except Exception as exc:
            return False, f"请求失败：{exc}"
        if j.get("code") == 0:
            return True, str((j.get("data") or {}).get("msg_key") or "")
        return False, f"{j.get('code')} {j.get('message')}"

    async def reply_in_thread(
        self,
        aid: int,
        root: int,
        text: str,
        *,
        target_rpid: int | None = None,
        at_mid: int | None = None,
        at_name: str | None = None,
    ) -> tuple[int, str]:
        """在楼中楼里回某个人。

        parent 必须指向对方那条评论，指成自己就会变成「回复自己」。
        没给 target_rpid 就翻一遍楼中楼，挑对方最新的一条来挂。
        """
        me = await self.whoami()
        my_mid = int(me.get("mid") or 0)
        parent = int(target_rpid or 0)
        if not parent:
            try:
                reps = await self.replies_to(aid, int(root), limit=20)
            except Exception:
                reps = []
            others = [r for r in reps if int(getattr(r, "mid", 0) or 0) != my_mid]
            if others:
                parent = int(getattr(others[-1], "rpid", 0) or 0)
        if not parent:
            raise BiliError("楼中楼里没找到要回的那条评论，先别发")
        return await self.post_comment(
            aid, text, root=int(root), parent=parent, at_mid=at_mid, at_name=at_name
        )

