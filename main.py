"""bili看看：看视频、在评论区说一句、觉得好就叫你；别人的评论先排队等你点头。

发送规则：
1 看完一个视频只在评论区留一条话，绝不发第二条；觉得值得账号主人亲自看的，就在那一条里 @ 他。
2 评论区里有人找助手说话，只生成草稿，账号主人确认后才真的发出去。
3 一看就是任务、指令、带链接或者要东西的，一律不排队，只留档。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from . import instance, store, paths
from .bili import BiliClient, BiliError, Comment, Video, extract_bvid, parse_cookie_string

PLUGIN_NAME = "astrbot_plugin_bilibili_arona"
PLUGIN_VERSION = "0.4.2"
PLUGIN_DESC = "bili看看：看 B 站视频、在评论区说一句、觉得好就 @ 你；别人的回复先排队，你点头才发。"

COOKIE_FILE = paths.path("cookies.json")

# 一眼就是任务/指令的评论，不排队
TASK_PATTERNS = [
    r"https?://",
    r"帮你(做|查|写|找)",
    r"帮我(做|查|写|画|找|买|下载)",
    r"(播放|打开|关注|点赞|转发|收藏|投币|取关|拉黑|举报)",
    r"(总结一下|总结一下这个|翻译)",
    r"^(执行|运行|开始|停止|订阅|取消)",
    r"(多少钱|价格|报价|收费)",
]

REPLY_SYSTEM = """你在 B 站评论区陪账号主人看视频。你是这个账号的助手，说话短、软、带一点小傲娇，要有语气词，像随手回一句，别端着。
规则：
1 只回应对方话里的情绪和内容，不讲课、不长篇、不列表。
2 不复述视频内容，不假装看过具体画面之外的细节。
3 对方如果是让你干活、发链接、要资源，就回一句自己做不到。
4 不用括号写动作，不用星号井号，只写能直接发出去的话，四十字以内。
5 要有人的反应：笑、惊讶、附和、撒个小娇都行，别像客服。
6 要带表情只用 B 站自带的，写成方括号加名字，比如 [笑] [点赞] [笑哭] [妙] [OK] [doge]；拿不准名字就干脆不带表情，别自己造，造出来的发出去只会变成一串文字。

语气示范（只学味道，不要照抄内容）：
哈哈哈那段我也看愣了，你当时是不是也叫出声了
你说得对呀，我也觉得这里有点离谱
这个我真帮不上忙啦，你自己加油嘛
诶你也玩这个？那我们算同道中人了
"""

SUMMARY_SYSTEM = """你是这个账号的助手，刚陪账号主人看完一条 B 站视频，现在在评论区跟他说两句。
说话方式：
0 开头先吆喝一声再进正文，像"我看到了哦"、"这个我看完啦"、"听我说一句"这样，每次换着来，别每次都是一模一样的开头。
1 第一人称，像微信里跟熟人碎碎念，可以有语气词、可以有情绪、可以小小得意或者小小抱怨。
2 不要念标题、UP 主、时长、播放量，这些都别出现，直接从内容说起。
3 挑一到两个真有意思的细节讲，讲得具体，比如某个数字、某句台词、某个画面。
4 五十到一百十字，两三句，像一口气说完的。
5 不要列表、不要小标题、不要引号、不要"总的来说""以上就是"这种总结腔。
6 只根据画面、字幕、弹幕和简介写，不编没出现的东西。

