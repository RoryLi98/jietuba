# -*- coding: utf-8 -*-
"""窗口显示时跳过整表重载的判据测试。

on_window_show 在 (查询参数, 显示设置, 数据指纹) 三者都没变时必须跳过
load_history——空开空关是剪贴板窗口最高频的动作，全量重查重建是它的
主要开销；任一变化（含指纹缺失=回退）都必须照常重载。
"""
import pytest

from clipboard.controllers.clipboard_controller import ClipboardController


class _FingerprintManager:
    """带指纹的替身：get_history 不真的查库，只记录调用次数。"""

    def __init__(self, fingerprint=(7, 100)):
        self.fingerprint = fingerprint
        self.load_calls = 0
        self.history_calls = 0

    def get_history_fingerprint(self):
        return self.fingerprint

    def get_history(self, **kwargs):
        self.history_calls += 1
        return []

    def get_by_group(self, group_id=None, offset=0, limit=50, search=None):
        self.history_calls += 1
        return []


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(ClipboardController, "_load_settings", lambda self: None)
    manager = _FingerprintManager()
    ctrl = ClipboardController(manager)
    # _load_more_items 桩：走真实 load_history 的重置逻辑，并按真实实现记录指纹
    def fake_load_more():
        manager.load_calls += 1  # 一次"加载" = load_history → _load_more_items
        ctrl._last_load_signature = (
            (ctrl._query_signature(), ctrl._last_display_sig),
            manager.get_history_fingerprint())

    monkeypatch.setattr(ctrl, "_load_more_items", fake_load_more)
    return ctrl, manager


class TestSkipReloadOnShow:
    def test_unchanged_skips_reload(self, controller):
        ctrl, manager = controller
        ctrl.on_window_show(display_sig=("meta", 17, "small", 15, 20, "dark"))
        ctrl.on_window_show(display_sig=("meta", 17, "small", 15, 20, "dark"))
        assert manager.load_calls == 1, "第二次显示时数据与设置都没变，应跳过重载"

    def test_fingerprint_change_reloads(self, controller):
        ctrl, manager = controller
        ctrl.on_window_show(display_sig=None)
        manager.fingerprint = (8, 101)  # 隐藏期间来了新内容
        ctrl.on_window_show(display_sig=None)
        assert manager.load_calls == 2

    def test_display_sig_change_reloads(self, controller):
        ctrl, manager = controller
        ctrl.on_window_show(display_sig=("meta", 17, "small", 15, 20, "dark"))
        ctrl.on_window_show(display_sig=("meta", 18, "small", 15, 20, "dark"))  # 字号改了
        assert manager.load_calls == 2

    def test_query_param_change_reloads(self, controller):
        ctrl, manager = controller
        ctrl.on_window_show(display_sig=None)
        ctrl._search_text = "apple"
        ctrl.on_window_show(display_sig=None)
        assert manager.load_calls == 2

    def test_missing_fingerprint_falls_back_to_reload(self, monkeypatch):
        """旧版 manager 没有指纹查询：必须回退为每次都重载，不能跳过。"""

        class _NoFingerprintManager(_FingerprintManager):
            def get_history_fingerprint(self):
                raise AttributeError("旧版没有这个方法")

        monkeypatch.setattr(ClipboardController, "_load_settings", lambda self: None)
        manager = _NoFingerprintManager()
        ctrl = ClipboardController(manager)
        monkeypatch.setattr(ctrl, "load_history", lambda: setattr(manager, "load_calls", manager.load_calls + 1))

        ctrl.on_window_show(display_sig=None)
        ctrl.on_window_show(display_sig=None)
        assert manager.load_calls == 2, "指纹不可用时不许跳过（保守方向）"
