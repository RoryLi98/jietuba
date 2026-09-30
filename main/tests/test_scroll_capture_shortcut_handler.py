# -*- coding: utf-8 -*-
"""长截图窗口快捷键分发测试（stitch/scroll_window.py · ScrollCaptureShortcutHandler）

长截图会话里截图窗口已经关闭，优先级 100 的 ScreenshotShortcutHandler 随之
注销，本 handler（优先级 90）是唯一活跃的键盘入口。它的三条分发——ESC 取消、
Ctrl+C 完成并复制、确认键同 Ctrl+C——在实现之前完全不存在：长截图是「只能
点按钮」的，Ctrl+C 按下去会被系统当成复制热键消费掉，窗口根本收不到。

隔离方式与 test_screenshot_shortcut_handler 一致：用 __new__ 跳过 __init__
（它会读用户配置里的快捷键绑定），手工装配 _bindings / _window，用假事件对象
和 MagicMock 窗口驱动。
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import stitch.scroll_window as sw
from stitch.scroll_window import ScrollCaptureShortcutHandler, ScrollCaptureWindow

NO_MOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier

# 刻意不用生产默认值（Enter）：confirm 走 Space，才能和「Ctrl+C 是硬编码分支」
# 分开验证，不会出现两条分支都命中 Enter 说不清是谁接的
BINDINGS = {
    "inapp_confirm": (Qt.Key.Key_Space, NO_MOD),
}


class _FakeKeyEvent:
    def __init__(self, key, modifiers=NO_MOD, auto_repeat=False):
        self._key = key
        self._mods = modifiers
        self._auto = auto_repeat

    def key(self):
        return self._key

    def modifiers(self):
        return self._mods

    def isAutoRepeat(self):
        return self._auto


def _make_window():
    window = MagicMock()
    window.isVisible.return_value = True
    return window


def _make_handler(window):
    handler = ScrollCaptureShortcutHandler.__new__(ScrollCaptureShortcutHandler)
    handler._window = window
    handler._bindings = dict(BINDINGS)
    return handler


class TestHandlerIdentity:

    def test_priority_sits_between_screenshot_and_gif(self):
        """100 截图窗口 < 本 handler < 200 热键录入框：长截图期间截图窗口已注销，
        但 GIF(80)/剪贴板(60)/钉图(50) 可能同时在链上，必须压过它们否则按键
        会被先接走。"""
        assert _make_handler(_make_window()).priority == 90

    def test_name_matches_the_window_it_serves(self):
        assert _make_handler(_make_window()).handler_name == "ScrollCaptureWindow"


class TestIsActive:

    def test_visible_window_without_modal_is_active(self):
        assert _make_handler(_make_window()).is_active() is True

    def test_missing_window_is_inactive(self):
        assert _make_handler(None).is_active() is False

    def test_hidden_window_is_inactive(self):
        window = _make_window()
        window.isVisible.return_value = False
        assert _make_handler(window).is_active() is False

    def test_destroyed_cpp_object_is_inactive_not_a_crash(self):
        """窗口已被 WA_DeleteOnClose 销毁后迟到的按键：isVisible() 抛
        RuntimeError，handler 必须自己咽掉——分发链只放行 is_active()。"""
        window = _make_window()
        window.isVisible.side_effect = RuntimeError("wrapped C/C++ object deleted")
        assert _make_handler(window).is_active() is False

    def test_modal_dialog_takes_the_keyboard(self, monkeypatch):
        """长截图里弹出的模态对话框要让出键盘，否则对话框里按 ESC 会取消整次截图"""
        monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(lambda: object()))
        assert _make_handler(_make_window()).is_active() is False

    def test_stays_active_while_finishing_so_escape_can_still_cancel(self):
        """收尾（_on_finish 的事件泵，最长 20+10 秒）期间窗口仍可见：ESC 必须
        还能被接住去记取消请求，所以 _finishing 不该让 handler 失活。"""
        window = _make_window()
        window._finishing = True
        assert _make_handler(window).is_active() is True


class TestEscape:

    def test_escape_cancels(self):
        window = _make_window()
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_Escape)) is True
        window._on_cancel.assert_called_once()
        window._on_finish.assert_not_called()

    def test_escape_cancels_even_while_finishing(self):
        window = _make_window()
        window._finishing = True
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_Escape)) is True
        # 只转发请求：是否真的中止由 _on_cancel 自己按 _finishing 决定
        window._on_cancel.assert_called_once()

    def test_escape_with_modifiers_still_cancels(self):
        """ESC 的语义固定，Ctrl+ESC / Shift+ESC 也走取消（与截图会话一致）"""
        for mods in (CTRL, SHIFT, CTRL | SHIFT):
            window = _make_window()
            handler = _make_handler(window)
            assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_Escape, mods)) is True
            window._on_cancel.assert_called_once()


class TestCtrlC:

    def test_plain_ctrl_c_finishes_and_copies(self):
        window = _make_window()
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_C, CTRL)) is True
        window._on_finish.assert_called_once()
        window._on_cancel.assert_not_called()

    @pytest.mark.parametrize("mods", [
        SHIFT, ALT, CTRL | SHIFT, CTRL | ALT, CTRL | SHIFT | ALT,
        NO_MOD,
    ])
    def test_ctrl_c_variants_do_not_finish(self, mods):
        """只认裸 Ctrl+C：Ctrl+Shift+C 是终端复制、Ctrl+Alt+C 可能被用户绑了
        别的，裸 C 是普通输入——这些都不能触发结束。"""
        window = _make_window()
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_C, mods)) is False
        window._on_finish.assert_not_called()

    def test_ctrl_plus_another_key_does_not_finish(self):
        window = _make_window()
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_V, CTRL)) is False
        window._on_finish.assert_not_called()

    def test_held_ctrl_c_fires_once(self):
        """按住不放的自动重复事件必须被挡下：否则收尾期间会反复进分发"""
        window = _make_window()
        handler = _make_handler(window)
        first = handler.handle_key(_FakeKeyEvent(Qt.Key.Key_C, CTRL))
        repeat = handler.handle_key(_FakeKeyEvent(Qt.Key.Key_C, CTRL, auto_repeat=True))
        assert first is True
        assert repeat is False
        window._on_finish.assert_called_once()


class TestConfirmBinding:

    def test_confirm_binding_finishes(self):
        window = _make_window()
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_Space)) is True
        window._on_finish.assert_called_once()

    def test_confirm_binding_with_extra_modifier_does_not_match(self):
        """绑定是 Space 裸键，Space+Ctrl 不能命中"""
        window = _make_window()
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_Space, CTRL)) is False
        window._on_finish.assert_not_called()

    def test_unbound_key_is_not_consumed(self):
        """没接住的按键要放行，交给窗口自身（按钮焦点、系统行为）"""
        window = _make_window()
        handler = _make_handler(window)
        assert handler.handle_key(_FakeKeyEvent(Qt.Key.Key_Q, CTRL)) is False
        window._on_finish.assert_not_called()
        window._on_cancel.assert_not_called()


class TestRegistration:
    """showEvent → _register_shortcut_handler，_cleanup → 注销：成对且幂等。"""

    @pytest.fixture()
    def manager(self, monkeypatch):
        from core.shortcut_manager import ShortcutManager
        mgr = MagicMock()
        monkeypatch.setattr(ShortcutManager, "instance", staticmethod(lambda: mgr))
        return mgr

    def test_show_creates_and_registers_exactly_one_handler(self, monkeypatch, manager):
        handler = _make_handler(_make_window())
        monkeypatch.setattr(sw, "ScrollCaptureShortcutHandler", lambda w: handler)

        fake = MagicMock()
        fake._shortcut_handler = None
        ScrollCaptureWindow._register_shortcut_handler(fake)
        assert fake._shortcut_handler is handler
        manager.register.assert_called_once_with(handler)

        # 重新 show：复用同一 handler（真实 register 按 identity 去重）
        ScrollCaptureWindow._register_shortcut_handler(fake)
        assert fake._shortcut_handler is handler
        assert manager.register.call_count == 2

    def test_cleanup_unregisters_and_drops_the_reference(self, manager):
        handler = _make_handler(_make_window())
        fake = MagicMock()
        fake._shortcut_handler = handler

        ScrollCaptureWindow._cleanup(fake)
        manager.unregister.assert_called_once_with(handler)
        assert fake._shortcut_handler is None

    def test_cleanup_is_idempotent(self, manager):
        """_on_finish 与 closeEvent 各调一次 _cleanup，第二次不能再注销一次"""
        handler = _make_handler(_make_window())
        fake = MagicMock()
        fake._shortcut_handler = handler

        ScrollCaptureWindow._cleanup(fake)
        ScrollCaptureWindow._cleanup(fake)
        manager.unregister.assert_called_once_with(handler)
        assert fake._shortcut_handler is None

    def test_cleanup_without_a_registered_handler_is_safe(self, manager):
        """构造半途失败、窗口从未 show 过：没有 handler 也要能走完清理"""
        fake = MagicMock()
        fake._shortcut_handler = None
        ScrollCaptureWindow._cleanup(fake)
        manager.unregister.assert_not_called()
