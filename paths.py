"""插件数据目录。

按 AstrBot 规范，插件自己的数据要放在 data/plugin_data/<插件名>/ 下面，
不能塞进插件安装目录里，不然用户升级或者重装插件时，Cookie、日记、
留言箱这些东西会跟着插件目录一起被覆盖掉。

老版本确实把东西存在插件自己的 data/ 里，所以这里带一段搬运：
第一次跑到新位置时，发现新目录是空的、而老目录里有文件，就整个复制过去，
老的留着不动，留个后路。老用户重载一次就迁完，新用户直接在新目录建档。
"""

from __future__ import annotations

import shutil
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_bilibili_arona"

# 插件安装目录（老位置就在它下面的 data/）
PLUGIN_DIR = Path(__file__).resolve().parent
LEGACY_DIR = PLUGIN_DIR / "data"


def _plugin_data_root() -> Path | None:
    """拿到 AstrBot 规定的 plugin_data 目录。拿不到就返回 None。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path())
    except Exception:
        return None


def _migrate(old: Path, new: Path) -> None:
    """把老目录里的东西搬到新目录，搬的是副本，不删原件。"""
    try:
        if not old.is_dir():
            return
        if any(new.iterdir()):
            # 新目录已经有东西了，说明要么搬过了，要么本来就是新装的，
            # 这时候再覆盖会把用户现在的数据弄坏，所以一律不动。
            return
        for item in old.iterdir():
            target = new / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    except Exception:
        # 搬失败也不能让插件起不来，老路径还能用
        pass


def _pick() -> Path:
    root = _plugin_data_root()
    if root is None:
        # 拿不到规范目录就退回老位置，功能优先
        LEGACY_DIR.mkdir(parents=True, exist_ok=True)
        return LEGACY_DIR
    new = root / PLUGIN_NAME
    try:
        new.mkdir(parents=True, exist_ok=True)
    except Exception:
        LEGACY_DIR.mkdir(parents=True, exist_ok=True)
        return LEGACY_DIR
    _migrate(LEGACY_DIR, new)
    return new


DATA_DIR = _pick()


def path(name: str) -> Path:
    """取数据目录下某个文件，顺手保证目录存在。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return DATA_DIR / name
