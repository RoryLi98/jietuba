# -*- coding: utf-8 -*-
"""clipboard 模块导出入口。

core 层（数据模型 + Rust 监听封装）保持包级导入：启动预热、钉图、
主窗口都只要 ClipboardManager 这一层。UI（历史窗口 / 管理窗口）很重，
走惰性导出（PEP 562）——`from clipboard import ClipboardWindow` 的用法
不变，但 `import clipboard` 不再把整套剪贴板 UI 拖进内存。
"""

from .core import ClipboardItem, ClipboardManager, Group, GroupType

_LAZY_EXPORTS = {
    "ManageDialog": ".ui.dialogs.manage_dialog",
    "destroy_manage_dialog": ".ui.dialogs.manage_dialog",
    "get_existing_manage_dialog": ".ui.dialogs.manage_dialog",
    "get_manage_dialog": ".ui.dialogs.manage_dialog",
    "ClipboardWindow": ".ui.windows.clipboard_window",
}

__all__ = [
    "ClipboardItem",
    "ClipboardManager",
    "ClipboardWindow",
    "Group",
    "GroupType",
    "ManageDialog",
    "destroy_manage_dialog",
    "get_existing_manage_dialog",
    "get_manage_dialog",
]


def __getattr__(name):
    path = _LAZY_EXPORTS.get(name)
    if path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module(path, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)
