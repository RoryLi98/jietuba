# -*- coding: utf-8 -*-
"""工具栏溢出自动收纳测试：屏幕放不下时尾部按钮自动收进「…」。

不构造真实 Toolbar（会注册全局单例），只装配 _fold_overflow_into_more
依赖的宽度表与屏幕桩，验证折叠决策本身。
"""
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_toolbar(button_widths, handle_width=30, more_width=45, screen_width=1920):
    """宽度表驱动的折叠逻辑测试桩。"""
    from ui.toolbar import Toolbar
    toolbar = Toolbar.__new__(Toolbar)
    toolbar._button_widths = dict(button_widths)
    toolbar.drag_handle = SimpleNamespace(width=lambda: handle_width)
    toolbar._folded_keys = []
    toolbar._auto_folded_keys = []
    if screen_width is None:
        toolbar._available_toolbar_width = lambda: None
    else:
        geometry = SimpleNamespace(width=lambda: screen_width)
        screen = SimpleNamespace(availableGeometry=lambda: geometry)
        toolbar.screen = lambda: screen
        import PySide6.QtGui as gui
        toolbar._primary_screen = gui
    return toolbar


def _base_widths(keys, width=67):
    return {key: width for key in keys}


SHOW_KEYS = [f"b{i}" for i in range(12)]


class TestOverflowFold:

    def test_everything_fits_when_screen_is_wide(self, qapp):
        toolbar = _make_toolbar(_base_widths(SHOW_KEYS), screen_width=1920)
        kept = toolbar._fold_overflow_into_more(SHOW_KEYS)
        assert kept == SHOW_KEYS
        assert toolbar._auto_folded_keys == []

    def test_tail_buttons_fold_when_screen_is_narrow(self, qapp):
        # 800px 预算：扣除手柄 30 + more 45 + 余量 12 = 713 → 约 10 个 67px 按钮
        toolbar = _make_toolbar(_base_widths(SHOW_KEYS), screen_width=800)
        kept = toolbar._fold_overflow_into_more(SHOW_KEYS)
        assert kept and len(kept) < len(SHOW_KEYS)
        assert toolbar._auto_folded_keys == SHOW_KEYS[len(kept):]
        # 自动收纳的是尾部按钮，不是头部
        assert not set(kept) & set(toolbar._auto_folded_keys)

    def test_at_least_one_button_always_stays(self, qapp):
        toolbar = _make_toolbar(_base_widths(["only"], width=500), screen_width=400)
        kept = toolbar._fold_overflow_into_more(["only"])
        assert kept == ["only"]

    def test_unknown_screen_disables_folding(self, qapp):
        toolbar = _make_toolbar(_base_widths(SHOW_KEYS), screen_width=None)
        kept = toolbar._fold_overflow_into_more(SHOW_KEYS)
        assert kept == SHOW_KEYS
        assert toolbar._auto_folded_keys == []

    def test_configured_more_keys_are_not_duplicated(self, qapp):
        """用户手动收进「…」的按钮不该出现在自动收纳列表里。"""
        toolbar = _make_toolbar(_base_widths(["a", "b", "c"]), screen_width=800)
        toolbar._folded_keys = ["c"]          # 用户手动收起
        kept = toolbar._fold_overflow_into_more(["a", "b"])
        assert "c" not in toolbar._auto_folded_keys
        assert set(kept) <= {"a", "b"}


class TestScaleRebase:

    def test_new_default_100_equals_old_150(self, qapp):
        """缩放基准重定义：新 100% 的换算值 = 旧 150%（BASE_FACTOR 1.5）。"""
        from core.ui_scale import UIScaleManager, BASE_FACTOR
        manager = UIScaleManager.instance()
        original = manager.percent
        try:
            manager.set_percent(100)
            assert manager.px(100) == 150
            assert manager.factor == pytest.approx(1.5)
            assert BASE_FACTOR == 1.5
        finally:
            manager.set_percent(original)

    def test_dialog_scale_rebased_too(self, qapp):
        from core.ui_scale import DialogScaleManager
        manager = DialogScaleManager.instance()
        original = manager.percent
        try:
            manager.set_percent(100)
            assert manager.px(100) == 150
        finally:
            manager.set_percent(original)
