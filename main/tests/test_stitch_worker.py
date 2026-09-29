# -*- coding: utf-8 -*-
"""长截图像素管线（_StitchWorker）与自动完成路径的回归测试。

背景：自动完成路径曾把 T() 的 LogMsg 直接传给 PreviewPanel.show_warning →
setToolTip(LogMsg) 抛 TypeError。旧代码里它被 _do_capture 的 try/except 吞掉
（自动收尾同样静默失效）；改成排队槽后异常直接顶到 sys.excepthook，表现为
滚动到底后弹崩溃日志、永远不出图。

这组测试锁住三件事：
1. worker 线程用真实帧走完 翻转/哈希/Rust拼接/预览缩略图 全流程；
2. 连续两帧重复时 auto_finish 必须置位（自动完成的触发源）；
3. 即使提示 UI 抛异常，900ms 的自动收尾定时器也必须已经排上。
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

longstitch = pytest.importorskip(
    "longstitch", reason="需要安装自制的 longstitch Rust 扩展包"
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ── 帧构造 ────────────────────────────────────────────────────────

W, H = 120, 100


def _make_frame(band_top: int) -> QImage:
    """造一帧截图：白底 + 一条红色横带（band_top = 带子在帧内的 y）。"""
    img = QImage(W, H, QImage.Format.Format_ARGB32)
    img.fill(QColor(255, 255, 255))
    painter = QPainter(img)
    painter.fillRect(QRect(0, band_top, W, 20), QColor(220, 30, 30))
    painter.end()
    return img


def _submit_and_wait(worker, qimage, timeout=10.0):
    done = threading.Event()
    results = []
    worker._emit = lambda payload: (results.append(payload), done.set())
    worker.submit(qimage, "vertical")
    assert done.wait(timeout), "worker 未在超时内返回结果"
    return results[0]


class TestWorkerPipeline:

    def test_stitches_two_overlapping_frames(self):
        """frame1 显示页面 0..100（带子在 80..100），frame2 显示 20..120
        （带子在 60..80）——向下滚 20px。拼接结果应为页高 120。"""
        from stitch.scroll_window import _StitchWorker

        worker = _StitchWorker(
            scroll_direction="vertical",
            locked_direction=None,
            duplicate_threshold=0.95,
            emit_fn=lambda payload: None,
        )

        first = _submit_and_wait(worker, _make_frame(80))
        assert first["ok"] is True
        assert first["screenshot_count"] == 1
        assert first["stitched"].size == (W, H)
        assert first["preview_qimage"] is not None
        assert not first["preview_qimage"].isNull()

        second = _submit_and_wait(worker, _make_frame(60))
        assert second["ok"] is True
        assert second["screenshot_count"] == 2
        # 向下滚 20px：拼接结果 = 100 + 20 = 120 高
        assert second["stitched"].size == (W, H + 20)
        # 自动方向检测把首次滚动锁定为 forward（down）
        assert second["locked_direction"] == "down"

        worker.stop()

    def test_flags_auto_finish_on_two_duplicate_frames(self):
        """连续 2 帧画面不变（到底）→ 第二次重复时 auto_finish=True。"""
        from stitch.scroll_window import _StitchWorker

        worker = _StitchWorker(
            scroll_direction="vertical",
            locked_direction=None,
            duplicate_threshold=0.95,
            emit_fn=lambda payload: None,
        )

        _submit_and_wait(worker, _make_frame(80))   # 初始帧
        _submit_and_wait(worker, _make_frame(60))   # 滚到底
        dup1 = _submit_and_wait(worker, _make_frame(60))
        assert dup1["auto_finish"] is False          # 只差 1 次：观望
        dup2 = _submit_and_wait(worker, _make_frame(60))
        assert dup2["auto_finish"] is True           # 连续 2 帧：收尾

        worker.stop()


class TestAutoFinishPath:

    def _make_window(self, preview_panel):
        from stitch.scroll_window import ScrollCaptureWindow

        win = ScrollCaptureWindow.__new__(ScrollCaptureWindow)
        win.screenshots = []
        win.stitched_result = None
        win.scroll_distances = []
        win.scroll_locked_direction = None
        win._auto_finish_scheduled = False
        win.preview_warning_active = False
        win.preview_panel = preview_panel
        win._position_preview_panel = lambda: None
        win._clear_preview_warning = lambda: None
        win._show_preview_warning = lambda message: None
        return win

    def test_auto_finish_schedules_finish_even_if_warning_ui_raises(self, qapp, monkeypatch):
        """回归：提示 UI 抛异常不能拖住自动收尾的 900ms 定时器。"""
        panel = MagicMock()
        panel.show_warning.side_effect = RuntimeError("模拟 setToolTip(TypeError)")
        win = self._make_window(panel)

        scheduled = []
        monkeypatch.setattr(
            QTimer, "singleShot",
            lambda delay, receiver, callback: scheduled.append((delay, callback)),
        )

        payload = {
            "ok": True,
            "stitched": None,
            "preview_qimage": None,
            "locked_direction": "down",
            "screenshot_count": 4,
            "failed_frame_no": 0,
            "scroll_distance": 0,
            "auto_finish": True,
            "error_detail": None,
            "width": 100,
            "height": 100,
        }
        win._on_stitch_result(payload)  # 不应向外抛异常

        assert win._auto_finish_scheduled is True
        assert len(scheduled) == 1 and scheduled[0][0] == 900
        # 异常被防护吞掉而不是顶到 sys.excepthook（崩溃对话框）

    def test_normal_result_updates_state_and_panel(self, qapp):
        panel = MagicMock()
        win = self._make_window(panel)

        payload = {
            "ok": True,
            "stitched": object(),
            "preview_qimage": None,
            "locked_direction": "down",
            "screenshot_count": 2,
            "failed_frame_no": 0,
            "auto_finish": False,
            "error_detail": None,
            "width": 100,
            "height": 120,
        }
        win._on_stitch_result(payload)

        assert win.stitched_result is payload["stitched"]
        assert len(win.screenshots) == 2
        # 拼接增益（120 - 0）作为等效滚动距离记录
        assert win.scroll_distances == [120]
        assert win.scroll_locked_direction == "down"
        assert win._auto_finish_scheduled is False
        panel.update_count.assert_called_once_with(2)


class TestShowWarningAcceptsLogMsg:

    def test_tooltip_renders_log_msg(self, qapp):
        from core.logger import T
        from stitch.scroll_window import PreviewPanel

        panel = PreviewPanel()
        try:
            panel.show_warning(T("测试警告 {n}", n=1))  # 旧代码在这里 TypeError
            assert panel.warning_icon.toolTip() == "测试警告 1"
        finally:
            panel.deleteLater()

    def test_plain_str_still_works(self, qapp):
        from stitch.scroll_window import PreviewPanel

        panel = PreviewPanel()
        try:
            panel.show_warning("第 3 张图片拼接失败：未找到可靠的重叠区域")
            assert "第 3 张" in panel.warning_icon.toolTip()
        finally:
            panel.deleteLater()
