"""保存插件实例引用，供控制面板接口拿到底层客户端。"""

from __future__ import annotations

from typing import Any

_PLUGIN: Any = None


def set_plugin(instance: Any) -> None:
    global _PLUGIN
    _PLUGIN = instance


def get_plugin() -> Any:
    return _PLUGIN
