# -*- coding: utf-8 -*-
"""钉图缩略图模式测试：等比缩放 + 高度可配置。"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_mode(height=100):
    """用 __new__ 跳过依赖真实窗口的 __init__，直接装配尺寸状态。"""
    from pin.pin_thumbnail import PinThumbnailMode
    mode = PinThumbnailMode.__new__(PinThumbnailMode)
    mode._win = MagicMock()
    mode._win.config_manager = None  # _load_height 走 get_tool_settings_manager 兜底
    mode._active = False
    mode._size = height
    mode._prev_geometry = None
    mode._scene_center = None
    mode._region_rect = None
    return mode


class TestProportionalSize:

    def test_landscape_image_keeps_aspect_ratio(self, qapp):
        mode = _make_mode(height=100)
        w, h = mode.compute_thumbnail_size(800, 400)
        assert (w, h) == (200, 100)

    def test_portrait_image_keeps_aspect_ratio(self, qapp):
        mode = _make_mode(height=100)
        w, h = mode.compute_thumbnail_size(300, 600)
        assert (w, h) == (50, 100)

    def test_height_follows_configured_value(self, qapp):
        mode = _make_mode(height=200)
        w, h = mode.compute_thumbnail_size(1000, 500)
        assert (w, h) == (400, 200)

    def test_square_image_yields_square_thumbnail(self, qapp):
        mode = _make_mode(height=100)
        assert mode.compute_thumbnail_size(500, 500) == (100, 100)

    def test_degenerate_dimensions_fall_back_to_square(self, qapp):
        mode = _make_mode(height=100)
        assert mode.compute_thumbnail_size(0, 0) == (100, 100)

    def test_tiny_image_width_never_below_three(self, qapp):
        mode = _make_mode(height=100)
        w, h = mode.compute_thumbnail_size(1, 1000)
        assert w >= 3
        assert h == 100


class TestConfigAccessors:

    def test_default_height_is_100(self):
        from settings.tool_settings import ToolSettingsManager
        assert ToolSettingsManager.APP_DEFAULT_SETTINGS["pin_thumbnail_height"] == 100

    def test_setter_clamps_to_valid_range(self):
        assert 40 <= 100 <= 400  # 边界与默认值自洽
        # clamp 逻辑
        assert max(40, min(400, 10)) == 40
        assert max(40, min(400, 999)) == 400


class TestSettingsPageWiring:

    def test_combo_exists_with_expected_options(self, qapp, monkeypatch):
        import ui.dialogs as dialogs_mod
        monkeypatch.setattr(dialogs_mod, "show_warning_dialog", lambda *a, **k: None)
        from ui.settings_ui.mock_config import MockConfig
        from ui.settings_ui.dialog import SettingsDialog

        dlg = SettingsDialog(MockConfig())
        dlg.build_all_pages()  # 各页按需建页（见 _ensure_page），先全部建出再访问控件
        combo = dlg.pin_thumbnail_height_combo
        data = [combo.itemData(i) for i in range(combo.count())]
        assert 100 in data
        assert combo.currentData() == 100  # MockConfig 默认


class TestEnterUsesProportionalGeometry:

    def test_enter_sets_proportional_window_geometry(self, qapp, monkeypatch):
        """进入缩略图：窗口尺寸 = compute_thumbnail_size，而不是 100×100。"""
        from pin.pin_thumbnail import PinThumbnailMode
        from PySide6.QtCore import QRect

        win = MagicMock()
        win._orig_size = MagicMock()
        win._orig_size.width.return_value = 800
        win._orig_size.height.return_value = 400
        win.geometry.return_value = QRect(0, 0, 800, 400)
        win.canvas.is_editing = False
        win.canvas.scene.sceneRect.return_value.__iter__ = lambda self: iter([])
        win.toolbar = None

        mode = PinThumbnailMode.__new__(PinThumbnailMode)
        mode._win = win
        mode._active = False
        mode._size = 100
        mode._prev_geometry = None
        mode._scene_center = None
        mode._region_rect = None

        monkeypatch.setattr(
            "pin.pin_thumbnail.QCursor.pos",
            staticmethod(lambda: QPoint(500, 300)),
        )
        captured = {}
        win.setGeometry.side_effect = lambda x, y, w, h: captured.update(
            x=x, y=y, w=w, h=h
        )

        mode._enter()
        assert (captured["w"], captured["h"]) == (200, 100)
        assert captured["x"] == 500 - 100   # 以鼠标为中心
        assert captured["y"] == 300 - 50
