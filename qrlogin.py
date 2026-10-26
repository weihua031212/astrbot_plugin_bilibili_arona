"""扫码登录：生成二维码、轮询结果、自动写入登录态。

本质和手动贴 Cookie 一样，最后拿到的都是 SESSDATA 和 bili_jct，
只是让账号主人在手机上点两下，不用去开发者工具里翻请求头。
"""

from __future__ import annotations

import base64
import io
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 86101 未扫码 86090 已扫待确认 86100 已确认登录 86038 二维码已失效
STATE_TEXT = {
    0: "登录成功",
    86101: "等你扫码",
    86090: "扫到了，在手机上点确认",
    86100: "已确认，正在取登录态",
    86038: "二维码过期了，刷新一下",
}


@dataclass
class LoginSession:
    qrcode_key: str
    created: float
    state: int = 86101
    cookies: dict[str, str] | None = None


def make_qr_png(url: str, box: int = 7, border: int = 2) -> str:
    """把登录链接变成 PNG 的 base64 data URI。"""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=box, border=border)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#152530", back_color="#ffffff").convert("RGB")
    # 面板里显示 200px 上下就够了，太大反而占内存
    if img.width > 260:
        ratio = 260 / img.width
        img = img.resize((260, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def make_qr_svg(url: str, box: int = 6, border: int = 3) -> str:
    """同一条链接画成内联 SVG。

    面板里用 data URI 的图片有时显示不出来，直接塞 SVG 就不用管图片加载了。
    """
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=box, border=border)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    side = n * box
    parts = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                parts.append(f"M{x * box} {y * box}h{box}v{box}h-{box}z")
    path = "".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" '
        f'width="100%" height="100%" shape-rendering="crispEdges" role="img" '
        f'aria-label="B 站登录二维码">'
        f'<rect width="{side}" height="{side}" fill="#ffffff"/>'
        f'<path d="{path}" fill="#152530"/></svg>'
    )


def parse_set_cookie(resp: httpx.Response) -> dict[str, str]:
    """从响应的 Set-Cookie 里抠出登录票据。"""
    out: dict[str, str] = {}
    for raw in resp.headers.get_list("set-cookie"):
        first = raw.split(";", 1)[0]
        if "=" not in first:
            continue
        k, v = first.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3"} and v:
            out[k] = urllib.parse.unquote(v)
    return out


def parse_from_url(url: str) -> dict[str, str]:
    """成功回调里的 url 查询串同样带着票据，作为兜底。"""
    out: dict[str, str] = {}
    if not url:
        return out
    q = urllib.parse.urlparse(url).query
    for k, v in urllib.parse.parse_qsl(q):
        if k in {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"} and v:
            out[k] = v
    return out


class QrLogin:
    def __init__(self, timeout: float = 15.0) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def generate(self) -> tuple[LoginSession, str, str]:
        r = await self.client.get(PASSPORT + "/generate")
        data = (r.json() or {}).get("data") or {}
        key = str(data.get("qrcode_key") or "")
        link = str(data.get("url") or "")
        if not key or not link:
            raise RuntimeError("拿不到二维码，B 站接口没有返回登录链接")
        sess = LoginSession(qrcode_key=key, created=time.time())
        return sess, make_qr_png(link), make_qr_svg(link)

    async def poll(self, sess: LoginSession) -> tuple[str, dict[str, str] | None]:
        """返回 (状态文案, 登录态或 None)。"""
        try:
            r = await self.client.get(PASSPORT + "/poll", params={"qrcode_key": sess.qrcode_key})
            payload = (r.json() or {}).get("data") or {}
        except Exception as exc:
            return f"查询失败：{exc}", None

        code = int(payload.get("code") or 0)
        sess.state = code
        if code != 0:
            return STATE_TEXT.get(code, f"状态 {code}"), None

        cookies = parse_set_cookie(r)
        cookies.update(parse_from_url(str(payload.get("url") or "")))
        # 二次兜底：有些环境只给跳转链接，参数在重定向地址里
        if not cookies.get("SESSDATA"):
            for c in r.cookies.jar:
                if c.name in {"SESSDATA", "bili_jct"} and c.value:
                    cookies[c.name] = urllib.parse.unquote(c.value)

        if not cookies.get("SESSDATA"):
            return "已确认，但没拿到 SESSDATA，换手动绑定试试", None
        sess.cookies = cookies
        return STATE_TEXT[0], cookies

    @staticmethod
    def expired(sess: LoginSession) -> bool:
        return time.time() - sess.created > 170
