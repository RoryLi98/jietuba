# -*- coding: utf-8 -*-
"""「跟随截图主题色」主题测试：强调色合一 + 动态刷新。"""
import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from clipboard.ui.theme.themes import (
    FOLLOW_THEME_NAME,
    build_follow_theme,
    get_theme_manager,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_follow_theme_derives_accent_from_screenshot_theme(qapp):
    from core.theme import get_theme
    accent = get_theme().theme_color_hex
    theme = build_follow_theme()
    c = theme.colors
    assert theme.name == FOLLOW_THEME_NAME
    assert c.accent_primary == accent
    assert c.border_accent == accent
    assert c.text_accent == accent
    # hover 是强调色的变暗版本
    assert c.accent_hover != accent
    a, h = QColor(accent), QColor(c.accent_hover)
    assert h.value() < a.value()


def test_follow_theme_background_follows_ui_mode_tokens(qapp):
    from core.ui_theme import get_ui_theme
    t = get_ui_theme().tokens
    theme = build_follow_theme()
    assert theme.colors.bg_primary == t.popup_background
    assert theme.colors.text_primary == t.text


def test_set_and_reload_follow_theme(qapp):
    mgr = get_theme_manager()
    assert mgr.set_theme(FOLLOW_THEME_NAME) is True
    assert mgr.get_current_theme().name == FOLLOW_THEME_NAME

    from settings import get_tool_settings_manager
    assert get_tool_settings_manager().get_clipboard_theme() == FOLLOW_THEME_NAME

    # 重新取管理器主题也应构建 follow（单例，直接读回）
    assert mgr.get_current_theme().name == FOLLOW_THEME_NAME


def test_accent_change_refreshes_following_theme(qapp):
    from core.theme import get_theme
    mgr = get_theme_manager()
    mgr.set_theme(FOLLOW_THEME_NAME)

    received = []
    mgr.theme_changed.connect(lambda theme: received.append(theme))

    original = QColor(get_theme().theme_color_hex)
    try:
        new_color = QColor("#FF8800") if original.name().upper() != "#FF8800" else QColor("#00CC66")
        get_theme().set_theme_color(new_color)
        after = mgr.get_current_theme().colors.accent_primary
        assert after == new_color.name().upper() or QColor(after) == new_color
        assert received, "跟随主题应广播 theme_changed"
    finally:
        get_theme().set_theme_color(original)


def test_follow_theme_has_complete_tokens(qapp):
    """派生主题不允许缺 token：27 个字段全部非空。"""
    theme = build_follow_theme()
    for name, value in theme.colors.to_dict().items():
        assert value, name
