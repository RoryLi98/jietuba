# -*- coding: utf-8 -*-
"""长截图"内容感知监视 → 稳定判定 → 抓帧 → 拼接 → 预览更新"回归测试。

长截图已从"滚轮驱动"重写为"内容感知"：监视定时器持续对比框内画面的
降采样签名，检测到变化并稳定后自动抓帧。本文件验证：

1. 初始帧后，内容变化并稳定 → 自动采集第二帧并拼接；
2. 内容持续变化（滚动中/动画）→ 不抓帧，直到稳定或超时强拍；
3. 内容静止足够久且已有 ≥2 帧 → 自动收尾被排上；
4. 手动抓帧按钮照常工作；
5. 内容签名对光标闪烁级别的微变化有容忍度（不会被搅成永远不稳定）。
"""
import time

import pytest
from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QColor, QScreen, QPainter, QPixmap

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_pixmap(band_top: int, w=120, h=100) -> QPixmap:
    """白底 + 红色横带的抓屏替代帧（band_top = 带子 y）。"""
    pm = QPixmap(w, h)
    pm.fill(QColor(255, 255, 255))
    painter = QPainter(pm)
    painter.fillRect(QRect(0, band_top, w, 20), QColor(220, 30, 30))
    painter.end()
    return pm


def _make_caret_pixmap(caret_on: bool, w=120, h=100) -> QPixmap:
    """白底 + 一个 1px 宽文本光标（模拟输入框闪烁）。"""
    return _make_page(band_top=None, caret_y=40 if caret_on else None, w=w, h=h)


def _make_page(band_top, caret_y, w=120, h=100) -> QPixmap:
    """页面帧：白底 + 可选红色横带（band_top）+ 可选 1px 光标（caret_y）。

    光标纵坐标随内容滚动而移动（caret_y -= 滚动量），保证滚动前后
    两帧的重叠区逐像素一致，拼接结果可预期。"""
    pm = QPixmap(w, h)
    pm.fill(QColor(255, 255, 255))
    painter = QPainter(pm)
    if band_top is not None:
        painter.fillRect(QRect(0, band_top, w, 20), QColor(220, 30, 30))
    if caret_y is not None:
        painter.fillRect(QRect(10, caret_y, 1, 20), QColor(0, 0, 0))
    painter.end()
    return pm


