# -*- coding: utf-8 -*-
"""界面缩放管理

本应用刻意运行在「1 逻辑像素 = 1 物理像素」模式下（bootstrap 关闭了 Qt
高 DPI 缩放），因为截图/钉图/长拼接等大量使用 mss 与 win32 返回的物理
像素坐标。直接打开 QT_SCALE_FACTOR 会让整条坐标链错位，因此界面缩放
采用「字体 + 固定尺寸」方案：

  1. 启动时替换 QWidget.setStyleSheet / QApplication.setStyleSheet：
     把样式表中的 font-size: Npx|pt 与 font: Npx|pt 简写按系数放大，
     原始文本存入动态属性，供切换系数时无损重算；
  2. 接管 QWidget.setFixed*/setMinimum* 系列方法：硬编码的控件尺寸
     同样按系数放大（原始值存属性），字体变大后布局随之变大，
     不会出现「字大了框没大」的挤压；由图片适配、字体度量或运行时
     几何值决定尺寸的控件用 mark_unscaled() 豁免；
  3. 按系数放大应用默认字体，覆盖未走样式表的文字（菜单、提示框等）；
  4. set_ui_scale() 支持运行中切换：重设应用字体，并按原始值重刷
     所有已存在控件的样式表与固定尺寸。

系数保存在应用设置 ui_scale 中（默认 1.0，重启后依然生效）。
"""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

UI_SCALE_MIN = 1.0
UI_SCALE_MAX = 2.0
UI_SCALE_OPTIONS = [1.0, 1.25, 1.5, 1.75, 2.0]

_scale: float = 1.0
_base_font: Optional[QFont] = None
_patched = False
_orig_widget_setStyleSheet = None
_orig_app_setStyleSheet = None

# 原始样式表存放的动态属性名
_RAW_SHEET_PROP = "_ui_scale_raw_stylesheet"
# 「不参与固定尺寸缩放」标记的动态属性名（由 mark_unscaled 设置）
_NO_SCALE_PROP = "_ui_scale_no_scale"
# 固定尺寸调用日志存放的动态属性名：[(方法名, 原始参数), ...]
_OPS_PROP = "_ui_scale_fixed_ops"

# 接管的固定尺寸方法。豁免（mark_unscaled）用于尺寸由图片适配 /
# 字体度量 / 运行时几何决定的控件，避免二次缩放。
_FIXED_SIZE_METHODS = (
    "setFixedWidth",
    "setFixedHeight",
    "setFixedSize",
    "setMinimumWidth",
    "setMinimumHeight",
    "setMinimumSize",
)

# 原始方法备份，运行中重放固定尺寸时绕过补丁直接调用
_orig_fixed_methods: dict = {}

# font-size: 13px / 9pt
_FONT_SIZE_RE = re.compile(r"(font-size\s*:\s*)(\d+(?:\.\d+)?)(px|pt\b)")
# font: 600 13px Microsoft YaHei（简写，尺寸前最多三个样式/字重关键字，
# 字重可能是数字如 600，因此前缀 token 允许字母数字）
_FONT_SHORTHAND_RE = re.compile(
    r"(\bfont\s*:\s*(?:[a-zA-Z0-9]+\s+){0,3})(\d+(?:\.\d+)?)(px|pt\b)"
)
# 已缩放标记：防止「读回样式表再设回去」的代码路径造成二次放大
_SCALED_MARK = "/*ui-scale-scaled*/"


def get_ui_scale() -> float:
    """当前界面缩放系数。"""
    return _scale


def scaled(value: float) -> int:
    """按当前系数换算字号/尺寸（px、pt 通用，至少为 1）。"""
    return max(1, round(value * _scale))


# 兼容别名：语义上专指像素尺寸的场景
scaled_px = scaled


