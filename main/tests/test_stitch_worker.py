# -*- coding: utf-8 -*-
"""长截图像素管线（_StitchWorker）与自动完成路径的回归测试。

背景：自动完成路径曾把 T() 的 LogMsg 直接传给 PreviewPanel.show_warning →
setToolTip(LogMsg) 抛 TypeError。旧代码里它被 _do_capture 的 try/except 吞掉
（自动收尾同样静默失效）；改成排队槽后异常直接顶到 sys.excepthook，表现为
滚动到底后弹崩溃日志、永远不出图。

这组测试锁住三件事：
1. worker 线程用真实帧走完 方向变换/哈希/行签名对齐拼接/预览缩略图 全流程；
2. 连续两帧重复时 auto_finish 必须置位（自动完成的触发源）；
3. 即使提示 UI 抛异常，900ms 的自动收尾定时器也必须已经排上。
"""
import threading
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QRect, QTimer
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
        assert (first["width"], first["height"]) == (W, H)
        assert first["preview_qimage"] is not None
        assert not first["preview_qimage"].isNull()

        second = _submit_and_wait(worker, _make_frame(60))
        assert second["ok"] is True
        assert second["screenshot_count"] == 2
        # 向下滚 20px：新帧顶行落在画布第 20 行 → 结果 = 100 + 20 = 120 高
        assert (second["width"], second["height"]) == (W, H + 20)
        # 新帧接在画布下方 → 方向自动锁为 down
        assert second["locked_direction"] == "down"

        worker.stop()

    def test_flags_auto_finish_on_two_duplicate_frames(self):
        """连续 2 帧画面不变（到底）→ 第二次重复时 auto_finish=True。

        自动完成的门槛是「已有 ≥3 张有效拼接」，所以重复帧之前要先滚出
        第 3 张有效帧（重复帧整幅落在画布内，不计入总数）。
        """
        from stitch.scroll_window import _StitchWorker

        worker = _StitchWorker(
            scroll_direction="vertical",
            locked_direction=None,
            duplicate_threshold=0.95,
            emit_fn=lambda payload: None,
        )

        _submit_and_wait(worker, _make_frame(80))   # 初始帧
        _submit_and_wait(worker, _make_frame(60))   # 滚 20px
        _submit_and_wait(worker, _make_frame(40))   # 再滚 20px → 第 3 张有效帧
        dup1 = _submit_and_wait(worker, _make_frame(40))
        assert dup1["auto_finish"] is False          # 只差 1 次：观望
        dup2 = _submit_and_wait(worker, _make_frame(40))
        assert dup2["auto_finish"] is True           # 连续 2 帧：收尾

        worker.stop()


def _make_bare_window(preview_panel):
    """用 __new__ 造一个只带 _apply_stitch_result 所需状态的窗口（不触发抓屏）。"""
    from stitch.scroll_window import ScrollCaptureWindow

    win = ScrollCaptureWindow.__new__(ScrollCaptureWindow)
    win.screenshots = []
    win.stitched_result = None
    win.scroll_distances = []
    win.scroll_locked_direction = None
    win._last_stitch_height = 0
    win._auto_finish_scheduled = False
    win.preview_warning_active = False
    win.scroll_direction = "vertical"
    win._finishing = False
    win._cancel_requested = False
    win.preview_panel = preview_panel
    win._position_preview_panel = lambda: None
    # 注意：_show_preview_warning / _clear_preview_warning 必须用真实实现——
    # 感叹号清不掉的回归正是 preview_warning_active 没被置位导致的
    return win


class TestAutoFinishPath:

    def _make_window(self, preview_panel):
        return _make_bare_window(preview_panel)

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
            "preview_qimage": None,
            "locked_direction": "down",
            "screenshot_count": 4,
            "failed_frame_no": 0,
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

        # 全图不再随回包走（按节流出缩略图），成图在收尾时才从 worker 取
        assert win.stitched_result is None
        assert len(win.screenshots) == 2
        # 拼接增益（120 - 0）作为等效滚动距离记录
        assert win.scroll_distances == [120]
        assert win.scroll_locked_direction == "down"
        assert win._auto_finish_scheduled is False
        panel.update_count.assert_called_once_with(2)

    def test_warning_icon_shown_then_cleared(self, qapp, monkeypatch):
        """worker 上报到底 → 预览右上角感叹号；下一帧正常拼接 → 必须清掉。

        回归：该路径曾直接调 preview_panel.show_warning（绕过
        _show_preview_warning），preview_warning_active 没置位，于是后续
        所有 _clear_preview_warning 都早退——感叹号挂住，用户继续滚动也清不掉。
        """
        monkeypatch.setattr(QTimer, "singleShot", lambda delay, receiver, callback: None)
        panel = MagicMock()
        win = self._make_window(panel)

        base = {
            "ok": True,
            "preview_qimage": None,
            "locked_direction": "down",
            "failed_frame_no": 0,
            "error_detail": None,
            "width": 100,
            "height": 120,
        }
        win._on_stitch_result({**base, "screenshot_count": 3, "auto_finish": True})
        assert win.preview_warning_active is True, "到底提示未显示"
        assert panel.show_warning.called

        win._on_stitch_result({**base, "screenshot_count": 4, "auto_finish": False})
        assert win.preview_warning_active is False, "后续正常帧没清掉感叹号"
        panel.clear_warning.assert_called_once()