def _pump_until(qapp, condition, timeout_s=8.0):
    """驱动事件循环直到 condition 成立（让 QTimer / 排队信号有机会跑）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class _GrabSeq:
    """按次序返回预制帧的 grabWindow 替身（耗尽后重复最后一帧）。"""

    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def __call__(self, screen, win_id, x=0, y=0, w=0, h=0):
        frame = self._frames[min(self._i, len(self._frames) - 1)]
        self._i += 1
        return frame


class _Alternator:
    """在几帧之间无限循环的 grabWindow 替身（模拟永不停止的变化）。"""

    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def __call__(self, screen, win_id, x=0, y=0, w=0, h=0):
        frame = self._frames[self._i % len(self._frames)]
        self._i += 1
        return frame


@pytest.fixture
def window(qapp, monkeypatch):
    """真实 ScrollCaptureWindow，缩短监视节奏便于测试。"""
    from stitch.scroll_window import ScrollCaptureWindow

    monkeypatch.setattr(ScrollCaptureWindow, "_WATCH_IDLE_MS", 20)
    monkeypatch.setattr(ScrollCaptureWindow, "_WATCH_ACTIVE_MS", 20)
    monkeypatch.setattr(ScrollCaptureWindow, "_AUTO_FINISH_IDLE_S", 0.4)
    monkeypatch.setattr(ScrollCaptureWindow, "_AUTO_FINISH_WARN_S", 0.2)

    win = ScrollCaptureWindow(QRect(0, 0, 120, 100), None)
    monkeypatch.setattr(
        QScreen, "grabWindow", _GrabSeq([_make_pixmap(80)])
    )
    win.show()
    # 初始帧（showEvent + 100ms 延迟 → 泵事件循环等它跑完）
    assert _pump_until(qapp, lambda: len(win.screenshots) >= 1), "初始帧未采集"
    yield win
    win._cleanup()
    win.close()
    qapp.processEvents()


class TestContentAwareWatcher:

    def test_change_then_stable_captures_frame(self, qapp, window, monkeypatch):
        """内容变化 → 稳定 2 拍 → 自动采集并拼接出更高的结果。"""
        window._grab_seq = _GrabSeq([_make_pixmap(60)])
        monkeypatch.setattr(QScreen, "grabWindow", window._grab_seq)

        assert _pump_until(
            qapp,
            lambda: len(window.screenshots) >= 2
            and window.stitched_result is not None
            and window.stitched_result.size[1] > 100,
        ), "画面变化并稳定后未自动采集（内容感知链路断裂）"
        assert window.preview_panel.count_label.text() == "2"

    def test_continued_change_defers_capture(self, qapp, window, monkeypatch):
        """内容持续变化（滚动中）：稳定点出现前不应抓帧。"""
        seq = _Alternator([_make_pixmap(70), _make_pixmap(30)])
        monkeypatch.setattr(QScreen, "grabWindow", seq)

        # 泵 ~0.5s（远超多拍），期间内容一直在变
        assert not _pump_until(
            qapp, lambda: len(window.screenshots) >= 2, timeout_s=0.5
        ), "内容持续变化时不该抓帧"
        assert len(window.screenshots) == 1

    def test_idle_timeout_schedules_auto_finish(self, qapp, window, monkeypatch):
        """已有 ≥2 帧后内容长期静止 → 自动收尾被排上。"""
        # 先让第二帧发生：变化 + 稳定
        monkeypatch.setattr(
            QScreen, "grabWindow", _GrabSeq([_make_pixmap(60)])
        )
        assert _pump_until(qapp, lambda: len(window.screenshots) >= 2)

        # 内容保持一致（grab 序列耗尽后重复最后一帧）→ 静止 → 自动收尾
        assert window._auto_finish_scheduled is False
        assert _pump_until(
            qapp,
            lambda: window._auto_finish_scheduled,
            timeout_s=3.0,
        ), "内容静止后自动收尾未被排上"

    def test_manual_capture_still_works(self, qapp, window, monkeypatch):
        """工具栏手动抓帧按钮：无视稳定判定立即出帧。"""
        monkeypatch.setattr(
            QScreen, "grabWindow", _GrabSeq([_make_pixmap(60)])
        )
        window._on_manual_capture()
        assert _pump_until(qapp, lambda: len(window.screenshots) >= 2), \
            "手动抓帧未生效"

    def test_caret_flicker_does_not_block_capture(self, qapp, monkeypatch):
        """光标闪烁级的微变化在签名相似度内：不被当成新内容反复抓帧。

        初始帧就是带光标的页面（真实场景），之后光标亮/暗交替——每对
        签名的差异只有约 32 位（阈值 160），应始终视为"内容未变"。
        """
        from stitch.scroll_window import ScrollCaptureWindow

        monkeypatch.setattr(ScrollCaptureWindow, "_WATCH_IDLE_MS", 20)
        monkeypatch.setattr(ScrollCaptureWindow, "_WATCH_ACTIVE_MS", 20)
        monkeypatch.setattr(ScrollCaptureWindow, "_AUTO_FINISH_IDLE_S", 5.0)
        monkeypatch.setattr(ScrollCaptureWindow, "_AUTO_FINISH_WARN_S", 3.0)

        seq = _Alternator([_make_page(80, 40), _make_page(80, None)])
        monkeypatch.setattr(QScreen, "grabWindow", seq)
        win = ScrollCaptureWindow(QRect(0, 0, 120, 100), None)
        win.show()
        try:
            assert _pump_until(qapp, lambda: len(win.screenshots) >= 1), "初始帧未采集"
            # 光标闪烁 0.6s：不应产生第二帧
            assert not _pump_until(
                qapp, lambda: len(win.screenshots) >= 2, timeout_s=0.6
            ), "光标闪烁不该被当成新内容"

            # 真实滚动 20px：光标随内容上移，横带 80→60（重叠区逐像素一致）
            monkeypatch.setattr(
                QScreen, "grabWindow", _GrabSeq([_make_page(60, 20)])
            )
            assert _pump_until(
                qapp,
                lambda: len(win.screenshots) >= 2
                and win.stitched_result is not None
                and win.stitched_result.size[1] > 100,
            ), "光标闪烁之后的新内容未被采集"
        finally:
            win._cleanup()
            win.close()
            qapp.processEvents()

    def test_signature_rejects_real_changes(self, qapp, window):
        """签名必须能区分真实滚动（横带位移），否则整个机制失效。"""
        s1 = window._signature(_make_pixmap(80))
        s2 = window._signature(_make_pixmap(60))
        assert not window._sig_similar(s1, s2), "真实滚动被判成了同一画面"
        assert window._sig_similar(s1, s1), "相同画面被判成了不同"
