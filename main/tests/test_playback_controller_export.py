# -*- coding: utf-8 -*-
"""GIF 导出的重入防护测试。

合成走 ComposerProgressDialog 的嵌套事件循环，期间点击仍会派发：控制器
必须有 _composing 守卫，二次导出请求直接忽略；工具栏按钮在导出期间禁用，
从源头挡住第二次点击。
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


def _make_controller(monkeypatch):
    from gif.playback_controller import PlaybackController

    ctrl = PlaybackController()
    ctrl._recorder = MagicMock()
    ctrl._recorder.frames = [object()] * 5
    ctrl._recorder.store = MagicMock()
    toolbar = MagicMock()
    ctrl._playback_toolbar = toolbar
    ctrl._cursor_export_enabled = False
    monkeypatch.setattr(ctrl, "_get_trim_range", lambda: (0, 4))
    return ctrl, toolbar


class TestExportReentry:
    def test_second_request_during_compose_is_ignored(self, qapp, monkeypatch):
        import gif.playback_controller as pc

        ctrl, toolbar = _make_controller(monkeypatch)
        calls = []

        # run_compose 阻塞期间模拟"用户再点一次"：嵌套调用 _compose_and_export
        def fake_run_compose(**kwargs):
            assert ctrl._composing is True
            ctrl._compose_and_export("/tmp/again.gif", copy_to_clipboard=True)
            calls.append("compose")
            return "/tmp/again.gif"

        monkeypatch.setattr(pc.ComposerProgressDialog, "run_compose", staticmethod(fake_run_compose))

        ctrl._compose_and_export("/tmp/out.gif", copy_to_clipboard=True)

        assert calls == ["compose"], "重入的导出请求必须被忽略"
        assert ctrl._composing is False, "导出结束后守卫必须复位"
        toolbar.set_export_busy.assert_any_call(True)
        toolbar.set_export_busy.assert_any_call(False)

    def test_buttons_restored_even_when_compose_raises(self, qapp, monkeypatch):
        import gif.playback_controller as pc

        ctrl, toolbar = _make_controller(monkeypatch)

        def broken_compose(**kwargs):
            raise RuntimeError("rust exploded")

        monkeypatch.setattr(pc.ComposerProgressDialog, "run_compose", staticmethod(broken_compose))
        with pytest.raises(RuntimeError):
            ctrl._compose_and_export("/tmp/out.gif", copy_to_clipboard=False)

        assert ctrl._composing is False, "异常路径也要复位守卫"
        toolbar.set_export_busy.assert_any_call(False)
