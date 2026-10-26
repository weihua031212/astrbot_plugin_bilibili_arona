"""控制面板的后端接口。

面板页面在 pages/panel/index.html，通过 AstrBot 的插件页面桥接调用这里：
  GET  /panel/state   读当前状态
  POST /panel/action  执行动作（看视频、扫描、发送、作废、改文字、换绑）
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

from . import instance, store
from .qrlogin import LoginSession, QrLogin

PLUGIN_SLUG = "astrbot_plugin_bilibili_arona"

# 扫码会话只活在内存里，进程重启就作废，反而更干净
_QR: QrLogin | None = None
_SESS: LoginSession | None = None



# 面板能改的配置项：字段名 → (类型, 最小值, 最大值)
CONFIG_FIELDS: dict[str, tuple[str, Any, Any]] = {
    "owner_mid": ("int", 0, 10**18),
    "owner_name": ("str", 0, 40),
    "summon_enabled": ("bool", None, None),
    "summon_poll_seconds": ("int", 60, 3600),
    "inbox_watch_enabled": ("bool", None, None),
    "inbox_poll_seconds": ("int", 120, 7200),
    "reply_requires_confirm": ("bool", None, None),
    "history_limit": ("int", 0, 15),
    "provider_id": ("str", 0, 120),
    "daily_enabled": ("bool", None, None),
    "nurture_enabled": ("bool", None, None),
    "nurture_hour": ("int", 0, 23),
    "nurture_view_limit": ("int", 1, 50),
    "nurture_like_limit": ("int", 0, 50),
    "nurture_coin_limit": ("int", 0, 10),
    "nurture_share_limit": ("int", 0, 10),
    "diary_enabled": ("bool", None, None),
    "diary_hour": ("int", 0, 23),
    "read_video": ("bool", None, None),
    "vision_provider": ("str", 0, 120),
    "max_frames": ("int", 4, 24),
    "timeout_seconds": ("int", 5, 60),
    "verify_online": ("bool", None, None),
    "whisper_enabled": ("bool", None, None),
    "whisper_model": ("str", 0, 20),
    "roam_enabled": ("bool", None, None),
    "roam_interval_hours": ("int", 1, 24),
    "roam_start_hour": ("int", 0, 23),
    "roam_per_day": ("int", 0, 999),
    "roam_audit": ("bool", None, None),
    "roam_keep": ("int", 3, 30),
}


_VISION_HINTS = ("vision", "-vl", "vl-", "mimo", "omni", "gpt-4o", "qwen", "glm-4v", "gemini", "claude")


def _likely_vision(model: str, ptype: str) -> bool:
    """粗略猜这个模型吃不吃图。猜错不要紧，只是在下拉里给个提示。"""
    s = f"{model} {ptype}".lower()
    return any(h in s for h in _VISION_HINTS)


def provider_choices(plugin: Any) -> list[dict[str, Any]]:
    """给面板下拉用：列出 AstrBot 里已绑定的模型。"""
    out: list[dict[str, Any]] = []
    try:
        for prov in (plugin.context.get_all_providers() or []):
            try:
                meta = prov.meta()
                pid = str(getattr(meta, "id", "") or "")
                mdl = str(getattr(meta, "model", "") or "")
                ptype = str(getattr(meta, "type", "") or "")
            except Exception:
                pid, mdl, ptype = "", "", ""
            if not pid:
                continue
            label = pid + (" · " + mdl if mdl else "")
            out.append({
                "id": pid, "model": mdl, "type": ptype, "label": label,
                "likely_vision": _likely_vision(mdl, ptype + " " + pid),
            })
    except Exception:
        pass
    return out


def vision_default(plugin: Any) -> str:
    """AstrBot 自己配的默认识图模型，面板上让用户看得见用不选时是谁在干活。"""
    try:
        cfg = plugin.context.get_config() or {}
        return str(
            ((cfg.get("provider_settings") or {}).get("default_image_caption_provider_id"))
            or ""
        )
    except Exception:
        return ""


def config_view(plugin: Any) -> dict[str, Any]:
    c = plugin.config
    out: dict[str, Any] = {}
    for key, (kind, _lo, _hi) in CONFIG_FIELDS.items():
        raw = c.get(key)
        if kind == "int":
            try:
                out[key] = int(raw)
            except Exception:
                out[key] = 0
        elif kind == "bool":
            out[key] = bool(raw)
        else:
            out[key] = str(raw or "")
    return out


def config_apply(plugin: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """只认白名单里的字段，顺手把范围卡住。"""
    clean: dict[str, Any] = {}
    for key, (kind, lo, hi) in CONFIG_FIELDS.items():
        if key not in payload:
            continue
        raw = payload.get(key)
        if kind == "bool":
            clean[key] = bool(raw)
        elif kind == "int":
            try:
                n = int(raw)
            except Exception:
                continue
            if lo is not None:
                n = max(lo, min(hi, n))
            clean[key] = n
        else:
            clean[key] = str(raw or "").strip()[: int(hi or 120)]
    return clean


def refresh_watchers(plugin: Any) -> None:
    """改完频率和开关，让后台循环按新设置重启。"""
    for name in ("ensure_watcher", "ensure_inbox_watcher", "ensure_daily_watcher"):
        fn = getattr(plugin, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass



def watched_view(rec_mod: Any, limit: int = 30) -> list[dict[str, Any]]:
    """看片档案：我看过什么、看了里面哪些东西。"""
    pics: dict[str, str] = {}
    for s in rec_mod.summon_list(300):
        if s.get("bvid") and s.get("pic"):
            pics.setdefault(str(s["bvid"]), str(s["pic"]))
    out: list[dict[str, Any]] = []
    for v in store.watched_videos()[:limit]:
        bvid = str(v.get("bvid") or "")
        out.append(
            {
                "bvid": bvid,
                "title": v.get("title") or "",
                "owner": v.get("owner") or "",
                "duration": v.get("duration") or 0,
                "cover": v.get("cover") or pics.get(bvid, ""),
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                "watched_at": v.get("watched_at") or 0,
                "frames": v.get("saw_frames") or 0,
                "subtitle": bool(v.get("saw_subtitle")),
                "danmaku": v.get("saw_danmaku") or 0,
                "summary": v.get("summary") or "",
                "my_rpid": v.get("my_rpid") or 0,
            }
        )
    return out


async def state_handler(**_: Any) -> dict[str, Any]:
    plugin = instance.get_plugin()
    if plugin is None:
        return {"ok": False, "message": "插件还没加载好，稍等一下再刷新。"}

    plugin.ensure_watcher()
    account = await plugin.account()
    mid, name = plugin.owner()
    drafts = [d for d in store.all_drafts() if d.get("status") == "pending"]
    drafts.sort(key=lambda d: d.get("created", 0), reverse=True)
    videos = store.watched_videos()[:12]
    audit = list(reversed(store.read_audit(20)))

    from . import nurture as nurture_mod
    from . import records as rec_mod

    parts = nurture_mod.load_parts()

    return {
        "ok": True,
        "account": account,
        "owner": {"mid": mid, "name": name},
        "flags": {
            "confirm_required": bool(plugin.config.get("reply_requires_confirm", True)),
            "summon_enabled": bool(plugin.config.get("summon_enabled", False)),
        },
        "drafts": drafts,
        "videos": videos,
        "audit": audit,
        "stats": {
            "pending": len(drafts),
            "videos": len(store.watched_videos()),
            "sent": len([a for a in store.read_audit(200) if a.get("action") == "reply_sent"]),
        },
        "nurture": {
            "parts": parts,
            "all_parts": list(nurture_mod.PARTITIONS.keys()),
            "max_parts": nurture_mod.MAX_PARTS,
            "today": nurture_mod.today_summary(),
            "days": nurture_mod.view_days(14),
        },
        "diary": rec_mod.diary_list(40),
        "inbox": rec_mod.inbox_reply_list(60),
        "summons": rec_mod.summon_list(60),
        "profile": rec_mod.load_profile(),
        "review": rec_mod.review_list(80),
        "roam": rec_mod.roam_days(14),
        "roam_today": rec_mod.roam_list(50, rec_mod.today()),
        "watched": watched_view(rec_mod),
        "config": config_view(plugin),
        "providers": provider_choices(plugin),
        "vision_default": vision_default(plugin),
    }


async def action_handler(**_: Any) -> dict[str, Any]:
    plugin = instance.get_plugin()
    if plugin is None:
        return {"ok": False, "message": "插件还没加载好，稍等一下再刷新。"}

    raw = b""
    try:
        raw = await request.body()
    except Exception:
        raw = b""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        body = {}

    act = str(body.get("action") or "").strip()

    try:
        if act == "send":
            ok, msg = await plugin.send_draft(int(body.get("id") or 0))
            return {"ok": ok, "message": msg}

        if act == "drop":
            ok, msg = plugin.drop_draft(int(body.get("id") or 0))
            return {"ok": ok, "message": msg}

        if act == "edit":
            draft_id = int(body.get("id") or 0)
            text = str(body.get("text") or "").strip()
            if not text:
                return {"ok": False, "message": "正文不能是空的。"}
            if len(text) > 400:
                return {"ok": False, "message": "太长了，四百字以内。"}
            hit = store.update_draft(draft_id, reply_text=text)
            if not hit:
                return {"ok": False, "message": "没这条草稿。"}
            return {"ok": True, "message": "改好了。"}

        if act == "qr_start":
            global _QR, _SESS
            if _QR is None:
                _QR = QrLogin(float(plugin.config.get("timeout_seconds", 20)))
            try:
                _SESS, png, svg = await _QR.generate()
            except Exception as exc:
                return {"ok": False, "message": f"生成二维码失败：{exc}"}
            return {
                "ok": True,
                "message": "用 B 站手机客户端扫这个码",
                "png": png,
                "svg": svg,
                "key": _SESS.qrcode_key,
            }

        if act == "qr_poll":
            if _SESS is None:
                return {"ok": False, "message": "先点生成二维码。"}
            state, cookies = await _QR.poll(_SESS) if _QR else ("未就绪", None)
            if not cookies:
                return {"ok": True, "state": state, "done": False}
            try:
                info = await plugin.rebind(cookies)
            except Exception as exc:
                return {"ok": False, "message": f"拿到登录态但校验失败：{exc}"}
            return {
                "ok": True,
                "message": f"登录成功，现在是 {info.get('uname')}。",
                "state": state,
                "done": True,
                "uname": info.get("uname"),
            }

        if act == "config_save":
            payload = body.get("config") or {}
            if not isinstance(payload, dict):
                return {"ok": False, "message": "配置要按对象传。"}
            clean = config_apply(plugin, payload)
            if not clean:
                return {"ok": False, "message": "没有可保存的项。"}
            plugin.config.save_config(clean)
            refresh_watchers(plugin)
            msg = "已保存"
            # 有人把语音转文字打开了，但机器上没装 faster-whisper，直接告诉他
            if clean.get("whisper_enabled"):
                try:
                    import faster_whisper  # noqa: F401
                except Exception:
                    msg = "已保存。不过语音转文字要先装 faster-whisper 才生效，没装的话这条会跳过转写。"
            return {"ok": True, "message": msg, "config": config_view(plugin)}

        if act == "profile_save":
            from . import records as rec_mod

            saved = rec_mod.save_profile(body.get("profile") or {})
            return {"ok": True, "message": "已保存"}

        if act == "diary_save":
            from . import records as rec_mod

            day = str(body.get("day") or "").strip() or rec_mod.today()
            text = str(body.get("text") or "").strip()
            if not text:
                return {"ok": False, "message": "日记正文不能空着。"}
            row = rec_mod.diary_upsert(day, text, posted=bool(body.get("posted")))
            return {"ok": True, "message": f"{day} 的日记存好了。", "diary": row}

        if act == "diary_posted":
            from . import records as rec_mod

            day = str(body.get("day") or "").strip()
            ok = rec_mod.diary_set_posted(day, bool(body.get("posted", True)))
            return {"ok": ok, "message": "标成已发了。" if ok else "没这天的日记。"}

        if act == "inbox_draft":
            from . import records as rec_mod, store as store_mod

            key = str(body.get("key") or "").strip()
            if not key:
                return {"ok": False, "message": "缺少消息编号。"}
            rows = rec_mod.inbox_reply_list(200)
            row = next((r for r in rows if r.get("key") == key), None)
            if not row:
                return {"ok": False, "message": "没这条消息。"}
            parts = key.split(":")
            oid = int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else int(row.get("oid") or 0)
            parent = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            root = int(row.get("root") or 0)
            if not root:
                # 老记录没存过，就翻我发过的评论，按视频号对回来
                from . import inbox as inbox_mod

                for c in inbox_mod.my_comments():
                    if int(c.get("oid") or 0) == oid:
                        root = int(c.get("rpid") or 0)
                        break
            if not root or not parent:
                return {"ok": False, "message": "找不到原评论在哪，这条拟不了。"}
            said = str(row.get("said") or "")
            uname = str(row.get("uname") or "")
            title = str(row.get("title") or "")
            sys_p = (
                "你在 B 站评论区回一句话。别人在你自己的评论下面回你，你顺口回他一句。"
                "要求：口语、二十个字以内、可以用语气词，不客套、不解释、不宣传、不追问。只输出这一句。"
            )
            prompt = f"视频：{title}\n对方昵称：{uname}\n对方说：{said}\n\n你回他一句。"
            text = await plugin.gen(sys_p, prompt, force=True)
            text = (text or "").strip().strip("「」\"'")
            if not text:
                return {"ok": False, "message": "没拟出来，可能是模型没配好。"}
            nid = store_mod.add_draft(
                {"bvid": row.get("bvid") or "", "title": title},
                {
                    "root": root,
                    "rpid": parent,
                    "uname": uname,
                    "mid": int(row.get("mid") or 0),
                    "message": said,
                },
                text,
                inbox_key=key,
            )
            return {"ok": True, "message": f"拟好了，编号 {nid}，在上面队列里等你点头。"}

        if act == "rebind":
            cookie = str(body.get("cookie") or "").strip()
            from .bili import BiliError, parse_cookie_string

            cookies = parse_cookie_string(cookie)
            if not cookies.get("SESSDATA"):
                return {"ok": False, "message": "没读到 SESSDATA，复制整行 Cookie 再试。"}
            try:
                info = await plugin.rebind(cookies)
            except BiliError as exc:
                return {"ok": False, "message": f"保存了，但校验没过：{exc}"}
            return {"ok": True, "message": f"换好了，现在登录的是 {info.get('uname')}。"}

        return {"ok": False, "message": f"不认识的动作：{act}"}
    except Exception as exc:
        logger.exception("[bili-arona] 面板操作失败")
        return {"ok": False, "message": f"出错了：{exc}"}


def register_routes(context: Any, plugin: Any) -> None:
    for route in (f"/{PLUGIN_SLUG}/panel/state", "/panel/state"):
        context.register_web_api(route, state_handler, ["GET"], "bili看看面板：读状态")
    for route in (f"/{PLUGIN_SLUG}/panel/action", "/panel/action"):
        context.register_web_api(route, action_handler, ["POST"], "bili看看面板：执行动作")