def _refresh_and_wait(worker, timeout=10.0):
    done = threading.Event()
    results = []
    worker._emit = lambda payload: (results.append(payload), done.set())
    worker.submit_preview_refresh()
    assert done.wait(timeout), "worker 未在超时内返回结果"
    return results[0]


class TestPreviewRefreshPath:
    """空闲补刷（submit_preview_refresh）的回包必须带 preview_only。

    回归：这个键曾只被主线程 _apply_stitch_result 读、worker 从不写，
    于是早退分支是死代码——空闲补刷被当成"又拼了一帧"：重复打 📸 第 N 张
    日志、往 scroll_distances 里塞 0 增益、update_count 重复调用，
    还会 _clear_preview_warning() 把"即将自动完成拼接"的感叹号提前抹掉。
    """

    def test_worker_payload_flags_preview_only(self):
        from stitch.scroll_window import _StitchWorker

        worker = _StitchWorker(
            scroll_direction="vertical",
            locked_direction=None,
            duplicate_threshold=0.95,
            emit_fn=lambda payload: None,
        )
        try:
            first = _submit_and_wait(worker, _make_frame(80))
            assert first["preview_only"] is False

            refresh = _refresh_and_wait(worker)
            assert refresh["preview_only"] is True
            assert refresh["ok"] is True
            # 纯预览刷新：帧计数原样，不得当成新帧
            assert refresh["screenshot_count"] == first["screenshot_count"] == 1
            assert refresh["preview_qimage"] is not None
            assert not refresh["preview_qimage"].isNull()
        finally:
            worker.stop()

    def test_preview_only_payload_skips_all_stitch_state_updates(self, qapp):
        panel = MagicMock()
        win = _make_bare_window(panel)
        # 到底感叹号正在显示：补刷绝不能把它清掉
        win.preview_warning_active = True

        win._on_stitch_result({
            "ok": True,
            "preview_only": True,
            "preview_qimage": QImage(8, 8, QImage.Format.Format_RGBA8888),
            "locked_direction": "down",
            "screenshot_count": 99,   # 只允许传给面板做显示，不得进 screenshots/scroll_distances
            "failed_frame_no": 0,
            "auto_finish": False,
            "error_detail": None,
            "width": 120,
            "height": 100,
        })

        assert win.scroll_distances == []
        assert win.screenshots == []
        assert win.preview_warning_active is True
        assert win._auto_finish_scheduled is False
        panel.update_count.assert_not_called()
        panel.clear_warning.assert_not_called()
        panel.update_preview.assert_called_once()


class TestCancelDuringFinish:
    """收尾期间点取消必须真的取消。

    回归：_flush_pending_stitch 会 pump 事件循环（20s 排水 + 10s finalize），
    期间取消按钮仍可点，但 _on_cancel 撞上 `_finishing` 早退直接 return——
    _cancel_requested 永远置不上，flush 里的三处取消检查全是死代码，
    收尾随后照常保存 + 复制 + 关窗，用户的取消被静默吞掉。
    """

    def test_cancel_click_while_finishing_only_records_the_request(self, qapp):
        win = _make_bare_window(MagicMock())
        win._finishing = True          # 收尾进行中（事件泵已接管）
        win._cleanup = MagicMock(side_effect=AssertionError("收尾途中不该就地清理"))

        win._on_cancel()

        assert win._cancel_requested is True
        win._cleanup.assert_not_called()

    def test_finish_observes_the_cancel_request_and_aborts(self, qapp):
        win = _make_bare_window(MagicMock())
        aborted, saved = [], []
        win._abandon_as_cancelled = lambda: aborted.append("abandoned")
        win._save_result = lambda: saved.append("saved")

        def flush_during_which_user_cancels():
            assert win._finishing is True
            win._on_cancel()           # 事件泵里点下的取消按钮

        win._flush_pending_stitch = flush_during_which_user_cancels

        win._on_finish()

        assert aborted == ["abandoned"]
        assert saved == [], "被取消的收尾不该再保存/复制/关窗"

    def test_pin_observes_the_cancel_request_and_aborts(self, qapp):
        win = _make_bare_window(MagicMock())
        aborted = []
        win._abandon_as_cancelled = lambda: aborted.append("abandoned")
        win._flush_pending_stitch = lambda: setattr(win, "_cancel_requested", True)

        win._on_pin()

        assert aborted == ["abandoned"]

    def test_failed_pin_reenables_the_buttons(self, qapp):
        """钉图没做成不能把 _finishing 永久挂在 True 上——那样三个按钮全死。"""
        win = _make_bare_window(MagicMock())
        win._finishing = True
        win._auto_finish_scheduled = True
        timer = MagicMock()
        win._watch_timer = timer

        win._resume_watching_after_failed_pin()

        assert win._finishing is False
        assert win._auto_finish_scheduled is False
        timer.start.assert_called_once_with(win._WATCH_IDLE_MS)


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
