"""把视频真正看一遍：播放地址、字幕、弹幕、抽帧拼图。

没有 ffmpeg 也能跑，抽帧用 OpenCV 读下载下来的 mp4；
拼图用 PIL 均匀排网格，一张图交给多模态模型就能看全片走势。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import tempfile
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import logger  # 用 AstrBot 统一的日志器，插件规范要求

# 语音转写缺依赖时只提醒一次，别每条视频刷一遍
_WHISPER_WARNED = False

API = "https://api.bilibili.com"

# 官方 wbi 混音表，用来给 playurl / player 接口签名
MIXIN_KEY_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


@dataclass
class Watched:
    """一次「看视频」抓到的所有素材。"""

    cid: int = 0
    duration: int = 0
    subtitles: str = ""
    danmaku: list[str] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)  # 每帧的 mm:ss
    sheet_b64: str = ""
    notes: list[str] = field(default_factory=list)
    hot: list[str] = field(default_factory=list)  # 热评，按点赞排
    tname: str = ""
    dm_stat: dict[str, Any] = field(default_factory=dict)  # 弹幕统计
    script: list[tuple[float, float, str]] = field(default_factory=list)  # 语音转写


class VideoReader:
    def __init__(self, client: httpx.AsyncClient, cookies: dict[str, str]) -> None:
        self.http = client
        self.cookies = cookies or {}
        self._keys: tuple[str, str] | None = None
        self._keys_at = 0.0
        self._dm_rows: list[tuple[float, str]] = []
        self._wm: Any = None
        self._wm_size: str = ""

    # ---------- wbi 签名 ----------

    async def _wbi_keys(self) -> tuple[str, str]:
        if self._keys and time.time() - self._keys_at < 3600:
            return self._keys
        r = await self.http.get(API + "/x/web-interface/nav")
        data = (r.json() or {}).get("data") or {}
        wbi = data.get("wbi_img") or {}
        img = str(wbi.get("img_url") or "")
        sub = str(wbi.get("sub_url") or "")
        img_key = img.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub.rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("拿不到 wbi 密钥")
        self._keys = (img_key, sub_key)
        self._keys_at = time.time()
        return self._keys

    async def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = await self._wbi_keys()
        raw = img_key + sub_key
        mixin = "".join(raw[i] for i in MIXIN_KEY_TAB)[:32]
        clean = {
            k: re.sub(r"[!'()*]", "", str(v))
            for k, v in params.items()
        }
        clean["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(clean.items()))
        clean["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return clean

    # ---------- 播放地址 ----------

    async def play_url(self, bvid: str, cid: int, qn: int = 32) -> str | None:
        """拿一个 mp4 直链，qn 32 是 480P，够抽帧看内容了。"""
        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": qn,
            "fnval": 1,       # 只要 mp4/flv，不要 dash 分片
            "fnver": 0,
            "fourk": 0,
            "platform": "html5",
            "high_quality": 1,
        }
        signed = await self._sign(params)
        r = await self.http.get(API + "/x/player/wbi/playurl", params=signed)
        data = (r.json() or {}).get("data") or {}
        durl = data.get("durl") or []
        if durl and durl[0].get("url"):
            return durl[0]["url"]
        # 有些清晰度只有 dash，退一步试低清
        if qn > 16:
            return await self.play_url(bvid, cid, 16)
        return None

    async def download(self, url: str, limit_mb: int = 220) -> Path | None:
        """把 mp4 拉到临时目录，超限就放弃。"""
        tmp = Path(tempfile.gettempdir()) / "arona_bili"
        tmp.mkdir(parents=True, exist_ok=True)
        out = tmp / f"v_{int(time.time()*1000)}.mp4"
        total = 0
        try:
            async with self.http.stream(
                "GET", url, headers={"Referer": "https://www.bilibili.com", "Range": "bytes=0-"}
            ) as r:
                r.raise_for_status()
                with out.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 18):
                        total += len(chunk)
                        if total > limit_mb * 1024 * 1024:
                            f.close()
                            out.unlink(missing_ok=True)
                            return None
                        f.write(chunk)
        except Exception:
            out.unlink(missing_ok=True)
            return None
        return out if out.exists() and out.stat().st_size > 4096 else None

    # ---------- 字幕 ----------

    async def subtitles(self, bvid: str, cid: int, aid: int) -> str:
        try:
            signed = await self._sign({"aid": aid, "cid": cid, "bvid": bvid})
            r = await self.http.get(API + "/x/player/wbi/v2", params=signed)
            data = (r.json() or {}).get("data") or {}
            subs = ((data.get("subtitle") or {}).get("subtitles")) or []
            lines: list[str] = []
            for s in subs[:2]:
                url = s.get("subtitle_url") or ""
                if url.startswith("//"):
                    url = "https:" + url
                if not url:
                    continue
                sr = await self.http.get(url)
                body = (sr.json() or {}).get("body") or []
                lines.extend(str(x.get("content") or "").strip() for x in body)
                if len("".join(lines)) > 12000:
                    break

            lines = [x for x in lines if x]
            text = "\n".join(lines)
            if len(text) <= 1200:
                return text

            # 长了就按时间轴均匀抽，保证头尾中间都有，而不是只截开头
            keep = 60
            step = len(lines) / keep
            sampled = [lines[int(i * step)] for i in range(keep)]
            return "\n".join(sampled)[:1200]
        except Exception:
            return ""


    # ---------- 弹幕统计 ----------

    STOP = {
        "这个", "那个", "什么", "怎么", "就是", "没有", "我们", "你们", "他们", "可以",
        "真的", "一个", "不是", "还是", "现在", "时候", "因为", "所以", "已经", "这样",
        "哈哈", "哈哈哈哈", "刚刚", "前排", "第一", "来了", "好看", "牛逼", "厉害",
    }

    @classmethod
    def danmaku_stats(cls, rows: list[tuple[float, str]], duration: int = 0) -> dict[str, Any]:
        """纯本地数数：高频句、高频词、情绪粗判、最热闹的时段。"""
        from collections import Counter

        if not rows:
            return {}
        noise = re.compile(r"^(刚刚|\d+\s*(秒|分钟|小时|天)前|\d+人|\d+在看|打卡|第一)$")
        clean_rows = [(t, x) for t, x in rows if not noise.match(x.strip())]
        if not clean_rows:
            clean_rows = rows
        texts = [x for _, x in clean_rows]
        n = len(texts)

        # 高频整句：同一句话被刷出来的，基本就是全场高能
        rep = [(w, c) for w, c in Counter(texts).most_common(12) if c >= 3][:6]

        # 高频词：中文没有分词就用两字滑窗，去掉常见虚词
        grams: Counter = Counter()
        for t in texts:
            clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", t)
            for i in range(len(clean) - 1):
                g = clean[i : i + 2]
                if g in cls.STOP:
                    continue
                grams[g] += 1
        hot_words = [(w, c) for w, c in grams.most_common(30) if c >= 3][:10]

        # 情绪：不用模型，关键词粗分
        moods = {
            "笑": ["哈", "草", "笑", "乐", "233", "xswl", "绷", "梗"],
            "惊": ["卧槽", "我去", "竟", "居然", "妈呀", "天啊", "！"],
            "感动": ["哭", "泪", "破防", "心疼", "感动", "舍不得"],
            "不满": ["退钱", "烂", "垃圾", "骗", "无语", "就这", "劝退"],
        }
        mood_cnt = {}
        for name, kws in moods.items():
            mood_cnt[name] = sum(1 for t in texts if any(k in t for k in kws))

        # 最热闹的时段：按十二秒分桶
        buckets: Counter = Counter()
        for t, _ in clean_rows:
            buckets[int(t // 12) * 12] += 1
        peaks = []
        for t, c in buckets.most_common(3):
            if c < 3:
                continue
            m, sec = divmod(int(t), 60)
            m2, s2 = divmod(int(t) + 12, 60)
            peaks.append(f"{m}:{sec:02d}-{m2}:{s2:02d}（{c} 条）")

        top_mood = max(mood_cnt.items(), key=lambda kv: kv[1]) if mood_cnt else ("", 0)
        return {
            "total": n,
            "repeated": rep,
            "words": hot_words,
            "moods": {k: v for k, v in mood_cnt.items() if v},
            "top_mood": top_mood[0] if top_mood[1] >= max(3, n // 20) else "",
            "peaks": peaks,
        }

    @classmethod
    def describe_stats(cls, st: dict[str, Any]) -> str:
        if not st:
            return ""
        bits = [f"弹幕共 {st.get('total', 0)} 条"]
        if st.get("peaks"):
            bits.append("最热闹的时段：" + "、".join(st["peaks"][:3]))
        if st.get("repeated"):
            bits.append("被反复刷的整句：" + "、".join(f"{w}({c})" for w, c in st["repeated"][:5]))
        if st.get("words"):
            bits.append("高频词：" + "、".join(f"{w}({c})" for w, c in st["words"][:8]))
        if st.get("moods"):
            ms = "、".join(f"{k} {v} 条" for k, v in sorted(st["moods"].items(), key=lambda kv: -kv[1]))
            bits.append("情绪分布：" + ms)
            if st.get("top_mood"):
                bits.append(f"整体偏{st['top_mood']}")
        return "；".join(bits)


    # ---------- 弹幕 ----------

    async def danmaku(self, cid: int, limit: int = 40) -> list[str]:
        """弹幕按时间轴采样：既有全片分布，也把刷屏最狠的那几句挑出来。"""
        try:
            r = await self.http.get(
                f"https://comment.bilibili.com/{cid}.xml",
                headers={"Referer": "https://www.bilibili.com"},
            )
            raw = r.content
            if raw[:2] == b"\x78\x9c" or raw[:1] == b"\x78":
                raw = zlib.decompress(raw)
            text = raw.decode("utf-8", "replace")

            rows: list[tuple[float, str]] = []
            for p_attr, body_ in re.findall(r'<d p="([^"]*)">(.*?)</d>', text):
                clean = re.sub(r"<[^>]+>", "", body_).strip()
                if not (2 <= len(clean) <= 40):
                    continue
                try:
                    t = float(str(p_attr).split(",")[0])
                except Exception:
                    t = 0.0
                rows.append((t, clean))
            if not rows:
                return []

            rows.sort(key=lambda x: x[0])
            self._dm_rows = rows

            # 1 按时间均匀取大部分
            keep = max(4, int(limit * 0.75))
            step = len(rows) / keep
            picked = [rows[int(i * step)] for i in range(keep)]

            # 2 高频重复的补进去，这种通常是全场高能
            from collections import Counter

            hot = [w for w, c in Counter(x[1] for x in rows).most_common(12) if c >= 3][: limit - keep]
            for w in hot:
                if w not in [p[1] for p in picked]:
                    picked.append((0.0, w))

            # 带时间点的尽量保留顺序
            head = sorted(x for x in picked if x[0] > 0)
            tail = [x for x in picked if x[0] <= 0]
            seq = head + tail
            return [f"{int(t // 60)}:{int(t % 60):02d} {w}" if t > 0 else w for t, w in seq]
        except Exception:
            return []

    @staticmethod
    def hot_times(danmaku: list[str], buckets: int = 4) -> list[int]:
        """从带时间点的弹幕里找出刷得最密的那几段，抽帧时重点照顾。"""
        from collections import Counter

        ts: list[int] = []
        for d in danmaku:
            m = re.match(r"^(\d+):(\d{2})\s", d or "")
            if m:
                ts.append(int(m.group(1)) * 60 + int(m.group(2)))
        if not ts:
            return []
        cnt = Counter(t // 12 * 12 for t in ts)
        return [t + 6 for t, _ in cnt.most_common(buckets)]

    # ---------- 抽帧 ----------

    def extract_frames(
        self,
        video: Path,
        duration: int,
        max_frames: int = 12,
        hot_times: list[int] | None = None,
        span: tuple[float, float] | None = None,
    ) -> tuple[list[str], str]:
        """挑帧：一半按画面变化挑，一半均匀铺开，防止只截到平淡的镜头。

        返回 (时间戳列表, base64 jpeg 拼图)。
        """
        try:
            import cv2
            from PIL import Image, ImageDraw
        except Exception:
            return [], ""

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            video.unlink(missing_ok=True)
            return [], ""
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        real_dur = total / fps if total and fps else max(1, duration)
        real_dur = max(1.0, min(float(real_dur), float(duration or real_dur) + 5))

        if span:
            # 只看一小段：这一段里密一点抽，不做变化检测
            lo = max(0.0, float(span[0]))
            hi = min(real_dur, float(span[1]))
            if hi - lo < 0.5:
                hi = min(real_dur, lo + 2)
            n = max(2, min(max_frames, 12))
            stamps: list[str] = []
            shots: list[Image.Image] = []
            col_w = 320
            for i in range(n):
                t = lo + (hi - lo) * (i + 0.5) / n
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = rgb.shape[:2]
                img = Image.fromarray(rgb).resize((col_w, int(col_w * h / max(1, w))), Image.LANCZOS)
                m, s_ = divmod(int(t), 60)
                label = f"{m}:{s_:02d}"
                dr = ImageDraw.Draw(img)
                dr.rectangle([0, 0, 46, 18], fill=(0, 0, 0))
                dr.text((5, 4), label, fill=(255, 255, 255))
                shots.append(img)
                stamps.append(label)
            cap.release()
            try:
                video.unlink(missing_ok=True)
            except Exception:
                pass
            return stamps, self._sheet(shots, col_w)

        n = max(4, min(max_frames, int(real_dur // 20) + 4))
        if real_dur <= 30:
            n = min(6, max_frames)

        # 先按两倍密度抽候选，后面再挑
        cand_n = max(n * 2, 8)
        cand_t = [real_dur * (i + 0.5) / cand_n for i in range(cand_n)]
        # 弹幕最密的地方单独加进去，保证高能段落一定有画面
        extra_t = sorted({float(t) for t in (hot_times or []) if 0 <= float(t) < real_dur})
        all_t = sorted({round(t, 1) for t in cand_t + extra_t})
        extra_idx = {i for i, t in enumerate(all_t) if round(t, 1) in {round(x, 1) for x in extra_t}}

        got: list[tuple[float, Any]] = []
        for t in all_t:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if ok and frame is not None:
                got.append((t, frame))
        cap.release()

        if not got:
            try:
                video.unlink(missing_ok=True)
            except Exception:
                pass
            return [], ""

        # 相邻帧差异，用来找镜头切换
        scores: list[tuple[float, int]] = []
        for i in range(1, len(got)):
            try:
                a = cv2.resize(cv2.cvtColor(got[i - 1][1], cv2.COLOR_BGR2GRAY), (64, 36))
                b = cv2.resize(cv2.cvtColor(got[i][1], cv2.COLOR_BGR2GRAY), (64, 36))
                scores.append((float(cv2.absdiff(a, b).mean()), i))
            except Exception:
                continue

        keep_hot = sorted(extra_idx)
        left = max(1, n - len(keep_hot))
        half = max(1, left // 2)
        hot = [i for _, i in sorted(scores, reverse=True)[:half]]
        rest = left - len(hot)
        even = [int(len(got) * (j + 0.5) / max(1, rest)) for j in range(rest)] if rest > 0 else []
        idxs = sorted(set([i for i in hot + even + keep_hot if 0 <= i < len(got)]))
        if len(idxs) > n:
            idxs = idxs[:n]

        stamps: list[str] = []
        shots: list[Image.Image] = []
        col_w = 320
        for i in idxs:
            t, frame = got[i]
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = rgb.shape[:2]
                nh = int(col_w * h / max(1, w))
                img = Image.fromarray(rgb).resize((col_w, nh), Image.LANCZOS)
            except Exception:
                continue
            m, s_ = divmod(int(t), 60)
            label = f"{m}:{s_:02d}"
            dr = ImageDraw.Draw(img)
            dr.rectangle([0, 0, 46, 18], fill=(0, 0, 0))
            dr.text((5, 4), label, fill=(255, 255, 255))
            shots.append(img)
            stamps.append(label)

        try:
            video.unlink(missing_ok=True)
        except Exception:
            pass

        if not shots:
            return [], ""

        return stamps, self._sheet(shots, col_w)


    @staticmethod
    def _sheet(shots: list[Any], col_w: int = 320) -> str:
        """把抽到的帧拼成一张联络表，返回 data URI。"""
        if not shots:
            return ""
        try:
            from PIL import Image
        except Exception:
            return ""
        cols = 4 if len(shots) >= 8 else (3 if len(shots) >= 5 else 2)
        rows = (len(shots) + cols - 1) // cols
        cell_h = max(im.height for im in shots)
        gap = 6
        sheet = Image.new(
            "RGB",
            (cols * col_w + gap * (cols + 1), rows * cell_h + gap * (rows + 1)),
            (18, 22, 26),
        )
        for idx, im in enumerate(shots):
            r_, c_ = divmod(idx, cols)
            sheet.paste(im, (gap + c_ * (col_w + gap), gap + r_ * (cell_h + gap)))
        limit = 1024
        if sheet.width > limit:
            ratio = limit / sheet.width
            sheet = sheet.resize((limit, int(sheet.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        sheet.save(buf, format="JPEG", quality=80, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    async def read_segment(
        self,
        bvid: str,
        cid: int,
        duration: int,
        start: float,
        end: float,
        n: int = 8,
    ) -> tuple[list[str], str]:
        """只看一小段：把 start 到 end 之间密一点抽出来，用于回头确认细节。"""
        url = await self.play_url(bvid, cid)
        if not url:
            return [], ""
        path = await self.download(url)
        if not path:
            return [], ""
        stamps, sheet = self.extract_frames(path, duration, n, span=(start, end))
        return stamps, sheet


    # ---------- 语音转文字 ----------

    def transcribe(self, video: Path, model_size: str = "small") -> list[tuple[float, float, str]]:
        """本地把声音转成带时间点的句子。第一次用会下模型，之后走缓存。"""
        global _WHISPER_WARNED
        try:
            from faster_whisper import WhisperModel
        except Exception:
            if not _WHISPER_WARNED:
                _WHISPER_WARNED = True
                logger.warning(
                    "[bili-arona] 语音转文字开着，但没装 faster-whisper，这条先跳过转写。"
                    "要用的话执行 pip install faster-whisper，或者去面板把这个开关关掉。"
                )
            return []
        try:
            if self._wm is None or self._wm_size != model_size:
                self._wm = WhisperModel(model_size, device="cpu", compute_type="int8")
                self._wm_size = model_size
            segments, _info = self._wm.transcribe(
                str(video),
                language="zh",
                vad_filter=True,
                beam_size=1,
                initial_prompt="这是一段中文视频里的口播，可能有口语和语气词。",
            )
            out: list[tuple[float, float, str]] = []
            for seg in segments:
                text = str(seg.text or "").strip()
                if text:
                    out.append((float(seg.start), float(seg.end), text))
            return out
        except Exception:
            return []

    @staticmethod
    def build_timeline(
        script: list[tuple[float, float, str]], stamps: list[str], span: float = 30.0
    ) -> list[str]:
        """把口播稿按时间切片，每片配上落在这一段的画面帧编号。"""
        if not script:
            return []
        rows: list[str] = []
        cur = 0.0
        end_all = max(e for _, e, _ in script)
        while cur < end_all:
            nxt = cur + span
            said = "".join(t for st, en, t in script if st < nxt and en > cur)
            idx = [
                i + 1
                for i, lab in enumerate(stamps)
                if VideoReader._mmss_to_sec(lab) >= cur and VideoReader._mmss_to_sec(lab) < nxt
            ]
            if said.strip():
                m1, s1 = divmod(int(cur), 60)
                m2, s2 = divmod(int(nxt), 60)
                head = f"{m1}:{s1:02d}-{m2}:{s2:02d}"
                tail = f" 画面第 {', '.join(map(str, idx))} 帧" if idx else ""
                rows.append(f"{head} {said.strip()[:220]}{tail}")
            cur = nxt
        return rows

    @staticmethod
    def _mmss_to_sec(label: str) -> float:
        try:
            m, s = str(label).split(":")
            return int(m) * 60 + int(s)
        except Exception:
            return 0.0

    # ---------- 一次看全 ----------

    async def read(
        self,
        bvid: str,
        aid: int,
        cid: int,
        duration: int,
        max_frames: int = 12,
        hot: list[str] | None = None,
        tname: str = "",
        transcribe: bool = True,
        whisper_model: str = "small",
    ) -> Watched:
        out = Watched(cid=cid, duration=duration, hot=list(hot or []), tname=tname)

        out.subtitles = await self.subtitles(bvid, cid, aid)
        out.danmaku = await self.danmaku(cid)
        if self._dm_rows:
            out.dm_stat = self.danmaku_stats(self._dm_rows, duration)
            out.notes.append(self.describe_stats(out.dm_stat))
        if out.danmaku:
            out.notes.append(f"弹幕 {len(out.danmaku)} 条")
        if out.subtitles:
            out.notes.append(f"字幕 {len(out.subtitles)} 字")

        url = await self.play_url(bvid, cid)
        if not url:
            out.notes.append("拿不到播放地址，只看了字幕和弹幕")
            return out
        path = await self.download(url)
        if not path:
            out.notes.append("视频太大或下载失败，只看了字幕和弹幕")
            return out
        # 先把声音转成文字，再抽帧（抽完就把视频删了）
        if transcribe and path is not None:
            try:
                # 转写是纯 CPU 活，跑在事件循环里会把这几十秒全堵死，
                # 丢线程池去跑，界面和聊天才不会被它冻住
                out.script = await asyncio.to_thread(self.transcribe, path, whisper_model)
            except Exception:
                out.script = []
            if out.script:
                out.notes.append(f"口播转写 {len(out.script)} 句")

        stamps, sheet = self.extract_frames(path, duration, max_frames, self.hot_times(out.danmaku))
        if sheet:
            out.frames = stamps
            out.sheet_b64 = sheet
            out.notes.append(f"抽了 {len(stamps)} 帧拼成一张图")
        else:
            out.notes.append("抽帧失败，只看了字幕和弹幕")
        return out

    @staticmethod
    def build_prompt(
        title: str, owner: str, mmss: str, desc: str, w: Watched, hot: list[str] | None = None
    ) -> str:
        parts = [
            f"标题：{title}",
            f"UP主：{owner}",
            f"时长：{mmss}",
        ]
        if w.tname:
            parts.append(f"分区：{w.tname}")
        parts.append(f"简介：{(desc or '无')[:300]}")
        hot = hot if hot is not None else w.hot
        if hot:
            parts.append("热评，按点赞排：\n" + "\n".join(f"- {h}" for h in hot[:6]))
        if w.frames:
            parts.append(
                "下面这张图是按时间顺序抽的画面，画面变化大的地方和弹幕最密的地方优先抽，左上角黑底数字是时间点："
                + "、".join(w.frames)
            )
        if w.script:
            tl = VideoReader.build_timeline(w.script, w.frames)
            if tl:
                parts.append(
                    "按时间排的口播稿（左边是时间，右边是这一段说了什么，后面标了同时段画面是第几帧）：\n"
                    + "\n".join(tl[:40])
                )
        if w.subtitles:
            parts.append("字幕内容：\n" + w.subtitles[:1200])
        if w.dm_stat:
            desc = VideoReader.describe_stats(w.dm_stat)
            if desc:
                parts.append("弹幕统计：\n" + desc)
        if w.danmaku:
            parts.append("弹幕采样（反映观众即时反应）：\n" + " / ".join(w.danmaku[:40]))
        parts.append(
            "要求：只写这些东西里确实有的。画面没看清、字幕没提到的，就说不清楚，不要靠常识补。"
            "抽帧是按画面变化挑的，能代表全片走势，但两帧之间可能还有内容，别当成全部。"
        )
        return "\n\n".join(parts)
