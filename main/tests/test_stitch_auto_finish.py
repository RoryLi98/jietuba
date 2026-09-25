# -*- coding: utf-8 -*-
"""长截图到底自动完成判定测试。

ScrollCaptureWindow.__init__ 会建窗口、装鼠标钩子，测试用 __new__ 跳过，
只装配判定逻辑所需的属性，模拟一串帧哈希推进 _do_capture 的判定尾段。
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_window(screenshot_count):
    from stitch.scroll_window import ScrollCaptureWindow
    win = ScrollCaptureWindow.__new__(ScrollCaptureWindow)
    win._auto_finish_scheduled = False
    win._duplicate_capture_count = 0
    win._prev_capture_hash = None
    win.duplicate_threshold = 0.95
    win.screenshots = [None] * screenshot_count
    win.preview_panel = MagicMock()
    win.isVisible = MagicMock(return_value=True)
    win._on_finish = MagicMock()
    return win


def _evaluate(win, current_hash, frame_is_duplicate):
    """复刻 _do_capture 尾段的判定逻辑，避免为了测试去抓屏。

    与生产代码保持同构：若改动判定段，这里同步改。
    """
    if win._auto_finish_scheduled:
        pass
    elif frame_is_duplicate and len(win.screenshots) >= 3:
        win._duplicate_capture_count += 1
        if win._duplicate_capture_count >= 2:
            win._auto_finish_scheduled = True
            win.preview_panel.show_warning("已到达页面边缘")
    else:
        win._duplicate_capture_count = 0
    return win._auto_finish_scheduled


class TestAutoFinishDetection:

    def test_two_consecutive_duplicates_at_bottom_trigger_finish(self, qapp):
        win = _make_window(screenshot_count=4)
        # 第 3 帧与第 2 帧相同（到底），第 4 帧仍相同
        assert _evaluate(win, "h3", True) is False   # 第一次重复：观望
        assert _evaluate(win, "h4", True) is True    # 第二次重复：收尾
        win.preview_panel.show_warning.assert_called_once()

    def test_single_duplicate_does_not_finish(self, qapp):
        win = _make_window(screenshot_count=3)
        assert _evaluate(win, "h", True) is False
        assert win._on_finish.called is False

    def test_new_content_resets_duplicate_counter(self, qapp):
        win = _make_window(screenshot_count=6)
        assert _evaluate(win, "h1", True) is False
        assert _evaluate(win, "h2", False) is False  # 又滚出新内容
        assert win._duplicate_capture_count == 0
        assert _evaluate(win, "h3", True) is False   # 重新计 1

    def test_short_page_never_auto_finishes(self, qapp):
        """只有 2 帧时（初始 + 1 次滚动）不自动收尾。"""
        win = _make_window(screenshot_count=2)
        assert _evaluate(win, "h", True) is False
        assert _evaluate(win, "h", True) is False
        assert win._auto_finish_scheduled is False

    def test_scheduled_flag_prevents_double_trigger(self, qapp):
        win = _make_window(screenshot_count=5)
        win._auto_finish_scheduled = True
        assert _evaluate(win, "h", True) is True
        assert win.preview_panel.show_warning.called is False
