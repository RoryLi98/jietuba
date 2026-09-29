# -*- coding: utf-8 -*-
"""长截图"滚动 → 抓帧 → 拼接 → 预览更新"全链路回归测试。

背景：冻结包里用户反馈"滚动后预览不更新，完成时只有初始 1 帧"，且日志
零报错。单测只覆盖了 worker 与结果槽，没覆盖 滚轮信号 → 冷却定时器 →
_do_capture → worker → 排队信号 → 预览面板 这条真实主线程链路。

本测试构造真实 ScrollCaptureWindow（真实定时器、真实 worker 线程、真实
排队信号、真实预览面板），只把 QScreen.grabWindow 打桩成返回预制帧，
然后模拟滚轮回调的完整调用，断言预览与帧计数逐帧推进。
"""
import time

import pytest
from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QColor, QPainter, QScreen, QPixmap

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


def _pump_until(qapp, condition, timeout_s=6.0):
    """驱动事件循环直到 condition 成立（让 QTimer / 排队信号有机会跑）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def window(qapp):
    from stitch.scroll_window import ScrollCaptureWindow

    win = ScrollCaptureWindow(QRect(0, 0, 120, 100), None)
    yield win
    win._cleanup()


class TestLiveChain:

    def test_scroll_updates_preview_frame_by_frame(self, qapp, window, monkeypatch):
        """模拟滚轮回调 → 冷却定时器 → 抓帧（桩）→ worker → 预览更新。"""
        assert window.capture_timer is not None

        frames = iter([
            _make_pixmap(80),   # 初始帧（页面 0..100，带子在 80..100）
            _make_pixmap(60),   # 向下滚 20px（带子到 60..80）
        ])
        monkeypatch.setattr(
            QScreen, "grabWindow",
            lambda self, win_id, x=0, y=0, w=0, h=0: next(frames),
        )

        # 滚轮钩子在 pynput 线程里做的事：emit scroll_detected。
        # 这里直接在测试（主线程）里 emit，走同一条 QueuedConnection。
        window.scroll_detected.emit(500)

        # 150ms 冷却后 _do_capture 抓帧并提交 worker；结果经排队信号回主线程
        assert _pump_until(
            qapp,
            lambda: len(window.screenshots) >= 1
            and window.preview_panel.preview_label.pixmap() is not None,
        ), "第 1 帧后预览未更新"
        assert window.preview_panel.count_label.text() == "1"

        # 第二次滚动：应触发第二次抓帧与增量拼接
        window.scroll_detected.emit(500)
        assert _pump_until(
            qapp,
            lambda: len(window.screenshots) >= 2
            and window.stitched_result is not None
            and window.stitched_result.size[1] > 100,
        ), "第 2 帧后拼接结果未增长（用户看到的'滚动无反应'）"
        assert window.preview_panel.count_label.text() == "2"

    def test_three_scrolls_reach_preview(self, qapp, window, monkeypatch):
        """多帧连续滚动：每一帧都必须推进预览，不允许静默停摆。"""
        bands = [80, 60, 40, 20]
        frames = iter([_make_pixmap(b) for b in bands])
        monkeypatch.setattr(
            QScreen, "grabWindow",
            lambda self, win_id, x=0, y=0, w=0, h=0: next(frames),
        )

        for i in range(3):
            window.scroll_detected.emit(500)
            # 每次滚动之间留出超过冷却（150ms）的间隔，模拟真实的
            # "滚一下→页面滚→再滚一下"节奏
            assert _pump_until(
                qapp,
                lambda: len(window.screenshots) >= i + 1,
            ), f"第 {i + 1} 次滚动后帧计数未推进"
            time.sleep(0.2)
            qapp.processEvents()
        assert window.stitched_result.size[1] >= 100 + 40  # 至少 2 次成功增量

    def test_real_os_wheel_event_through_global_hook(self, qapp, window, monkeypatch):
        """真实 OS 滚轮事件 → 真实全局钩子（pynput 线程）→ 信号 → 抓帧。

        之前的测试直接在主线程 emit scroll_detected，绕过了钩子线程。
        这里用 pynput 注入真实滚轮事件（SendInput 走系统输入队列，全局钩子
        一定收得到），覆盖"钩子线程 emit 信号"这条真实路径。
        """
        pynput_mouse = pytest.importorskip("pynput.mouse", reason="需要 pynput")

        frames = iter([_make_pixmap(80), _make_pixmap(60)])
        monkeypatch.setattr(
            QScreen, "grabWindow",
            lambda self, win_id, x=0, y=0, w=0, h=0: next(frames),
        )

        # 窗口 rect 是 (0,0,120,100)：把光标移进截图区域再滚动
        controller = pynput_mouse.Controller()
        controller.position = (60, 50)
        time.sleep(0.1)

        # 等钩子就绪（_init_listener_bg 在后台线程启动）
        assert _pump_until(qapp, lambda: window.mouse_listener is not None), \
            "全局滚轮监听器未启动"

        controller.scroll(0, -3)  # 真实 OS 滚轮事件（向下）

        assert _pump_until(
            qapp,
            lambda: len(window.screenshots) >= 1,
            timeout_s=8.0,
        ), "真实滚轮事件未触发抓帧（钩子→信号→定时器链路断了）"