def _clamp(value: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    if value < UI_SCALE_MIN:
        return UI_SCALE_MIN
    if value > UI_SCALE_MAX:
        return UI_SCALE_MAX
    return value


def scale_stylesheet(text: Optional[str]) -> str:
    """把样式表中的字体尺寸按当前系数放大。"""
    if not text or _scale == 1.0 or _SCALED_MARK in text:
        return text or ""

    def _sub(m: "re.Match[str]") -> str:
        size = max(1, round(float(m.group(2)) * _scale))
        return f"{m.group(1)}{size}{m.group(3)}"

    # 先在末尾加标记再替换，替换结果自带标记
    result = _FONT_SIZE_RE.sub(_sub, text + "\n" + _SCALED_MARK)
    result = _FONT_SHORTHAND_RE.sub(_sub, result)
    return result


def load_ui_scale_from_config() -> float:
    """从应用设置读取缩放系数（QApplication 创建前调用）。"""
    global _scale
    try:
        from settings import get_tool_settings_manager
        value = get_tool_settings_manager().get_app_setting("ui_scale", 1.0)
    except Exception:
        value = 1.0
    _scale = _clamp(value)
    return _scale


def install_stylesheet_patch() -> None:
    """接管样式表与固定尺寸设置入口，按当前系数放大。

    必须在创建任何窗口之前调用；重复调用无效果。
    """
    global _patched, _orig_widget_setStyleSheet, _orig_app_setStyleSheet
    if _patched:
        return

    _orig_widget_setStyleSheet = QWidget.setStyleSheet

    def _widget_set_stylesheet(self: QWidget, styleSheet: str) -> None:
        self.setProperty(_RAW_SHEET_PROP, styleSheet or "")
        _orig_widget_setStyleSheet(self, scale_stylesheet(styleSheet))

    QWidget.setStyleSheet = _widget_set_stylesheet  # type: ignore[assignment]

    if hasattr(QApplication, "setStyleSheet"):
        _orig_app_setStyleSheet = QApplication.setStyleSheet

        def _app_set_stylesheet(self, styleSheet: str) -> None:
            self.setProperty(_RAW_SHEET_PROP, styleSheet or "")
            _orig_app_setStyleSheet(self, scale_stylesheet(styleSheet))

        QApplication.setStyleSheet = _app_set_stylesheet  # type: ignore[assignment]

    # 固定尺寸：每次调用的原始值按顺序存入动态属性，实际设置缩放后的值。
    # set_ui_scale() 运行中切换时按调用顺序重放（后调用的方法覆盖先前的，
    # 与原生语义一致），实现整体重排。
    for method_name in _FIXED_SIZE_METHODS:
        orig_method = getattr(QWidget, method_name)
        _orig_fixed_methods[method_name] = orig_method

        def _make_patched(orig=orig_method, name=method_name):
            def _patched(self, *args):
                if args and all(isinstance(a, (int, float)) for a in args):
                    if self.property(_NO_SCALE_PROP):
                        orig(self, *args)
                        return
                    ops = self.property(_OPS_PROP)
                    ops = list(ops) if ops else []
                    if ops and ops[-1][0] == name:
                        ops[-1] = (name, tuple(int(a) for a in args))
                    else:
                        ops.append((name, tuple(int(a) for a in args)))
                    self.setProperty(_OPS_PROP, ops)
                    args = tuple(scaled(a) for a in args)
                orig(self, *args)
            return _patched

        setattr(QWidget, method_name, _make_patched())

    _patched = True


def mark_unscaled(widget: QWidget) -> None:
    """标记控件不参与固定尺寸缩放。

    用于尺寸由内容/图片适配或运行时几何值决定的控件
    （再乘缩放系数会二次放大）。必须在首次 setFixed*/setMinimum* 之前调用。
    """
    widget.setProperty(_NO_SCALE_PROP, True)


def scaled_window_size(width: int, height: int) -> tuple:
    """缩放顶层窗口的默认尺寸，并夹到屏幕可用区域的 92% 以内。"""
    w, h = scaled(width), scaled(height)
    app = QApplication.instance()
    if app is not None:
        screen = app.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(w, int(avail.width() * 0.92))
            h = min(h, int(avail.height() * 0.92))
    return w, h


def apply_app_font(app: QApplication) -> None:
    """按系数设置应用默认字体（首次调用时记录未缩放的基准字体）。"""
    global _base_font
    if _base_font is None:
        _base_font = QFont(app.font())
    font = QFont(_base_font)
    font.setPointSize(max(1, round(_base_font.pointSize() * _scale)))
    app.setFont(font)


def _rescale_fixed_sizes(widget: QWidget) -> None:
    """按记录的调用顺序重放一个控件的固定/最小尺寸设置。"""
    ops = widget.property(_OPS_PROP)
    if not ops:
        return
    for name, args in ops:
        orig = _orig_fixed_methods.get(name)
        if orig is not None:
            orig(widget, *(scaled(a) for a in args))


def set_ui_scale(value: float) -> float:
    """设置缩放系数并即时生效（应用字体 + 样式表 + 固定尺寸整体重排）。"""
    global _scale
    _scale = _clamp(value)

    app = QApplication.instance()
    if app is None:
        return _scale

    apply_app_font(app)

    if _patched and _orig_widget_setStyleSheet is not None:
        for widget in QApplication.allWidgets():
            try:
                raw = widget.property(_RAW_SHEET_PROP)
                if raw is not None:
                    _orig_widget_setStyleSheet(widget, scale_stylesheet(raw))
                _rescale_fixed_sizes(widget)
            except RuntimeError:
                continue  # 底层 C++ 对象已销毁

        # 应用级样式表（QApplication 自身不是 QWidget，需单独处理）
        if _orig_app_setStyleSheet is not None:
            raw = app.property(_RAW_SHEET_PROP)
            if raw is not None:
                _orig_app_setStyleSheet(app, scale_stylesheet(raw))
    return _scale
