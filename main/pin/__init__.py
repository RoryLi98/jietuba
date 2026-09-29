"""
钉图(贴图)模块

提供将截图固定在屏幕上的功能，支持编辑、移动、缩放

架构说明（重构后）：
- PinWindow：主窗口，只负责窗口管理和子控件布局
- PinCanvasView：唯一内容渲染者，使用 Qt 的 GPU 加速渲染
- PinCanvas：画布核心，包含工具信号路由
- PinOCRManager：OCR 初始化和线程管理
- PinThumbnailMode：缩略图模式逻辑
- PinShortcutController：快捷键控制器（通过 ShortcutManager 统一管理）
- PinControlButtons：控制按钮管理器
- PinContextMenu：右键菜单管理器
- PinTranslationHelper：翻译功能助手

PinManager / get_pin_manager 保持包级导入：pin_manager 只依赖 Qt 与 core，
托盘菜单、启动预热都只要它，不值得为它按需加载。其余 UI 模块走惰性导出
（PEP 562）——否则托盘路径上 `from pin.pin_manager import PinManager` 也要
先执行整个钉图 UI 导入链，首次创建钉图时 UI 模块才真正加载。
"""

from .pin_manager import PinManager, get_pin_manager

# 导出名 → 所在子模块。首次访问时导入子模块、取符号、缓存进包 dict，
# 之后同名访问直接命中模块 dict，不再触发 __getattr__。
_LAZY_EXPORTS = {
    "PinWindow": ".pin_window",
    "PinToolbar": ".pin_toolbar",
    "PinCanvas": ".pin_canvas",
    "PinCanvasView": ".pin_canvas_view",
    "PinOCRManager": ".pin_ocr_manager",
    "PinThumbnailMode": ".pin_thumbnail",
    "PinShortcutController": ".pin_shortcut",
    "PinControlButtons": ".pin_controls",
    "PinContextMenu": ".pin_context_menu",
    "PinTranslationHelper": ".pin_translation",
    "OCRTextLayer": ".ocr_text_layer",
    "OCRTextItem": ".ocr_text_layer",
}

__all__ = ["PinManager", "get_pin_manager", *_LAZY_EXPORTS]


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