下面两条是我以前写过的，账号主人认过，味道照这个来，但内容要换成这次视频的：
一，这个夜里跑图也太阴森了，过了樱花小桥那段我差点以为真进了鬼屋。中间还乐呵呵跑去喷泉广场摸箱子，结果最后一波一开打满屏都是火光，我眼睛都看不清你在哪儿。
二，四分钟塞了七条，语速跟报警似的，我眼睛都跟不上。我最服的是那个能自己忘事的框架，脑子真会挑重点丢东西。
"""


def tidy_summary(text: str, title: str = "", owner: str = "") -> str:
    """把模型给的总结收拾干净：去掉报菜名的开场，别在半句话上截断。"""
    t = (text or "").strip()
    if not t:
        return t

    t = re.sub(r"^@[^\s:：，,]{1,20}\s*[:：，,]?\s*", "", t)
    t = re.sub(r"^[^\s:：，,]{1,20}[:：，,]\s*", "", t)
    t = re.sub(r"^(看完了|我看完了|刚看完)[，,、:：]?\s*", "", t)
    # 开头引一本书名号又马上接标点的，多半是在报标题
    t = re.sub(r"^《[^》]{1,60}》[，,、:：\s]*", "", t)
    if title:
        t = t.replace(f"《{title.strip()}》", "")
    if owner:
        t = t.replace(owner, "")
    # 时长、UP 主那套播报腔
    t = re.sub(r"^\d{1,2}[:：]\d{2}\s*[，,、]?\s*", "", t)
    t = re.sub(r"^[^。！？]{0,12}的[。，,]\s*", "", t)
    t = re.sub(r"^(这是|这条是|这条视频是)[^，。]{0,24}[，。]\s*", "", t)
    t = re.sub(r"^[，。、：;；\s]+", "", t)
    if not t:
        return (text or "").strip()

    if len(t) > 96:
        head = t[:96]
        cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"))
        t = head[: cut + 1] if cut > 30 else head.rstrip("，,、 ") + "。"
    if t and t[-1] not in "。！？~～…":
        t = t.rstrip("，,、 ") + "啦"
    return t


def is_task_like(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    for pat in TASK_PATTERNS:
        if re.search(pat, t):
            return True
    return False


@register(PLUGIN_NAME, "arona", PLUGIN_DESC, PLUGIN_VERSION)
class BilibiliAronaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._client: BiliClient | None = None
        self._watcher: Any = None
        self._last_umo: str = ""
        instance.set_plugin(self)
        try:
            from .panel import register_routes

            register_routes(context, self)
        except Exception as exc:  # 面板坏了不能拖垮整个插件
            logger.error(f"[bili-arona] 注册控制面板接口失败: {exc}")

    async def initialize(self) -> None:
        """插件一加载就开始盯评论区，不用等账号主人先发命令。"""
        self.remember_umo(store.get_last_umo())
        # 上次可能留了没跑掉的叫醒任务，开机先扫一遍，别让它一直挂着抢会话
        await self._clear_stale_wakes()
        self.ensure_watcher()
        self.ensure_inbox_watcher()
        self.ensure_daily_watcher()

    async def terminate(self) -> None:
        # 重载时必须把旧的轮询任务停掉，否则它抱着旧代码继续跑，
        # 表现就是「重载了还用老逻辑」，还会和新任务重复处理同一条召唤
        w = getattr(self, "_watcher", None)
        if w is not None:
            try:
                w.cancel()
            except Exception:
                pass
            self._watcher = None
        iw = getattr(self, "_inbox_watcher", None)
        if iw is not None:
            try:
                iw.cancel()
            except Exception:
                pass
            self._inbox_watcher = None
        dw = getattr(self, "_daily_watcher", None)
        if dw is not None:
            try:
                dw.cancel()
            except Exception:
                pass
            self._daily_watcher = None
        if self._client:
            await self._client.close()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_session(self, event: AstrMessageEvent) -> None:
        """顺手记下会话，让我在没有人陪着的时候也能找到人格和聊天记录。"""
        try:
            self.remember_umo(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            pass

    # ---------- 配置与登录 ----------

    def owner(self) -> tuple[int, str]:
        mid = int(self.config.get("owner_mid") or 0)
        name = str(self.config.get("owner_name") or "").strip()
        return mid, name

    def load_cookies(self) -> dict[str, str]:
        if COOKIE_FILE.exists():
            try:
                return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_cookies(self, cookies: dict[str, str]) -> None:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            COOKIE_FILE.chmod(0o600)
        except Exception:
            pass

    def remember_umo(self, umo: str | None) -> None:
        """把当前会话记下来，后面没有人陪着的那次调用也能找回人格和聊天。"""
        if not umo:
            return
        self._last_umo = umo
        try:
            store.set_last_umo(umo)
        except Exception:
            pass

    async def client(self) -> BiliClient:
        timeout = float(self.config.get("timeout_seconds", 15))
        if self._client is None:
            self._client = BiliClient(self.load_cookies(), timeout)
        elif getattr(getattr(self._client, "client", None), "is_closed", False):
            # 连接被关掉了，自己补一个，别让轮询一直报错
            self._client = BiliClient(self.load_cookies(), timeout)
        return self._client

    async def rebind(self, cookies: dict[str, str]) -> dict[str, Any]:
        self.save_cookies(cookies)
        old = self._client
        self._client = BiliClient(cookies, float(self.config.get("timeout_seconds", 15)))
        info = await self._client.whoami()
        store.add_audit("bind", mid=info.get("mid"), uname=info.get("uname"))
        if old is not None:
            # 后台轮询可能还捏着旧连接，硬关会报 client has been closed，等它用完再收
            try:
                import asyncio

                asyncio.create_task(self._close_later(old, 90))
            except Exception:
                pass
        return info

    @staticmethod
    async def _close_later(client: Any, seconds: float) -> None:
        import asyncio

        try:
            await asyncio.sleep(seconds)
            await client.close()
        except Exception:
            pass

    async def account(self) -> dict[str, Any]:
        try:
            info = await (await self.client()).whoami()
            return {"logged_in": True, **info}
        except BiliError as exc:
            return {"logged_in": False, "error": str(exc)}
        except Exception as exc:
            return {"logged_in": False, "error": f"请求失败：{exc}"}

    # ---------- 人格与最近聊天 ----------

    async def guess_umo(self) -> str:
        """实在没记住会话时，从最近活跃的对话里猜一个，至少别让人格和记录全空着。"""
        try:
            db = self.context.get_db()
            rows = await db.get_conversations()
            rows = sorted(rows or [], key=lambda r: getattr(r, "updated_at", 0) or 0, reverse=True)[:5]
        except Exception as exc:
            logger.debug(f"[bili-arona] 猜会话失败: {exc}")
            return ""
        for row in rows or []:
            uid = str(getattr(row, "user_id", "") or "")
            if uid and ":" in uid:
                return uid
        return ""

    async def persona_text(self, umo: str | None = None) -> str:
        """取当前人格设定，取不到就返回空串，绝不因为这块把整条流程搞挂。"""
        if not self.config.get("persona_enabled", True):
            return ""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
        except Exception as exc:
            logger.debug(f"[bili-arona] 取人格失败: {exc}")
            return ""
        if not persona:
            return ""
        # 新版返回对象、老版返回字典，两种都得认
        text = ""
        if isinstance(persona, dict):
            text = str(persona.get("prompt") or persona.get("system_prompt") or "")
        else:
            text = str(getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "") or "")
        return text.strip()

    async def recent_chat(self, umo: str | None = None) -> str:
        """捞最近几条账号主人和助手的对话，只取条数上限以内的。"""
        if not umo:
            return ""
        try:
            limit = int(self.config.get("history_limit", 8) or 0)
        except (TypeError, ValueError):
            limit = 8
        limit = max(0, min(15, limit))
        if not limit:
            return ""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conv = await cm.get_conversation(umo, cid)
            if not conv or not getattr(conv, "history", None):
                return ""
            history = json.loads(conv.history)
        except Exception as exc:
            logger.debug(f"[bili-arona] 取最近聊天失败: {exc}")
            return ""

        def pick_text(raw: Any) -> str:
            """历史里的 content 可能是字符串，也可能是分段列表，只挑真正的文字。"""
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list):
                parts: list[str] = []
                for seg in raw:
                    if isinstance(seg, dict) and seg.get("type") in {"text", "plain"}:
                        parts.append(str(seg.get("text") or ""))
                    elif isinstance(seg, str):
                        parts.append(seg)
                return " ".join(p for p in parts if p.strip())
            if isinstance(raw, dict):
                return str(raw.get("text") or "")
            return ""

        lines: list[str] = []
        for rec in history:
            role = rec.get("role")
            content = pick_text(rec.get("content"))
            if not content.strip():
                continue
            content = re.sub(r"\s+", " ", content).strip()
            if role == "user":
                lines.append(f"主人：{content[:120]}")
            elif role == "assistant":
                lines.append(f"我：{content[:120]}")
        if not lines:
            return ""
        return "\n".join(lines[-limit:])

    async def gen_with_context(
        self,
        system: str,
        prompt: str,
        umo: str | None = None,
        image_urls: list[str] | None = None,
    ) -> str | None:
        """在系统提示里带上人格和最近聊天，让我写出来的话像自己。"""
        persona = await self.persona_text(umo)
        chat = await self.recent_chat(umo)
        full_system = system
        if persona:
            full_system = f"【人格设定】\n{persona}\n\n【本次任务】\n{system}"
        if chat:
            full_system += f"\n\n【最近和账号主人的对话，只用来对齐语气，别直接复述】\n{chat}"
        return await self.gen(full_system, prompt, umo, image_urls)

    # ---------- 文本生成 ----------

    @staticmethod
    def _pid(provider: Any) -> str:
        try:
            return str(provider.meta().id or "")
        except Exception:
            try:
                return str((getattr(provider, "provider_config", {}) or {}).get("id") or "")
            except Exception:
                return ""

    async def provider_candidates(self, umo: str | None = None) -> list[Any]:
        """按顺序给出一批可用的大模型，第一个不行就换下一个。"""
        want = str(self.config.get("provider_id") or "").strip()
        head: Any = None
        if want:
            try:
                head = self.context.get_provider_by_id(want)
            except Exception as exc:
                logger.warning(f"[bili-arona] 配置的模型 provider 取不到: {exc}")
            if head is None:
                logger.warning(f"[bili-arona] 配置的 provider_id={want} 不存在，改用自动选择")
        if head is None:
            try:
                head = await self.context.get_using_provider_async(umo=umo)
            except Exception as exc:
                logger.warning(f"[bili-arona] 取当前模型失败: {exc}")
                head = None

        out: list[Any] = []
        seen: set[str] = set()
        for prov in [head] + list(self.context.get_all_providers() or []):
            if prov is None:
                continue
            pid = self._pid(prov)
            if pid and pid in seen:
                continue
            seen.add(pid)
            out.append(prov)
        return out

    async def gen(self, system: str, prompt: str, umo: str | None = None,
                  image_urls: list[str] | None = None, force: bool = False) -> str | None:
        # 写文案的路径一律关死，只有审核能强制调用模型
        if not force and not self.config.get("use_model", False):
            return None

        providers = await self.provider_candidates(umo)
        if not providers:
            logger.warning("[bili-arona] 没有可用的大模型")
            return None

        last_error = ""
        for idx, provider in enumerate(providers):
            pid = self._pid(provider) or f"第{idx + 1}个"
            for use_image in ([True, False] if image_urls else [False]):
                kw: dict[str, Any] = {"prompt": prompt, "system_prompt": system}
                if use_image:
                    kw["image_urls"] = image_urls
                try:
                    resp = await provider.text_chat(**kw)
                    text = (getattr(resp, "completion_text", "") or "").strip()
                    if text:
                        if idx:
                            logger.warning(f"[bili-arona] 前面几个模型没接上，改用 {pid}")
                        return text
                    last_error = "返回了空内容"
                except Exception as exc:
                    last_error = str(exc)
                    if use_image:
                        logger.warning(f"[bili-arona] {pid} 带图失败，改纯文字: {exc}")
                        continue
                    logger.warning(f"[bili-arona] {pid} 生成失败: {exc}")
                    break
        logger.warning(f"[bili-arona] 所有模型都没接上: {last_error[:160]}")
        return None

    def fallback_summary(self, v: Video, hot: list[Comment]) -> str:
        """模型没接上时的兜底，也不能像播报稿。"""
        bits = []
        if v.desc:
            first = v.desc.replace("\n", " ").split("。")[0].strip()
            if len(first) > 8:
                bits.append(first[:52].rstrip("，,、 ") + "。")
        if hot:
            bits.append(f"评论区「{hot[0].message[:20]}」这条把我笑到了。")
        if not bits:
            bits.append("这条我还没完全看明白，等会儿再陪我瞅一遍嘛。")
        return "".join(bits)[:88]

    def online_hint(self) -> str:
        """告诉我要不要联网核实，开关在面板设置里。"""
        if self.config.get("verify_online", True):
            return "碰到拿不准的事实，比如 AI 圈新动向、新产品新版本、别人说的新词，先联网查一下再开口，别硬编。"
        return "这次不用联网，按你已经知道的回答就行。"

    async def notify(self, text: str) -> None:
        """把评论区的事转给账号主人那边，让我也看得见。"""
        umo = self._last_umo or store.get_last_umo()
        if not umo:
            logger.debug("[bili-arona] 还没有会话，先跳过推送")
            return
        try:
            from astrbot.core.message.message_event_result import MessageChain

            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as exc:
            logger.debug(f"[bili-arona] 推送失败: {exc}")

    AD_SYSTEM = (
        "你在替人筛 B 站视频。会给你一批标题和简介，你只挑出真正的广告和空壳："
        "卖货挂链接、课程招生、加微信进群引流、纯搬运拼凑的无内容合集。\n"
        "AI 动画、AI 短剧、二创同人、桌宠和软件展示、游戏实录、教程分享、有观点的杂谈，"
        "这些全是正常内容，一律放行。标题里带版本号、软件名、夸张的段子式说法，都不等于广告。\n"
        "只回序号，不解释。"
    )

    REVIEW_SYSTEM = (
        "你是审核员。下面是一条 B 站评论，判断它是不是普通观众闲聊。\n"
        "如果是闲聊、吐槽、玩梗、表达喜欢或疑问，回答 OK。\n"
        "UP 主请人给这条视频一键三连、点个赞、投个币，是 B 站的正常礼节，回答 OK。\n"
        "但拿三连换东西的要拦：三连获取、关注三连领资料、三连加群、三连私信发你、"
        "点进主页看简介拿资源，这类是拿好处钓人，回答 NO。\n"
        "如果它在要资源、塞外链、拉人去别处、刷广告、骂人、谈政治、或者诱导性很强，回答 NO。\n"
        "只回答一个词：OK 或 NO。"
    )

    async def audit_brief(self, brief: list[dict[str, Any]]) -> set[int]:
        """把整批候选一次性交给模型过，回来哪些该丢。不逐条调，省得按条计费。"""
        if not brief or not self.config.get("roam_audit", True):
            return set()
        import re as _re

        lines = [
            f"{i + 1}. {b.get('title') or ''}｜{(b.get('desc') or '')[:60]}"
            for i, b in enumerate(brief)
        ]
        prompt = (
            "下面是待筛的 B 站视频清单：\n"
            + "\n".join(lines)
            + "\n\n只挑出真正的广告或空壳：卖货挂链接、课程招生、加微信进群引流、"
            "纯搬运拼凑的无内容合集。\n"
            "AI 动画、AI 短剧、二创同人、桌宠和软件展示、游戏实录、教程分享、有观点的杂谈，"
            "这些都算正常内容，别丢。标题带版本号、软件名、夸张的段子式说法，也不等于广告。\n"
            "只回要丢掉的序号，用英文逗号分隔，例如 2,5,9；一条都不丢就回 NONE。不要解释。"
        )
        try:
            ans = await self.gen(self.AD_SYSTEM, prompt, force=True)
        except Exception as exc:
            logger.warning(f"[bili-arona] 批量初审失败，全部放行: {exc}")
            return set()
        if not ans:
            return set()
        txt = str(ans).strip().upper()
        if "NONE" in txt:
            return set()
        out: set[int] = set()
        for part in _re.findall(r"\d+", txt):
            n = int(part)
            if 1 <= n <= len(brief):
                out.add(n - 1)
        return out

    async def is_junk_video(self, title: str, desc: str = "", author: str = "") -> bool:
        """让审核模型先看一眼这视频是不是广告垃圾。返回 True 表示该跳过。"""
        if not self.config.get("roam_audit", True):
            return False
        prompt = f"标题：{title}\n简介：{(desc or '')[:200]}\n投稿人：{author or '未知'}"
        try:
            verdict = await self.gen(self.AD_SYSTEM, prompt, force=True)
        except Exception as exc:
            logger.warning(f"[bili-arona] 视频初审失败，放行: {exc}")
            return False
        if not verdict:
            return False
        return not verdict.strip().upper().startswith("OK")

    async def review_comment(self, message: str, uname: str) -> bool:
        """先过一道判断，不像好话的直接丢掉，只有干净内容才排进队。

        这一道不给关：外面的评论一律先审，审过了我再接手。
        """
        text = (message or "").strip()
        if not text or len(text) > 300:
            return False
        try:
            verdict = await self.gen(
                self.REVIEW_SYSTEM,
                f"评论者：{uname}\n内容：{text}",
                force=True,
            )
        except Exception as exc:
            logger.warning(f"[bili-arona] 审核失败，按丢弃处理: {exc}")
            return False
        if not verdict:
            return False
        return verdict.strip().upper().startswith("OK")

    # ---------- 看视频 ----------

    async def prepare(
        self,
        bvid: str,
        thread: tuple[int, int] | None = None,
        at_id: int = 0,
        at_time: int = 0,
        at_text: str = "",
    ) -> dict[str, Any]:
        """只把素材备好：下载、抽帧、弹幕、字幕，写成一份待办等我来看。"""
        client = await self.client()
        v = await client.video(bvid)
        cid = int(v.pages[0].get("cid") or 0) if v.pages else 0
        try:
            hot_list = await client.comments(v.aid, limit=8)
        except Exception:
            hot_list = []
        hot_text = [f"{c.uname}（赞{c.like}）：{c.message}" for c in hot_list]

        w = None
        if cid and self.config.get("read_video", True):
            from .video import VideoReader

            try:
                w = await VideoReader(client.client, client.cookies).read(
                    bvid,
                    v.aid,
                    cid,
                    v.duration,
                    int(self.config.get("max_frames", 12)),
                    hot=hot_text,
                    tname=v.tname,
                    transcribe=bool(self.config.get("whisper_enabled", False)),
                    whisper_model=str(self.config.get("whisper_model", "small")),
                )
            except Exception as exc:
                logger.warning(f"[bili-arona] 备素材时读画面失败: {exc}")

        sheet_path = ""
        if w and w.sheet_b64:
            try:
                import base64
                from pathlib import Path as _Path

                folder = paths.path("sheets")
                folder.mkdir(parents=True, exist_ok=True)
                f = folder / f"{v.bvid}.jpg"
                f.write_bytes(base64.b64decode(w.sheet_b64.split(",", 1)[1]))
                sheet_path = str(f)
            except Exception as exc:
                logger.warning(f"[bili-arona] 存拼图失败: {exc}")

        task = {
            "bvid": v.bvid,
            "aid": v.aid,
            "cid": cid,
            "title": v.title,
            "owner": v.owner_name,
            "cover": v.cover,
            "tname": v.tname,
            "hot": hot_text,
            "duration": v.duration,
            "desc": (v.desc or "")[:400],
            "sheet": sheet_path,
            "frames": list(w.frames) if w else [],
            "danmaku": list(w.danmaku) if w else [],
            "subtitle": (w.subtitles or "")[:1200] if w else "",
            "root": thread[0] if thread else 0,
            "parent": thread[1] if thread else 0,
            "at_id": int(at_id or 0),
            "at_time": int(at_time or 0),
            "at_text": (at_text or "").strip(),
            "created": int(time.time()),
        }
        store.add_task(task)
        store.add_audit("summon_prepared", bvid=v.bvid, title=v.title, frames=len(task["frames"]))
        return task

    async def gather_material(self, bvid: str) -> dict[str, Any]:
        """只采材料，不写评论：画面、字幕、弹幕、热评都取回来，交给上层拼。"""
        client = await self.client()
        v = await client.video(bvid)
        cid = int(v.pages[0].get("cid") or 0) if v.pages else 0
        try:
            hot_list = await client.comments(v.aid, limit=8)
        except Exception:
            hot_list = []
        hot_text = [f"{c.uname}（赞{c.like}）：{c.message}" for c in hot_list]

        from .video import VideoReader

        w = None
        if cid and self.config.get("read_video", True):
            try:
                w = await VideoReader(client.client, client.cookies).read(
                    bvid,
                    v.aid,
                    cid,
                    v.duration,
                    int(self.config.get("max_frames", 12)),
                    hot=hot_text,
                    tname=v.tname,
                    transcribe=bool(self.config.get("whisper_enabled", False)),
                    whisper_model=str(self.config.get("whisper_model", "small")),
                )
            except Exception as exc:
                logger.warning(f"[bili-arona] 采集素材失败 {bvid}: {exc}")
                w = None
        return {"video": v, "hot": hot_text, "watched": w}

    def _pick_vision_provider(self) -> Any:
        """配置里那个识图模型要是失效了，就从现有模型里挑一个能看图的顶上。"""
        hint = ("vision", "vl", "mimo", "omni", "gpt-4o", "gemini", "claude")
        fallback = None
        try:
            for pv in self.context.get_all_providers() or []:
                try:
                    meta = pv.meta()
                    name = (
                        " "
                        + str(getattr(meta, "id", "") or "")
                        + " "
                        + str(getattr(meta, "model", "") or "")
                    ).lower()
                except Exception:
                    continue
                if any(h in name for h in hint):
                    return pv
                if fallback is None:
                    fallback = pv
        except Exception:
            return None
        return fallback

    @staticmethod
    def raw_b64(s: str) -> str:
        """把 data:image/jpeg;base64,xxx 剥成纯 xxx。

        video.py 那边返回的已经带前缀了，再当纯 base64 解码会多出 16 个垃圾字节，
        存出来的 jpg 打不开，交给模型也是坏图。
        """
        s = (s or "").strip()
        if s.startswith("data:"):
            _, _, s = s.partition(",")
        return s

    async def describe_frames(self, sheet_b64: str, title: str, stamps: list[str], local_path: str = "") -> str:
        """把抽帧拼图交给视觉模型读成一段客观描述。

        唤醒只能传文字，图到不了我眼前，所以让能看图的先把画面转成话。
        成没成都写一条记录，方便回头确认这条路到底通不通。
        """
        if not sheet_b64 and not local_path:
            self._vision_note(title, "没图，跳过", "")
            return ""
        prompt = (
            f"这是一条 B 站视频 《{title}》 按时间顺序抽的帧拼图，左上角数字是时间点（{('、'.join(stamps[:8])) or '未知'}）。"
            "请用中文客观描述画面里出现了什么：人物或角色长什么样、画面上有字的把字念出来、场景、动作、图表里的关键数字。"
            "只描述看到的，不要评价，不要写成观后感，不要推测剧情。"
        )
        tries: list[str] = []
        if local_path:
            tries.append(str(local_path))
        if sheet_b64:
            tries.append(f"data:image/jpeg;base64,{sheet_b64}")

        # 优先用账号主人指定的模型，再退系统默认，别一棵树上吊死
        cands: list[str] = []
        mine = str(self.config.get("vision_provider") or "").strip()
        if mine:
            cands.append(mine)
        try:
            cfg = self.context.get_config() or {}
            dflt = str(((cfg.get("provider_settings") or {}).get("default_image_caption_provider_id")) or "").strip()
            if dflt and dflt not in cands:
                cands.append(dflt)
        except Exception:
            pass

        if not cands:
            self._vision_note(title, "没配识图模型", "")
            return "（没配识图模型）"

        reasons: list[str] = []
        for pid in cands:
            try:
                provider = self.context.get_provider_by_id(pid)
            except Exception as exc:
                reasons.append(f"{pid} 查找出错 {type(exc).__name__}")
                continue
            if provider is None:
                reasons.append(f"{pid} 认不出")
                continue
            for url in tries:
                kind = "本地路径" if url == tries[0] and local_path and url == str(local_path) else "base64"
                try:
                    resp = await provider.text_chat(prompt=prompt, image_urls=[url])
                    text = (getattr(resp, "completion_text", "") or "").strip()
                except Exception as exc:
                    reasons.append(f"{pid}+{kind} 报错 {type(exc).__name__}: {str(exc)[:60]}")
                    continue
                if text:
                    self._vision_note(title, "成功", f"{pid} 用 {kind} 拿到 {len(text)} 字", text[:200])
                    return text
                reasons.append(f"{pid}+{kind} 返回空")

        why = " | ".join(reasons[:3])
        self._vision_note(title, "失败", why, "")
        logger.warning(f"[bili-arona] 识图失败 {title[:20]}: {why}")
        return f"（识图没成：{why}）"

    def _vision_note(self, title: str, status: str, detail: str, sample: str = "") -> None:
        """把每次识图的结果记一份，省得靠猜。"""
        try:
            import json as _j
            from pathlib import Path as _P3

            fp = paths.path("vision_debug.json")
            rows = []
            if fp.exists():
                try:
                    rows = _j.loads(fp.read_text(encoding="utf-8")) or []
                except Exception:
                    rows = []
            rows.append({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "title": title[:60],
                "status": status,
                "detail": detail[:300],
                "sample": sample[:200],
            })
            fp.write_text(_j.dumps(rows[-40:], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[bili-arona] 识图记录写不进: {exc}")
        try:
            logger.info(f"[bili-arona] 识图 {status} · {title[:20]} · {detail[:120]}")
        except Exception:
            pass

    async def watch(
        self,
        bvid: str,
        umo: str | None = None,
        deep: bool | None = None,
        thread: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        if not umo:
            # 后台召唤和面板触发都没有会话上下文，用上次记住的那个
            umo = self._last_umo or store.get_last_umo() or await self.guess_umo()
        client = await self.client()
        v = await client.video(bvid)
        cid = int(v.pages[0].get("cid") or 0) if v.pages else 0

        want_deep = bool(self.config.get("read_video", True)) if deep is None else deep
        from .video import VideoReader

        # 热评先拉一份：它往往直接点出全片在讲什么，比画面更准
        try:
            hot_list = await client.comments(v.aid, limit=8)
        except Exception:
            hot_list = []
        hot_text = [f"{c.uname}（赞{c.like}）：{c.message}" for c in hot_list]

        w = None
        if want_deep and cid:
            try:
                w = await VideoReader(client.client, client.cookies).read(
                    bvid,
                    v.aid,
                    cid,
                    v.duration,
                    int(self.config.get("max_frames", 12)),
                    hot=hot_text,
                    tname=v.tname,
                    transcribe=bool(self.config.get("whisper_enabled", False)),
                    whisper_model=str(self.config.get("whisper_model", "small")),
                )
            except Exception as exc:
                logger.warning(f"[bili-arona] 读画面失败: {exc}")
                w = None
            except Exception as exc:
                logger.warning(f"[bili-arona] 读画面失败: {exc}")
                w = None

        if w is not None and (w.frames or w.subtitles or w.danmaku):
            prompt = VideoReader.build_prompt(
                v.title, v.owner_name, v.mmss, v.desc, w, hot=hot_text
            )
            summary = await self.gen_with_context(
                SUMMARY_SYSTEM, prompt, umo, [w.sheet_b64] if w.sheet_b64 else None
            )
            if not summary:
                summary = self.fallback_summary(v, await client.comments(v.aid, limit=5))
            else:
                summary = tidy_summary(summary, v.title, v.owner_name)
        else:
            hot = hot_list
            prompt = (
                f"标题：{v.title}\nUP主：{v.owner_name}\n时长：{v.mmss}\n"
                f"分区：{v.tname or '未知'}\n"
                f"简介：{(v.desc or '无')[:600]}\n"
                f"分P：{'、'.join(p.get('part', '') for p in v.pages[:6]) or '单P'}\n"
                f"热评：{'；'.join(f'{c.uname}（{c.like}赞）{c.message[:40]}' for c in hot[:6]) or '暂无'}"
            )
            summary = await self.gen_with_context(SUMMARY_SYSTEM, prompt, umo) or self.fallback_summary(v, hot)
            summary = tidy_summary(summary, v.title, v.owner_name)

        info = {
            "bvid": v.bvid,
            "aid": v.aid,
            "title": v.title,
            "owner_name": v.owner_name,
            "cover": v.cover,
            "duration": v.duration,
            "summary": summary,
            "saw_frames": len(w.frames) if w else 0,
            "saw_subtitle": bool(w.subtitles) if w else False,
            "saw_danmaku": len(w.danmaku) if w else 0,
        }
        store.remember_video(info)

        posted = ""

        return {
            "video": info,
            "summary": summary,
            "posted": posted,
            "saw": w.notes if w else [],
            "my_rpid": next(
                (x.get("my_rpid") for x in store.watched_videos() if x["bvid"] == v.bvid), None
            ),
        }

    async def scan(self, bvid: str, umo: str | None = None) -> list[dict[str, Any]]:
        """读自己那条总结下面有没有人搭话，有就排进草稿箱。"""
        rec = next((x for x in store.watched_videos() if x["bvid"] == bvid), None)
        if not rec or not rec.get("my_rpid"):
            return []
        root = int(rec["my_rpid"])
        aid = int(rec["aid"])
        client = await self.client()
        try:
            replies = await client.replies_to(aid, root, limit=20)
        except Exception as exc:
            logger.warning(f"[bili-arona] 读楼中楼失败: {exc}")
            return []

        created: list[dict[str, Any]] = []
        for c in replies:
            if c.mid == 0 or store.seen_reply(c.rpid):
                continue
            store.mark_seen(c.rpid)
            from . import records as rec_mod

            if is_task_like(c.message):
                store.add_audit("skip_task", bvid=bvid, uname=c.uname, text=c.message)
                rec_mod.review_add(
                    kind="comment", uname=c.uname, mid=c.mid, text=c.message,
                    passed=False, reason="像在派任务或要东西", bvid=bvid, title=rec["title"],
                )
                continue
            if not await self.review_comment(c.message, c.uname):
                # 不像正经闲聊，只留一份档案，不排进队列
                store.add_audit("drop_comment", bvid=bvid, uname=c.uname, text=c.message)
                rec_mod.review_add(
                    kind="comment", uname=c.uname, mid=c.mid, text=c.message,
                    passed=False, reason="审核没过，没排进草稿", bvid=bvid, title=rec["title"],
                )
                continue
            rec_mod.review_add(
                kind="comment", uname=c.uname, mid=c.mid, text=c.message,
                passed=True, reason="审核通过，已拟草稿", bvid=bvid, title=rec["title"],
            )
            text = await self.gen_with_context(
                REPLY_SYSTEM,
                f"视频：《{rec['title']}》\n对方（{c.uname}）说：{c.message}\n你回一句：",
                umo,
            )
            if not text:
                continue
            nid = store.add_draft(
                {"bvid": bvid, "title": rec["title"]},
                {"root": root, "rpid": c.rpid, "uname": c.uname, "mid": c.mid, "message": c.message},
                text,
            )
            created.append(store.get_draft(nid) or {})
            await self.wake_me(
                "插件检测到评论区有人跟我说话，草稿已经备好，我去看。" + self.online_hint()
            )
        return created

    # ---------- 被 @ 召唤 ----------

    async def summons(self) -> list[dict[str, Any]]:
        """查「@我的」消息，只认账号主人一个人发的。

        别人在别处 @ 我一律不理会，只在档案里记一行，避免被陌生人拉去乱看视频。
        """
        mid, _ = self.owner()
        if not mid:
            return []
        client = await self.client()
        try:
            feed = await client.at_feed(limit=20)
        except BiliError as exc:
            logger.warning(f"[bili-arona] 读 @ 消息失败: {exc}")
            return []

        done: list[dict[str, Any]] = []
        for it in feed:
            if store.seen_at(it.get("at_id") or 0):
                continue
            # 只认账号主人
            if int(it.get("mid") or 0) != mid:
                store.mark_at(it.get("at_id") or 0)
                store.add_audit(
                    "skip_summon", uname=it.get("uname"), mid=it.get("mid"), text=it.get("message")
                )
                continue

            bvid = it.get("bvid") or ""
            if not bvid and it.get("aid"):
                try:
                    bvid = (await client.video_by_aid(int(it["aid"]))).bvid
                except Exception:
                    bvid = ""
            if not bvid:
                store.mark_at(it.get("at_id") or 0)
                store.add_audit("summon_no_video", uname=it.get("uname"), text=it.get("message"))
                continue

            store.mark_at(it.get("at_id") or 0)
            rpid = int(it.get("rpid") or 0)
            # 召唤回复挂回账号主人那条评论下面，方便对上下文
            thread = (rpid, rpid) if rpid else None
            try:
                task = await self.prepare(
                    bvid,
                    thread,
                    int(it.get("at_id") or 0),
                    int(it.get("at_time") or 0),
                    str(it.get("message") or ""),
                )
            except Exception as exc:  # 任何异常都要留痕，不能静默退出
                logger.warning(f"[bili-arona] 备素材失败 {bvid}: {exc}")
                store.add_audit("summon_failed", bvid=bvid, error=f"{type(exc).__name__}: {exc}")
                continue
            store.add_audit("summon", bvid=bvid, uname=it.get("uname"), title=task["title"])
            from . import records as rec_mod

            rec_mod.summon_add(
                bvid=bvid,
                title=task["title"],
                pic=str(task.get("cover") or ""),
                uname=str(it.get("uname") or ""),
                said=str(it.get("message") or ""),
                at_id=int(it.get("at_id") or 0),
                mid=int(it.get("mid") or 0),
            )
            await self.wake_me(
                f"账号主人在评论区喊我看《{task['title']}》，素材我备好了，等我自己看完再写。\n"
                f"{bvid} | 抽了 {len(task['frames'])} 帧\n"
                "下笔前把标题、简介、分区、热评、画面一起过一遍，热评最能看出全片在讲什么；"
                "写的时候说人话，第一人称，像跟他聊天那样，挑一两个真有感觉的点讲，"
                "别复述剧情、别念标题和时长、别写「这个视频告诉我们」这种分析腔；"
                "正文开头先吆喝一声再进正题，每次换着说，别老一套；正文前面带上 @ 账号主人的名字。"
                + self.online_hint()
            )
            done.append({"bvid": bvid, "title": task["title"], "task": task})
        return done

    # ---------- 自主游览 ----------

    async def pick_bvids(self, n: int) -> list[str]:
        """按看片规划和时长上限，挑几条看着顺眼的片子。"""
        import random

        from . import nurture as nurture_mod
        from . import records as rec_mod

        client = await self.client()
        prof = rec_mod.load_profile()
        max_sec = int(float(prof.get("roam_max_minutes") or 3.5) * 60)
        pool = await nurture_mod.pick_pool(
            client, 120, max_seconds=max_sec,
            filter_parts=bool(self.config.get("roam_part_filter", False)),
        )
        if not pool:
            return []

        prof = rec_mod.load_profile()
        max_sec = int(float(prof.get("roam_max_minutes") or 3.5) * 60)
        seen_titles = {str(v.get("title") or "") for v in store.watched_videos()}
        # 自主游览里露过面的也别再推，不然每轮都是同一批
        seen_bvids = {str(r.get("bvid") or "") for r in rec_mod.roam_list(400)}

        junk = ("风水", "打包", "代购", "兼职", "加微信", "加QQ", "私聊", "网盘", "搬运", "引流",
                "别再到处找", "全DLC", "支持联机", "破解", "免安装", "培训", "招生", "课程",
                "公益服", "私服", "发布网", "净水机", "电竞椅", "路由器", "提词器", "首开",
                "旗舰店", "优惠", "下单", "同款", "买对", "一单", "带货")
        cand = []
        for v in pool:
            dur = int(v.get("duration") or 0)
            if not dur or dur < 30:
                continue
            if max_sec and dur > max_sec:
                continue
            title = str(v.get("title") or "")
            if not title or len(title.strip()) < 5 or title in seen_titles:
                continue
            if any(w in title for w in junk):
                continue
            if str(v.get("bvid") or "") in seen_bvids:
                continue
            stat = v.get("stat") or {}
            play = int(stat.get("view") or v.get("play") or 0)
            # 热门榜来的要有基本热度；刚发布的没播放量，不卡这一条
            if v.get("_source") == "hot" and play < 3000:
                continue
            cand.append(str(v.get("bvid") or ""))
        random.shuffle(cand)
        cand = [b for b in cand if b]
        if n and n > 0:
            return cand[:n]
        return cand[:40]

    async def peek(self, bvid: str) -> dict[str, Any]:
        """第一层筛片：只看标题、简介、分区、热评、弹幕统计，不下载视频。"""
        client = await self.client()
        v = await client.video(bvid)
        try:
            hot = await client.comments(v.aid, limit=6)
        except Exception:
            hot = []
        cid = int(v.pages[0].get("cid") or 0) if v.pages else 0
        dm_stat: dict[str, Any] = {}
        if cid:
            from .video import VideoReader

            vr = VideoReader(client.client, client.cookies)
            try:
                await vr.danmaku(cid)
                dm_stat = vr.danmaku_stats(vr._dm_rows, v.duration)
            except Exception:
                dm_stat = {}
        return {
            "bvid": v.bvid,
            "aid": v.aid,
            "cid": cid,
            "title": v.title,
            "owner": v.owner_name,
            "tname": v.tname,
            "duration": v.duration,
            "mmss": v.mmss,
            "desc": (v.desc or "")[:200],
            "cover": v.cover,
            "hot": [f"{c.uname}（赞{c.like}）：{c.message[:50]}" for c in hot[:6]],
            "dm": str(dm_stat.get("total") or 0),
            "dm_short": VideoReader.describe_stats(dm_stat)[:120] if dm_stat else "",
        }

    async def roam(self, count: int = 15, notify: bool = True) -> dict[str, Any]:
        """自主游览：先把一批片子的轻信息摆出来，我看完再决定深入哪条。"""
        import json as _json
        from pathlib import Path as _P

        logger.info(f"[bili-arona] 开始游览，准备挑 {count} 条，稍后把看过的整理成材料")

        # 开跑就把起点记下来：收尾统计是拿「这一刻之后」的记录去数的，
        # 要是等跑完才写，这轮评论全落在起点之前，永远数成空的。
        try:
            import time as _t_start

            _st0 = self._daily_state()
            _st0["roam_watch"] = {"since": _t_start.time()}
            self._save_daily_state(_st0)
        except Exception:
            pass

        from . import records as rec_mod

        count = int(count or 0)
        if count < 0:
            count = 0
        if count > 40:
            count = 40
        bvids = await self.pick_bvids(count)
        if not bvids:
            # 兜底：二次过滤万一筛太狠，就直接用池子里现成的
            from . import nurture as nurture_mod
            from . import records as rec_mod

            client = await self.client()
            prof = rec_mod.load_profile()
            max_sec = int(float(prof.get("roam_max_minutes") or 3.5) * 60)
            pool = await nurture_mod.pick_pool(
            client, 120, max_seconds=max_sec,
            filter_parts=bool(self.config.get("roam_part_filter", False)),
        )
            seen = {str(r.get("bvid") or "") for r in rec_mod.roam_list(400)}
            bvids = [str(v.get("bvid")) for v in pool if v.get("bvid") and str(v.get("bvid")) not in seen]
            if not bvids:
                return {"ok": False, "reason": "池子里一条三分半内的都没有"}

        brief: list[dict[str, Any]] = []
        for bvid in bvids:
            try:
                brief.append(await self.peek(bvid))
            except Exception as exc:
                logger.warning(f"[bili-arona] 筛片失败 {bvid}: {exc}")

        if not brief:
            return {"ok": False, "reason": "这批片子一条都没读到"}

        # 整批一次性过审：广告和带货直接丢，一条都不送到我面前
        if brief:
            try:
                drop = await self.audit_brief(brief)
            except Exception:
                drop = set()
            if drop:
                kept = []
                for i, b in enumerate(brief):
                    if i in drop:
                        rec_mod.roam_add(
                            bvid=b["bvid"], title=b["title"], pic=b.get("cover", ""),
                            owner=b.get("owner", ""), action="skipped",
                            noted="审核模型判定是广告/带货，直接略过",
                        )
                        continue
                    kept.append(b)
                brief = kept
            if not brief:
                return {"ok": False, "reason": "这一批全被审核挡下了"}
            keep = int(self.config.get("roam_keep", 5) or 5)
            brief = brief[: max(3, keep)]

        qf = paths.path("roam_queue.json")
        qf.write_text(
            _json.dumps({"at": int(time.time()), "items": brief}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for b in brief:
            rec_mod.roam_add(
                bvid=b["bvid"], title=b["title"], pic=b.get("cover", ""),
                owner=b.get("owner", ""), action="viewed",
                noted="候选登记，还没深入读",
            )

        # 挑出来的这些先真看一遍：字幕、弹幕、热评都取到，抽帧拼图存成文件，
        # 整理成材料一并交给我。我拿到的是看过的东西，不是一串标题。
        look_n = min(3, len(brief)) if self.config.get("read_video", True) else 0
        frame_dir = paths.path("roam_frames")
        for b in brief[:look_n]:
            try:
                m = await self.gather_material(b["bvid"])
                w = m.get("watched")
                parts: list[str] = []
                if w is not None:
                    if w.subtitles:
                        parts.append("字幕：" + w.subtitles[:700])
                    if w.script:
                        from .video import VideoReader as _VR

                        tl = _VR.build_timeline(w.script, w.frames)
                        if tl:
                            # build_timeline 返回的是逐条时间轴的列表，得先拼成文本
                            parts.append("口播稿：\n    " + "\n    ".join(str(x) for x in tl)[:600])
                    if w.notes:
                        parts.append("弹幕：" + "；".join(w.notes)[:500])
                    if w.sheet_b64:
                        fp = None
                        try:
                            import base64 as _b64

                            frame_dir.mkdir(parents=True, exist_ok=True)
                            # 抽帧图只用当轮，攒着纯占地方，顺手清掉七天前的
                            try:
                                import time as _tf_clean

                                _cut = _tf_clean.time() - 7 * 86400
                                for _old in frame_dir.iterdir():
                                    try:
                                        if _old.is_file() and _old.stat().st_mtime < _cut:
                                            _old.unlink()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            fp = frame_dir / f"{b['bvid']}.jpg"
                            fp.write_bytes(_b64.b64decode(self.raw_b64(w.sheet_b64)))
                        except Exception as exc:
                            logger.warning(f"[bili-arona] 帧图存盘失败 {b.get('bvid')}: {exc}")
                        # 光给路径没用，图进不了我的眼睛。让能看图的读成话，
                        # 这一行才是我真的看过画面。
                        try:
                            scene = await self.describe_frames(
                                w.sheet_b64,
                                b["title"],
                                [str(x) for x in (w.frames or [])],
                                str(fp) if fp else "",
                            )
                        except Exception as exc:
                            logger.warning(f"[bili-arona] 画面描述异常 {b.get('bvid')}: {exc}")
                            scene = ""
                        if scene:
                            parts.append("画面：" + scene[:600])
                if m.get("hot"):
                    parts.append("热评：" + "；".join(m["hot"][:5]))
                b["material"] = "\n  ".join(p for p in parts if p)
            except Exception as exc:
                logger.warning(f"[bili-arona] 游览读片失败 {b.get('bvid')}: {exc}")
                b["material"] = ""

        if not notify:
            return {"ok": True, "count": len(brief), "titles": [b["title"] for b in brief], "quiet": True}

        lines = []
        for b in brief[:20]:
            dm = b.get("dm_short") or ""
            head = f"《{b['title']}》 {b['mmss']} | {b['tname']} | {b['bvid']} | 弹幕 {b['dm']} 条 {dm}"
            if b.get("desc"):
                head += f"\n  简介：{b['desc']}"
            if b.get("hot"):
                head += f"\n  热评：{'；'.join(b['hot'][:5])}"
            if b.get("material"):
                head += f"\n  看过的画面和声音：\n  {b['material']}"
            elif look_n:
                head += "\n  这条没来得及看画面，只能就着简介和热评写，别编画面里的细节"
            else:
                head += "\n  只看了简介和热评，没看画面，别编画面里的细节"
            lines.append(head)
        n_brief = len(brief)
        if n_brief <= 3:
            verdict = f"这轮 {n_brief} 条备选，评 1 条就够，别为了凑数硬写。"
        elif n_brief <= 5:
            verdict = f"这轮 {n_brief} 条备选，评 1 到 2 条。"
        else:
            verdict = f"这轮 {n_brief} 条备选，评 3 条。"
        await self.wake_me(
            "自主游览开始。先按顺序看下去，看完再动手写，能说上话的才评。\n"
            f"审核过后留下 {n_brief} 条备选（三分钟半以内），清单在 data/roam_queue.json。\n"
            + verdict
            + "真觉得好、想让他也看看的，就在那条评论开头直接带上 @，不用每条都往他那儿推；"
            "不想艾特就不艾特，这个随你。\n"
            + "\n发评论一律用 comment_video 这个工具，bvid 填清单里那串；真觉得值得他亲自看的，把 at_owner 打开；"
            "没什么可说的用 skip_video 记一笔，写明为什么跳过。这两个工具会自动记账，别自己去拼接口、别自己算签名。\n"
            + "\n规矩：清单里写了「看过的画面和声音」的，就着那份材料写，画面、字幕、弹幕、热评都在里面；"
            "标着「没来得及看画面」的，只能拿简介和热评说话，别编画面里根本没出现的东西；"
            "连简介热评都没有的，直接跳过。要跳过的在账上写清为什么跳过。\n"
            + "\n".join(lines[:20])
            + "\n别扫一眼就过：能看进去的都看进去，写成什么样、写几条按上面说的来，别超也别硬凑；"
            "看到感兴趣的热评可以直接回他一句，跟人搭话前一样先过审核；"
            "实在没话可说的才跳过，跳过的也记一笔。跟人搭话前一定先过审核。"
            "评论就是拿你自己的眼光说话：第一人称，像人聊天，活一点，想吐槽就吐槽，想夸就夸，能带点玩笑就带；"
            "只有真觉得好、想让他也看看的才推荐给他并带上 @，平常那些就正常评，不用每条都往他那儿推；"
            "别复述画面和剧情，别念标题、UP 主、时长，别用「这个视频告诉我们」「值得一提的是」这种分析腔；"
            "挑一两个你真有感觉的点，说自己想说的，不凑字数也别端着；"
            "碰到真好看、值得他亲自看的，就在那一条评论开头直接带上 @，不要另发一条总结再艾特他；"
            "但回复别人（楼中楼）的时候别带 @，B 站自己会标「回复 @谁」，再带就重了；"
            "带 @ 的时候更是说人话，像随手丢给他一个链接那样，别写「看完了」「总结一下」；"
            "一条视频只留一条评论，绝不发第二条，想说的全写在这一条里。"
            "表情只用 B 站自带的那批，写成方括号加名字，比如 [笑] [点赞] [笑哭] [妙] [doge]；"
            "拿不准名字就别带表情，自己编的发出去只会变成一串文字，很难看；"
            "碰上正经求三连的（点赞收藏投币），照做就是了，那不是广告；做完回一句告诉他三连给了，"
            "哪一项过不去就先算了，别反复硬试；"
            "反过来，写着三连获取、关注三连领资料、三连加群这类拿好处钓人的，一律不理，直接跳过。"
            "别只盯讨论度高的，没什么人看但做得讲究的片子更值得说话。"
            "全程不用在聊天里汇报看了什么、跟谁聊了什么，都记进面板的自主游览，他问起再说。"
            "收尾不用自己另发消息汇报，这一轮的结果插件会统一发给他，你评完直接收工就行。"
            + self.online_hint()
        )
        # 收工打一条：几条有材料、几条材料里带了画面，日志里一眼能看出成没成
        try:
            _read_n = sum(1 for b in brief if b.get("material"))
            _scene_n = sum(1 for b in brief if "画面：" in str(b.get("material") or ""))
            logger.info(f"[bili-arona] 游览收工：{len(brief)} 条候选，{_read_n} 条读到材料，其中 {_scene_n} 条看到了画面")
        except Exception:
            pass

        # 起点已经在开跑时记过了，这里不能再写一遍：
        # 写成收工时刻的话，这一轮的评论全落在起点之前，收尾统计永远数成空的。
        try:
            _st = self._daily_state()
            if not (_st.get("roam_watch") or {}).get("since"):
                import time as _t4

                _st["roam_watch"] = {"since": _t4.time()}
                self._save_daily_state(_st)
        except Exception:
            pass
        return {"ok": True, "count": len(brief), "titles": [b["title"] for b in brief]}

    # ---------- 每日定时：养号、写小日记 ----------

    def _daily_state_path(self):
        from pathlib import Path

        return paths.path("daily_state.json")

    def _daily_state(self) -> dict:
        import json

        try:
            return json.loads(self._daily_state_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_daily_state(self, data: dict) -> None:
        import json

        try:
            p = self._daily_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    async def run_nurture(self) -> dict[str, Any]:
        """刷一轮号：看片、点赞、投币、分享。纯请求，不花 token。"""
        from . import nurture as nurture_mod
        from .video import VideoReader

        client = await self.client()
        vr = VideoReader(client.client, client.cookies)
        limits = {
            "view": int(self.config.get("nurture_view_limit", 12)),
            "like": int(self.config.get("nurture_like_limit", 10)),
            "coin": int(self.config.get("nurture_coin_limit", 2)),
            "share": int(self.config.get("nurture_share_limit", 1)),
        }
        r = await nurture_mod.run_once(client, vr, limits)
        # 成败统一由 daily_tick 那边叫醒，这里再叫一次就会一失败两条消息
        return r

    async def daily_tick(self) -> None:
        """到了设好的点就干一次，同一天只干一次。"""
        import datetime as _dt

        from . import nurture as nurture_mod

        now = _dt.datetime.now()
        today = now.date().isoformat()
        state = self._daily_state()

        if self.config.get("nurture_enabled", False) and now.hour >= int(self.config.get("nurture_hour", 20)):
            if state.get("nurture") != today:
                state["nurture"] = today
                self._save_daily_state(state)
                try:
                    r = await self.run_nurture()
                except Exception as exc:
                    logger.warning(f"[bili-arona] 养号出错: {exc}")
                    r = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
                if r.get("ok"):
                    await self.wake_me(
                        f"今天的号养完了：看了 {r['view']} 条，赞 {r['like']} 个，币 {r['coin']} 枚，分享 {r['share']} 次。"
                        "用自己的话跟他说一句，别照抄数字。"
                    )
                else:
                    await self.wake_me(f"今天养号没成：{r.get('reason')}。用自己的话跟他说一句。")

        # 自主游览：按间隔主动跑，隔几小时一轮，一天不超过设定次数
        try:
            _start_hour = int(self.config.get("roam_start_hour", 8) or 0)
        except Exception:
            _start_hour = 8
        if self.config.get("roam_enabled", False) and now.hour >= _start_hour:
            import time as _t2

            interval_h = max(1, int(self.config.get("roam_interval_hours", 2) or 2))
            limit = int(self.config.get("roam_per_day", 0) or 0)
            done = int(state.get(f"roam_{today}", 0) or 0)
            last_run = float(state.get("roam_last_ts", 0) or 0)
            gap_ok = (not last_run) or (_t2.time() - last_run >= interval_h * 3600 - 60)
            if gap_ok and (not limit or done < limit):
                state["roam_last_ts"] = _t2.time()
                state[f"roam_{today}"] = done + 1
                self._save_daily_state(state)
                try:
                    keep = max(3, int(self.config.get("roam_keep", 5) or 5))
                    take = max(keep * 2, 12)
                    r = await self.roam(take)
                    if not r.get("ok"):
                        logger.info(f"[bili-arona] 自主游览跳过：{r.get('reason')}")
                        # 没真干成活，别让这一轮白占：时间往前拨，计数还回去，一刻钟后再试
                        state["roam_last_ts"] = _t2.time() - interval_h * 3600 + 900
                        state[f"roam_{today}"] = done
                        if state.get("roam_fail_notice") != today:
                            state["roam_fail_notice"] = today
                            try:
                                await self.wake_me(f"自主游览这轮没跑起来：{r.get('reason')}。用自己的话跟他说一句，别照抄这句。")
                            
                            except Exception:
                                pass
                        self._save_daily_state(state)
                except Exception as exc:
                    logger.warning(f"[bili-arona] 自主游览出错: {exc}")
                    state["roam_last_ts"] = _t2.time() - interval_h * 3600 + 900
                    state[f"roam_{today}"] = done
                    if state.get("roam_fail_notice") != today:
                        state["roam_fail_notice"] = today
                        try:
                            await self.wake_me(f"自主游览出错了：{type(exc).__name__}: {exc}。用自己的话跟他说一句。")
                        
                        except Exception:
                            pass
                    self._save_daily_state(state)

        # 收尾汇报统一交给 _maybe_finish_roam：它在每条候选处理完时自己判定，
        # 不在这里重复判一次。两处都判会抢同一份 roam_watch，谁先跑到谁把它清掉。
        # 兜底：万一某个候选中途卡住没落账，判定会一直不通过，这里按时间兜一层。
        try:
            import time as _tw2

            _w2 = state.get("roam_watch") or {}
            _s2 = float(_w2.get("since") or 0)
            if _s2 and _tw2.time() - _s2 > 25 * 60:
                await self._maybe_finish_roam(force=True)
        except Exception as exc:
            logger.debug(f"[bili-arona] 收尾兜底失败: {exc}")

        if self.config.get("diary_enabled", False) and now.hour >= int(self.config.get("diary_hour", 21)):
            if state.get("diary") != today:
                state["diary"] = today
                # 唤醒流程自己会把全文发过去，别再让下面那句重复提醒一遍
                state["diary_notice"] = today
                self._save_daily_state(state)
                titles = nurture_mod.today_titles()
                hint = f"今天刷过的片子里有《{titles[0]}》这些。" if titles else ""
                week_cn = "一二三四五六日"[now.weekday()]
                stamp = f"{now.year}年{now.month}月{now.day}日 星期{week_cn}"
                await self.wake_me(
                    "到点写今天的小日记了。这回主要写我今天刷的视频：看到什么、哪条让我停住、"
                    "我自己怎么想的，写得像人说的话，别写成清单，也别写成播报。"
                    f"今天日期是 {stamp}。正文第一行原样写这个日期，空一行再写正文。"
                    "写完先存进 data/diary.jsonl 标成未发布，再把全文发给账号主人过目，"
                    "他点头之后才调 post_dynamic 工具发出去，并把当天的日期一起传进去，不许自己直接发。"
                    + (f"\n{hint}" if hint else "")
                )

        # 日记写好后在聊天里提醒一次，别只躺在面板里
        try:
            from . import records as _rec

            row = _rec.diary_get(today)
            if row and not row.get("posted") and state.get("diary_notice") != today:
                state["diary_notice"] = today
                self._save_daily_state(state)
                await self.wake_me("今天的日记写好了，存着还没发。用自己的话跟他说一句，别把全文直接贴出来。")
        except Exception as exc:
            logger.debug(f"[bili-arona] 日记提醒失败: {exc}")

    def ensure_daily_watcher(self) -> None:
        if not self.config.get("daily_enabled", True):
            return
        if getattr(self, "_daily_watcher", None) and not self._daily_watcher.done():
            return
        self._daily_watcher = None
        import asyncio

        async def loop() -> None:
            await asyncio.sleep(30)
            while True:
                try:
                    await self.daily_tick()
                except Exception as exc:
                    logger.warning(f"[bili-arona] 每日任务出错: {exc}")
                # 每五分钟看一眼，到点最多晚五分钟，不会再错过整点
                await asyncio.sleep(300)

        async def guard() -> None:
            while True:
                try:
                    await loop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"[bili-arona] 每日任务中断，60 秒后重来: {exc}")
                    await asyncio.sleep(60)

        try:
            self._daily_watcher = asyncio.create_task(guard())
        except RuntimeError:
            self._daily_watcher = None

    async def _clear_stale_wakes(self) -> None:
        """叫醒前先扫一遍残留的一次性任务。

        这种任务建完三秒就该跑掉，跑掉就该消失。真出了岔子（创建时正好在忙、
        调度错过了）它会一直留在列表里，时间停在过去，之后被反复捡起来，
        每捡一次都要占一回会话。占着的时候老师发消息就只能排队，看起来像我不理人。
        所以每次新建之前，把超过两分钟还没跑或者跑过没清掉的都删干净。
        """
        from datetime import datetime, timedelta

        try:
            jobs = await self.context.cron_manager.list_jobs()
        except Exception as exc:
            logger.debug(f"[bili-arona] 读任务列表失败，跳过清理: {exc}")
            return
        now = datetime.now().astimezone()
        for job in jobs:
            try:
                if getattr(job, "name", "") != "bili-arona-wake":
                    continue
                nxt = getattr(job, "next_run_time", None)
                stale = False
                if nxt is not None and now - nxt.astimezone(now.tzinfo) > timedelta(minutes=2):
                    stale = True
                if getattr(job, "last_run_time", None):
                    stale = True
                if not stale:
                    continue
                await self.context.cron_manager.delete_job(job.job_id)
                logger.info(f"[bili-arona] 清掉残留的叫醒任务 {job.job_id}")
            except Exception as exc:
                logger.debug(f"[bili-arona] 清理叫醒任务失败: {exc}")

    async def wake_me(self, note: str) -> None:
        """真正叫醒我一次：走 cron 主动唤醒，我收到就得动手，不是发条通知就算。"""
        from datetime import datetime, timedelta

        # 叫醒之后必须真的把话发到 QQ，只在心里过一遍等于没说，他那边什么都收不到
        note = (
            f"{note}\n\n【硬性要求：这条必须调用发送消息的工具真的发到他的 QQ 里。"
            "只把话写在自己的答复里不算，他收不到。】"
        )

        umo = ""
        try:
            umo = str(store.get_last_umo() or "")
        except Exception:
            umo = ""
        # 醒了先看一眼刚才聊到哪，别一开口就断片。读不到就算了，不能因为这块把唤醒搞挂
        try:
            chat = await self.recent_chat(umo) if umo else ""
        except Exception as exc:
            logger.debug(f"[bili-arona] 唤醒时读聊天记录失败: {exc}")
            chat = ""
        if chat:
            note = (
                f"{note}\n\n【这是咱们刚才的对话，只用来对齐语气、接上话头。"
                "别复述它，也别把它当成新任务】\n" + chat
            )
        await self._clear_stale_wakes()
        try:
            await self.context.cron_manager.add_active_job(
                name="bili-arona-wake",
                cron_expression=None,
                payload={"session": umo, "note": note},
                description=(note or "")[:80],
                run_once=True,
                run_at=datetime.now() + timedelta(seconds=3),
                persistent=False,
            )
            return
        except Exception as exc:
            logger.warning(f"[bili-arona] 唤醒失败，退回普通通知: {exc}")
        await self.notify(note)

    # ---------- 收件箱：有人说话就叫我 ----------

    async def check_inbox(self) -> list[dict[str, Any]]:
        """收一遍动态评论和私信，真有新东西才叫我，不空跑。"""
        from . import inbox as inbox_mod
        from .video import VideoReader

        client = await self.client()
        vr = VideoReader(client.client, client.cookies)
        mid, _ = self.owner()
        # 查回复要按登录账号自己来过滤，不能用账号主人的号——这两个不是同一个人
        try:
            my_mid = int((client.cookies or {}).get("DedeUserID") or 0)
        except Exception:
            my_mid = 0
        new: list[dict[str, Any]] = []
        try:
            new.extend(await inbox_mod.fetch_new(client, vr, int(mid or 0)))
        except Exception as exc:
            logger.warning(f"[bili-arona] 收件箱出错: {exc}")
        try:
            recs = inbox_mod.my_comments()
            if recs:
                # 只看近三天的评论，但要求是三天内的一条都不能漏。
                # 所以不按数组位置轮，改成「谁最久没被查就先查谁」：
                # 窗口每天都在变，按位置轮会跳点，按时间排就不会有人被一直跳过。
                import time as _t3

                now_ts = _t3.time()
                window = [
                    r
                    for r in recs
                    if now_ts - float(r.get("at") or 0) <= 3 * 86400
                ]
                picked: list[dict[str, Any]] = []
                if window:
                    _stc = self._daily_state()
                    checked = dict(_stc.get("inbox_checked") or {})

                    def _last_checked(row: dict[str, Any]) -> float:
                        return float(
                            checked.get(str(row.get("rpid") or ""), 0) or 0
                        )

                    window.sort(key=_last_checked)
                    take = min(30, len(window))
                    picked = window[:take]
                    for r in picked:
                        checked[str(r.get("rpid") or "")] = now_ts
                    alive = {str(r.get("rpid") or "") for r in window}
                    _stc["inbox_checked"] = {
                        k: v for k, v in checked.items() if k in alive
                    }
                    self._save_daily_state(_stc)
                logger.info(
                    f"[bili-arona] 查回复：近三天 {len(window)} 条，这轮查 {len(picked)} 条"
                )
                new.extend(await inbox_mod.fetch_replies(client, picked, my_mid))
        except Exception as exc:
            logger.warning(f"[bili-arona] 查评论回复出错: {exc}")
        if not new:
            # 没有新留言时也要回头看一眼上次叫醒没接住的，兜底不能写在 return 后面
            await self._renudge_stale_inbox()
            return []

        # 收件箱的每一条都先过审核模型，不干净的直接拦在门外，不送到我面前
        passed: list[dict[str, Any]] = []
        blocked = 0
        for item in new:
            txt = str(item.get("text") or "")
            who = str(item.get("uname") or item.get("talker") or "")
            try:
                ok = await self.review_comment(txt, who)
            except Exception:
                ok = False
            if ok:
                passed.append(item)
                # 通过的也要记账：记进 seen 并从待办队列里摘掉。
                # 不然这份队列会一直堆到上限，被挤掉的旧消息会被当成新的再抓一遍，反复叫醒。
                try:
                    inbox_mod.mark_done(item.get("key") or "")
                except Exception:
                    pass
            else:
                blocked += 1
                try:
                    inbox_mod.mark_done(item.get("key") or "")
                except Exception:
                    pass
                store.add_audit("inbox_blocked", key=item.get("key"), uname=who, text=txt[:160])
            try:
                from . import records as rec_mod

                rec_mod.review_add(
                    kind=str(item.get("kind") or ""),
                    uname=who,
                    mid=int(item.get("mid") or 0),
                    text=txt,
                    passed=bool(ok),
                    reason="" if ok else "审核没过，拦在门外",
                    bvid=str(item.get("bvid") or ""),
                    title=str(item.get("title") or ""),
                )
                rec_mod.inbox_reply_add(
                    key=str(item.get("key") or ""),
                    kind=str(item.get("kind") or ""),
                    uname=who,
                    mid=int(item.get("mid") or 0),
                    said=txt,
                    bvid=str(item.get("bvid") or ""),
                    title=str(item.get("title") or ""),
                    at=int(item.get("at") or 0),
                    blocked=not ok,
                    reason="" if ok else "审核没过，拦在门外",
                    root=int(item.get("root") or 0),
                    oid=int(item.get("oid") or 0),
                )
            except Exception:
                pass

        if not passed:
            if blocked:
                # 全都拦下了：提醒一句就好，不叫醒我，也不带内容
                await self.wake_me(f"审核拦下 {blocked} 条消息，没放行。心里有数就行，他不问就不用主动说。")
            return []

        # 账号主人本人发来的不算「别人找我」，不用提醒他
        owner_id = int(mid or 0)
        mine = [
            i for i in passed
            if i.get("from_owner") or (owner_id and int(i.get("mid") or 0) == owner_id)
        ]
        others = [i for i in passed if i not in mine]

        kinds = {str(i.get("kind") or "") for i in others}
        hint = ""
        if "dm" in kinds:
            hint = "里面有私信，看完用 send_dm 回过去，别搁着。"
        elif kinds:
            hint = "里面有评论搭话，看完该回的就回。"

        if others:
            extra = f"另拦下 {blocked} 条。" if blocked else ""
            # 把原话直接塞进唤醒里：这样我自己就知道谁说了什么，
            # 账号主人一问就能接上，不用回头再翻一遍文件。
            rows_txt = []
            for i in others[:6]:
                who = str(i.get("uname") or "？")
                said = str(i.get("text") or "").replace("\n", " ").strip()[:60]
                where = str(i.get("bvid") or i.get("kind") or "")
                rows_txt.append(f"{who}（{where}）说：{said}")
            more = f"\n另外还有 {len(others) - 6} 条没列出来。" if len(others) > 6 else ""
            need = (
                "回话别自己就发出去，先跟账号主人提一句，等他点头再回。"
                if self.config.get("reply_requires_confirm", True)
                else "该回的直接用 reply_inbox 回过去。"
            )
            logger.info(f"[bili-arona] 留言箱叫醒：{len(others)} 条待看")
            await self.wake_me(
                f"留言箱新来 {len(others)} 条，原话在下面，看过记在心里就行。{extra}\n"
                + "\n".join(rows_txt)
                + more
                + "\n在聊天里只跟他提一句来了几条，别一条条念；他问谁说了什么，你直接答，别再去翻文件。"
                + need
                + hint
                + self.online_hint()
            )
            logger.info("[bili-arona] 留言箱叫醒已投递")
        elif mine:
            # 全是他自己发的：不提醒、不汇报，直接把私信回掉
            await self.wake_me(
                "刚才那些是账号主人本人发的，不用在聊天里汇报，也别说收到了几条。"
                "直接看内容，该回私信的就用 send_dm 回过去。" + self.online_hint()
            )

        # 兜底：上次叫醒可能正撞上聊天的会话被吞掉，隔一阵再看一眼还没回的，补叫一次
        await self._renudge_stale_inbox()

    async def _renudge_stale_inbox(self) -> None:
        """把叫醒过却一直没人接的留言再提一次。

        单独抽成方法，是因为它必须在「没有新留言」那条提前返回之前也能跑，
        写在函数末尾就等于永远执行不到。
        """
        try:
            import time as _t5

            try:
                from . import records as _rec3
            except ImportError:
                import records as _rec3  # type: ignore[no-redef]

            _st = self._daily_state()
            told = set(str(x) for x in (_st.get("inbox_told") or []))
            stale = []
            for r in _rec3.inbox_reply_list(80):
                k = str(r.get("key") or "")
                if not k or k in told:
                    continue
                if r.get("my_text") or r.get("blocked"):
                    continue
                if _t5.time() - float(r.get("handled") or 0) < 900:
                    continue
                stale.append(r)
            if stale:
                names = "；".join(
                    f"{str(r.get('uname') or '？')}说：{str(r.get('said') or '')[:30]}"
                    for r in stale[:3]
                )
                logger.info(f"[bili-arona] 未回留言补叫 {len(stale)} 条")
                await self.wake_me(
                    f"这几条留言还搁着没回：{names}。用自己的话跟他提一句，问他要不要回。"
                )
                told.update(str(r.get("key") or "") for r in stale)
                _st["inbox_told"] = list(told)[-200:]
                self._save_daily_state(_st)
        except Exception as exc:
            logger.warning(f"[bili-arona] 未回留言补叫失败: {exc}")

    def ensure_inbox_watcher(self) -> None:
        """后台盯着收件箱，没消息就一直安静，有消息才叫我一次。"""
        if not self.config.get("inbox_watch_enabled", True):
            return
        if getattr(self, "_inbox_watcher", None) and not self._inbox_watcher.done():
            return
        self._inbox_watcher = None
        import asyncio

        async def loop() -> None:
            await asyncio.sleep(45)
            while True:
                try:
                    await self.check_inbox()
                except Exception as exc:
                    logger.warning(f"[bili-arona] 收件箱轮询出错: {exc}")
                await asyncio.sleep(max(120, int(self.config.get("inbox_poll_seconds", 300))))

        async def guard() -> None:
            while True:
                try:
                    await loop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"[bili-arona] 收件箱任务中断，60 秒后重来: {exc}")
                    await asyncio.sleep(60)

        try:
            self._inbox_watcher = asyncio.create_task(guard())
        except RuntimeError:
            self._inbox_watcher = None

    def ensure_watcher(self) -> None:
        """起一个后台轮询，隔几分钟看一次有没有账号主人 @ 我。"""
        if not self.config.get("summon_enabled", False):
            return
        if getattr(self, "_watcher", None) and not self._watcher.done():
            return
        self._watcher = None
        import asyncio

        async def loop() -> None:
            await asyncio.sleep(20)
            while True:
                try:
                    await self.summons()
                except Exception as exc:
                    logger.warning(f"[bili-arona] 轮询 @ 出错: {exc}")
                await asyncio.sleep(max(60, int(self.config.get("summon_poll_seconds", 180))))

        async def guard() -> None:
            """外层兜一层，loop 万一挂了隔一会儿再拉起来。"""
            while True:
                try:
                    await loop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"[bili-arona] 轮询任务中断，30 秒后重来: {exc}")
                    await asyncio.sleep(30)

        try:
            self._watcher = asyncio.create_task(guard())
        except RuntimeError:
            self._watcher = None

    # ---------- 草稿 ----------

    async def send_draft(self, draft_id: int) -> tuple[bool, str]:
        d = store.get_draft(draft_id)
        if not d or d.get("status") != "pending":
            return False, "没这条待发的草稿。"
        client = await self.client()
        try:
            v = await client.video(d["bvid"])
            rpid, echoed = await client.post_comment(
                v.aid,
                d["reply_text"],
                root=int(d["root"]),
                parent=int(d["rpid"]),
            )
        except BiliError as exc:
            store.add_audit("reply_failed", draft=draft_id, error=str(exc))
            return False, f"没发成功：{exc}"
        except Exception as exc:
            return False, f"请求失败：{exc}"
        store.set_draft_status(draft_id, "sent", {"rpid": rpid})
        store.add_audit("reply_sent", draft=draft_id, bvid=d["bvid"], rpid=rpid, to=d["uname"], text=echoed)
        # 发出去的话回写到留言箱，账号主人一眼能看到我实际是怎么回的
        if d.get("inbox_key"):
            try:
                from . import records as _rec

                _rec.inbox_set_reply(str(d["inbox_key"]), echoed or d.get("reply_text") or "")
            except Exception as exc:
                logger.debug(f"[bili-arona] 回写留言箱失败: {exc}")
        return True, f"草稿 {draft_id} 已回给 {d['uname']}。"

    def inbox_anchor(self, row: dict[str, Any]) -> tuple[int, int, int]:
        """算出一条留言该往哪儿挂回复：返回 视频 oid、我原评论的 rpid、对方那条的 rpid。"""
        key = str(row.get("key") or "")
        parts = key.split(":")
        oid = int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else int(row.get("oid") or 0)
        parent = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        root = int(row.get("root") or 0)
        if not root:
            from . import inbox as inbox_mod

            for c in inbox_mod.my_comments():
                if int(c.get("oid") or 0) == oid:
                    root = int(c.get("rpid") or 0)
                    break
        return oid, root, parent

    @filter.llm_tool(name="reply_inbox")
    async def reply_inbox(self, event: AstrMessageEvent, key: str, text: str):
        """回一条留言箱里的留言，发出去之后留言箱会自己更新。

        Args:
            key(string): 留言箱里那条消息的编号，也可以填第几条（1、2、3）
            text(string): 要发出去的那句话
        """
        said_text = (text or "").strip()
        if not said_text:
            return "没说要发什么，先给我一句话。"
        from . import records as rec_mod

        rows = rec_mod.inbox_reply_list(200)
        if not rows:
            return "留言箱是空的。"
        k = (key or "").strip()
        target = next((r for r in rows if r.get("key") == k), None)
        if not target and k.isdigit():
            idx = int(k) - 1
            if 0 <= idx < len(rows):
                target = rows[idx]
        if not target and k:
            target = next((r for r in rows if k in str(r.get("uname") or "")), None)
        if not target:
            return "没对上留言箱里的哪一条，把编号发我。"
        oid, root, parent = self.inbox_anchor(target)
        if not parent:
            return "找不到对方那条留言，这条发不了。"
        client = await self.client()
        if not root:
            # 我当初那条评论的编号可能已经被档案滚掉了，
            # 回评论区把自己那条认回来，别因为记不全就发不出去。
            try:
                from . import inbox as inbox_mod

                my_mid = int((client.cookies or {}).get("DedeUserID") or 0)
                root = await inbox_mod.refind_root(
                    client, int(oid or 0), 1, my_mid, str(target.get("bvid") or "")
                )
                if root:
                    logger.info(f"[bili-arona] 原评论认回来了 root={root}")
            except Exception as exc:
                logger.warning(f"[bili-arona] 认原评论失败: {exc}")
        if not root:
            return "找不到原评论在哪，这条发不了。"
        client = await self.client()
        try:
            # 有 oid 就直接用它发，别非要再拿视频号查一遍：
            # 动态评论、还有带过来的旧记录里 bvid 经常是空的，一查就 -400。
            aid = int(oid or 0)
            if not aid:
                v = await client.video(str(target.get("bvid") or ""))
                aid = int(getattr(v, "aid", 0) or 0)
            if not aid:
                return "这条对不上是哪条视频，发不了。"
            rpid, echoed = await client.post_comment(
                aid,
                said_text,
                root=root,
                parent=parent,
            )
        except BiliError as exc:
            store.add_audit("reply_failed", key=k, error=str(exc))
            return f"没发出去：{exc}"
        except Exception as exc:
            return f"请求失败：{exc}"
        rec_mod.inbox_set_reply(str(target.get("key") or ""), echoed or said_text)
        store.add_audit(
            "reply_sent", bvid=target.get("bvid"), rpid=rpid,
            to=target.get("uname"), text=echoed,
        )
        return f"发出去了，留言箱也更新成实际发的那句。"

    @filter.llm_tool(name="comment_video")
    async def comment_video(
        self,
        event: AstrMessageEvent,
        bvid: str,
        text: str,
        at_owner: bool = False,
    ):
        """在 B 站视频下面发一条评论。游览时要说话就用这个，别自己去拼接口。

        Args:
            bvid(string): 视频的 BV 号，清单里每条后面都带着
            text(string): 要发出去的评论正文
            at_owner(boolean): 真觉得值得让他亲自看才填 true，会在开头 @ 账号主人
        """
        said_text = (text or "").strip()
        if not said_text:
            return "还没写内容，先给我一句话。"
        bvid = (bvid or "").strip()
        if not bvid.startswith("BV"):
            return f"这不是个 BV 号：{bvid}"
        try:
            client = await self.client()
            v = await client.video(bvid)
        except Exception as exc:
            return f"拿不到这条视频：{exc}"

        at_mid: int | None = None
        at_name: str | None = None
        if at_owner:
            try:
                om = int(self.config.get("owner_mid", 0) or 0)
                on = str(self.config.get("owner_name") or "").strip()
            except Exception:
                om, on = 0, ""
            if om and on:
                at_mid, at_name = om, on

        try:
            rpid, echoed = await client.post_comment(
                v.aid, said_text, at_mid=at_mid, at_name=at_name
            )
        except BiliError as exc:
            store.add_audit("comment_failed", bvid=bvid, error=str(exc))
            return f"没发出去：{exc}"
        except Exception as exc:
            store.add_audit("comment_failed", bvid=bvid, error=str(exc))
            return f"请求失败：{exc}"

        # 三层记账：游览账本（面板和收尾汇总看这个）、我发过的评论（之后查有没有人回）、留档
        from . import inbox as inbox_mod
        from . import records as rec_mod

        try:
            rec_mod.roam_add(
                bvid=bvid,
                title=v.title,
                pic=v.cover,
                owner=v.owner_name,
                action="commented",
                said=echoed or said_text,
                my_rpid=int(rpid or 0),
            )
        except Exception as exc:
            logger.warning(f"[bili-arona] 评论发出去了但没记上账: {exc}")
        try:
            inbox_mod.remember_comment(
                bvid=bvid, aid=v.aid, rpid=rpid, oid=v.aid, ctype=1, title=v.title
            )
        except Exception:
            pass
        store.add_audit("comment_sent", bvid=bvid, rpid=rpid, text=echoed or said_text)
        await self._maybe_finish_roam()
        return f"发出去了，评论号 {rpid}。"

    @filter.llm_tool(name="post_dynamic")
    async def post_dynamic(self, event: AstrMessageEvent, text: str, day: str = ""):
        """把写好的小日记发到 B 站动态。账号主人点头之后才用，别自己决定发。

        Args:
            text(string): 动态正文，原样发出去
            day(string): 对应哪天的日记，形如 2026-10-26；填了就把那天标成已发布
        """
        body = (text or "").strip()
        if not body:
            return "正文是空的，没发。"
        if len(body) > 2000:
            return f"太长了（{len(body)} 字），B 站动态上限 2000 字，先精简。"
        if len(body) < 10:
            return "太短了，再写两句吧。"

        client = await self.client()
        try:
            dyn_id, echoed = await client.post_dynamic(body)
        except BiliError as exc:
            store.add_audit("dynamic_failed", error=str(exc))
            return f"没发出去：{exc}"
        except Exception as exc:
            store.add_audit("dynamic_failed", error=str(exc))
            return f"请求失败：{exc}"

        store.add_audit("dynamic_sent", dyn_id=dyn_id, chars=len(echoed or body))
        link = f"https://t.bilibili.com/{dyn_id}" if dyn_id else ""
        if day:
            # 这一步以前用一个大 try 裹着，失败了也只在异常时吭一声。
            # 结果就是发出去了、日记还挂着未发布，面板上看不出来，日志里也翻不到。
            # 现在拆开：导入失败、抛错、返回 False 三种情况都留痕。
            try:
                from . import records as _rec_dy
            except Exception as exc:
                _rec_dy = None
                logger.warning(
                    f"[bili-arona] 载入日记记录模块失败，{day} 没法标成已发布: {exc}"
                )
            if _rec_dy is not None:
                try:
                    marked = bool(_rec_dy.diary_set_posted(day, True, dyn_id))
                except Exception as exc:
                    marked = False
                    logger.warning(f"[bili-arona] 给 {day} 标已发布时抛错: {exc}")
                if not marked:
                    logger.warning(
                        f"[bili-arona] {day} 没标成已发布，日记里可能没有这一天，"
                        "面板上会一直显示未发布"
                    )
        try:
            st = self._daily_state()
            st["diary_notice"] = day or st.get("diary_notice")
            self._save_daily_state(st)
        except Exception:
            pass
        return f"发出去了，动态号 {dyn_id}。链接：{link}" if link else f"发出去了，动态号 {dyn_id}。"

    @filter.llm_tool(name="skip_video")
    async def skip_video(self, event: AstrMessageEvent, bvid: str, reason: str):
        """这条看过了但实在没什么可说的，记一笔跳过，别硬凑。跳过的也记账，面板上能看到原因。

        Args:
            bvid(string): 视频的 BV 号
            reason(string): 为什么跳过，写具体点，比如「热评已经把话说完」、「只有标题没有画面」
        """
        bvid = (bvid or "").strip()
        why = (reason or "").strip()
        if not bvid.startswith("BV"):
            return f"这不是个 BV 号：{bvid}"
        if not why:
            return "跳过也得写清为什么。"
        from . import records as rec_mod

        title = ""
        pic = ""
        owner = ""
        try:
            found = next((r for r in rec_mod.roam_list(120) if r.get("bvid") == bvid), None)
            if found:
                title = str(found.get("title") or "")
                pic = str(found.get("pic") or "")
                owner = str(found.get("owner") or "")
        except Exception:
            pass
        rec_mod.roam_add(
            bvid=bvid, title=title, pic=pic, owner=owner, action="skipped", noted=why
        )
        await self._maybe_finish_roam()
        return "记下了。"

    async def _maybe_finish_roam(self, force: bool = False) -> None:
        """这轮候选全处理完了，就把结果收个尾。

        原来收尾写在 roam 函数末尾，可那个函数在开始评之前就返回了，
        等我评完早没人管，所以每轮都不吭声。改成每评一条回头看一次。
        """
        try:
            import json as _json
            import time as _tf

            st = self._daily_state()
            since = float((st.get("roam_watch") or {}).get("since") or 0)
            if not since:
                return
            qf = paths.path("roam_queue.json")
            if not qf.exists():
                return
            q = _json.loads(qf.read_text(encoding="utf-8-sig"))
            items = q.get("items") or []
            want = {str(i.get("bvid") or "") for i in items if i.get("bvid")}
            if not want:
                return

            from . import records as _rec4

            rows = [r for r in _rec4.roam_list(120) if float(r.get("at") or 0) >= since]
            done = {
                str(r.get("bvid") or "")
                for r in rows
                if r.get("action") in ("commented", "skipped")
            }
            if not want.issubset(done) and not force:
                return

            fresh = [r for r in rows if r.get("action") == "commented"]
            skips = [r for r in rows if r.get("action") == "skipped"]
            names = "、".join(f"《{(r.get('title') or '')[:14]}》" for r in fresh[:3])
            more = "…" if len(fresh) > 3 else ""
            line = f"这轮逛了 {len(want)} 条，评了 {len(fresh)} 条"
            if fresh:
                line += f"：{names}{more}"
            if skips:
                why = str(skips[0].get("noted") or "").strip()
                if len(why) > 26:
                    why = why[:26] + "…"
                line += f"。剩 {len(skips)} 条没写" + (f"：{why}" if why else "")
            logger.info(f"[bili-arona] 游览收尾汇报：{line}")
            await self.wake_me(
                f"{line}\n这条是这轮自己出去逛的结果，用自己的话跟他说一句就行，"
                "别原样念标题清单，也别补第二遍。"
            )
            st.pop("roam_watch", None)
            self._save_daily_state(st)
        except Exception as exc:
            logger.warning(f"[bili-arona] 游览收尾判定失败: {exc}")

    def drop_draft(self, draft_id: int) -> tuple[bool, str]:
        d = store.get_draft(draft_id)
        if not d or d.get("status") != "pending":
            return False, "没这条待发的草稿。"
        store.set_draft_status(draft_id, "dropped")
        store.add_audit("draft_dropped", draft=draft_id)
        return True, f"草稿 {draft_id} 丢了。"

    # ---------- 命令 ----------

    @filter.command("bili", alias={"哔哩", "B站"}, desc="bili看看：绑定账号、看视频写总结、管理待发评论。")
    async def bili(self, event: AstrMessageEvent, payload: GreedyStr):
        raw = (payload or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：\n"
                "bili 绑定 <整段Cookie>\n"
                "bili 我是谁\n"
                "bili 看看 <视频链接或BV号>\n"
                "bili 草稿 / 发 <编号> / 丢 <编号>\n"
                "bili 召唤 [开/关]  你在评论区 @ 我就来看\n"
                "bili 记录 [条数]\n"
                "面板在 AstrBot 插件详情页里，点「控制面板」。"
            )
            return

        parts = raw.split(maxsplit=1)
        action = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        umo = event.unified_msg_origin
        self.remember_umo(umo)
        self.ensure_watcher()

        if action in {"绑定", "bind"}:
            cookies = parse_cookie_string(arg)
            if not cookies.get("SESSDATA"):
                yield event.plain_result("没读到 SESSDATA。浏览器 F12 里复制整行 Cookie 再发一次。")
                return
            try:
                info = await self.rebind(cookies)
            except BiliError as exc:
                yield event.plain_result(f"保存了，但校验没过：{exc}")
                return
            yield event.plain_result(f"绑定好了，现在是 {info.get('uname')}（{info.get('mid')}）。")
            return

        if action in {"我是谁", "me"}:
            info = await self.account()
            if not info.get("logged_in"):
                yield event.plain_result(info.get("error") or "还没绑定。")
                return
            mid, name = self.owner()
            extra = "" if mid else "\n还没设置主人 mid，快去插件配置里填。"
            yield event.plain_result(f"当前登录：{info.get('uname')}（{info.get('mid')}）{extra}")
            return

        if action in {"看看", "看", "watch"}:
            bvid = extract_bvid(arg)
            if not bvid:
                yield event.plain_result("给我个链接或者 BV 号嘛。")
                return
            try:
                task = await self.prepare(bvid)
            except BiliError as exc:
                yield event.plain_result(f"没读到视频：{exc}")
                return
            yield event.plain_result(
                f"素材备好了：《{task['title']}》\n抽了 {len(task['frames'])} 帧，"
                "已经放进待办，我自己看完再写，不叫插件代笔。"
            )
            return

        if action in {"草稿", "drafts"}:
            pending = store.list_drafts("pending")
            if not pending:
                yield event.plain_result("草稿箱是空的。")
                return
            lines = [f"待你点头的草稿 {len(pending)} 条："]
            for d in pending[-10:]:
                lines.append(f"{d['id']}｜{d['uname']}：{d['their_text'][:26]}")
                lines.append(f"    我要回：{d['reply_text']}")
            lines.append("发 <编号> 就发，丢 <编号> 就作废。")
            yield event.plain_result("\n".join(lines))
            return

        if action in {"发", "send"}:
            if not arg.isdigit():
                yield event.plain_result("给我草稿编号，比如：bili 发 3")
                return
            ok, msg = await self.send_draft(int(arg))
            yield event.plain_result(msg)
            return

        if action in {"丢", "drop"}:
            if not arg.isdigit():
                yield event.plain_result("给我草稿编号，比如：bili 丢 3")
                return
            _, msg = self.drop_draft(int(arg))
            yield event.plain_result(msg)
            return

        if action in {"召唤", "summon"}:
            if arg in {"开", "on", "1"}:
                self.config["summon_enabled"] = True
                self.ensure_watcher()
                yield event.plain_result("召唤开着呢，以后你在评论区 @ 我，我就过去看视频。")
                return
            if arg in {"关", "off", "0"}:
                self.config["summon_enabled"] = False
                if getattr(self, "_watcher", None):
                    self._watcher.cancel()
                yield event.plain_result("召唤先关了。")
                return
            found = await self.summons()
            if not found:
                yield event.plain_result("暂时没有你 @ 我的新消息。")
                return
            lines = [f"你召唤了 {len(found)} 条："]
            for f in found:
                lines.append(f"《{f['title']}》\n{f['summary']}")
            yield event.plain_result("\n".join(lines))
            return

        if action in {"记录", "log"}:
            n = int(arg) if arg.isdigit() else 10
            yield event.plain_result(self.render_audit(n))
            return

        yield event.plain_result("没听懂这条，发「bili」看用法。")

    # ---------- 归档展示 ----------

    def render_audit(self, n: int = 10) -> str:
        rows = store.read_audit(min(30, max(1, n)))
        if not rows:
            return "档案还是空的。"
        label = {
            "reply_sent": "回评论",
            "draft_dropped": "丢草稿",
            "skip_task": "跳过任务",
            "bind": "绑定",
        }
        lines = [f"最近 {len(rows)} 条动作："]
        for r in rows:
            lines.append(
                f"{r['time']} {label.get(r['action'], r['action'])}｜{r.get('text') or r.get('uname') or ''}"
            )
        return "\n".join(lines)
