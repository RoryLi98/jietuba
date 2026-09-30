# -*- coding: utf-8 -*-
"""
钉图缩略图模式测试

PinThumbnailMode 把钉图缩成鼠标位置上的一个小方块，再按原样还原。
它对 PinWindow 的依赖都是很窄的接口调用，所以可以用假窗口驱动，
不必创建真实的置顶窗口。

要紧的行为：
- 进入时以鼠标所在的画面位置为取景中心；鼠标不在窗口上时退回画面中心
- 取景框必须被夹在图像范围内，否则缩略图里会露出图像外的空白
- 进入 → 退出应还原成原来的尺寸，且画面中心仍落在同一处
- 进入时要退出编辑状态、关掉 OCR 层，退出时再恢复
- 模式切换后要让悬停控件重新计算显隐，否则缩略图上会浮着工具栏和按钮
"""
import pytest
from PySide6.QtCore import QPoint, QRect, QSize, QRectF
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class FakeView:
    def __init__(self):
        self.reset_calls = 0
        self.fitted_rects = []

    def resetTransform(self):
        self.reset_calls += 1

    def fitInView(self, rect, mode):
        self.fitted_rects.append(QRectF(rect))


class FakeCanvas:
    def __init__(self, is_editing=False):
        self.is_editing = is_editing
        self.deactivated = 0

    def deactivate_tool(self):
        self.deactivated += 1
        self.is_editing = False


class FakeToolbar:
    def __init__(self):
        self.current_tool = None
        self.tool_buttons = {}


class FakeHoverControls:
    """记录每次 sync() 时缩略图模式是否已生效"""

    def __init__(self):
        self.mode = None
        self.synced_with_active = []

    def sync(self):
        self.synced_with_active.append(self.mode.active)


class FakeOCRManager:
    def __init__(self):
        self.enabled_calls = []
        self.geometry_updates = []

    def set_enabled(self, enabled):
        self.enabled_calls.append(enabled)

    def update_geometry(self, rect):
        self.geometry_updates.append(rect)


class FakePinWindow:
    """只实现 PinThumbnailMode 用到的那部分 PinWindow 接口"""

    def __init__(self, geometry=QRect(200, 100, 400, 300),
                 orig_size=QSize(800, 600), editing=False, toolbar=True):
        self._geometry = QRect(geometry)
        self._orig_size = orig_size
        self.view = FakeView()
        self.canvas = FakeCanvas(editing)
        self.toolbar = FakeToolbar() if toolbar else None
        self._ocr_mgr = FakeOCRManager()
        self.hover_controls = FakeHoverControls()

        self.transform_updates = 0
        self.button_position_updates = 0

    # -- 几何 --
    def geometry(self):
        return QRect(self._geometry)

    def setGeometry(self, x, y, w, h):
        self._geometry = QRect(x, y, w, h)

    def mapFromGlobal(self, pos):
        return QPoint(pos.x() - self._geometry.x(), pos.y() - self._geometry.y())

    def content_rect(self):
        return QRectF(0, 0, self._geometry.width(), self._geometry.height())

    # -- 回调 --
    def _update_view_transform(self):
        self.transform_updates += 1

    def update_button_positions(self):
        self.button_position_updates += 1


@pytest.fixture
def mode(qapp):
    from pin.pin_thumbnail import PinThumbnailMode

    def _make(win=None, **kwargs):
        win = win or FakePinWindow(**kwargs)
        m = PinThumbnailMode(win)
        win.hover_controls.mode = m
        return m

    return _make


@pytest.fixture
def cursor_at(monkeypatch):
    def _set(x, y):
        monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: QPoint(x, y)))
    return _set


# ============================================================================
# 初始状态
# ============================================================================

class TestInitialState:
    def test_starts_inactive(self, mode):
        assert mode().active is False

    def test_default_thumbnail_size(self, mode):
        assert mode().size == 100


# ============================================================================
# 进入缩略图模式
# ============================================================================

class TestEnter:
    def test_toggle_activates_the_mode(self, mode, cursor_at):
        cursor_at(400, 250)
        m = mode()
        m.toggle()
        assert m.active is True

    def test_window_shrinks_to_the_proportional_thumbnail_size(self, mode, cursor_at):
        """整张图等比缩放：800x600 的原图在高度 100 时宽 133。"""
        cursor_at(400, 250)
        win = FakePinWindow()
        m = mode(win)

        m.toggle()

        assert win.geometry().size() == QSize(133, 100)

    def test_thumbnail_is_centred_on_the_cursor(self, mode, cursor_at):
        cursor_at(400, 250)
        win = FakePinWindow()
        m = mode(win)

        m.toggle()

        # 等比宽度 133 → x = 400 - 66；高度 100 → y = 250 - 50
        assert win.geometry().center() in (
            QPoint(400, 250), QPoint(400, 249), QPoint(400, 251),
        )

    def test_scene_center_is_always_the_image_centre(self, mode, cursor_at):
        """整图视图下缩略图中心显示的就是图片中心，还原锚点据此计算。"""
        win = FakePinWindow(geometry=QRect(200, 100, 400, 300),
                            orig_size=QSize(800, 600))
        cursor_at(200 + 200, 100 + 150)
        m = mode(win)

        m.toggle()

        assert m._scene_center.x() == pytest.approx(400, abs=1)
        assert m._scene_center.y() == pytest.approx(300, abs=1)

    def test_cursor_position_only_affects_placement_not_the_view(self, mode, cursor_at):
        win = FakePinWindow(geometry=QRect(200, 100, 400, 300),
                            orig_size=QSize(800, 600))
        cursor_at(5000, 5000)
        m = mode(win)
        m.toggle()
        rect = win.view.fitted_rects[-1]
        # 视图始终覆盖整张图
        assert rect.left() == pytest.approx(0)
        assert rect.top() == pytest.approx(0)
        assert rect.width() == pytest.approx(800)
        assert rect.height() == pytest.approx(600)

    def test_entering_disables_the_ocr_layer(self, mode, cursor_at):
        win = FakePinWindow()
        cursor_at(400, 250)
        m = mode(win)
        m.toggle()
        assert win._ocr_mgr.enabled_calls == [False]

    def test_entering_leaves_edit_mode(self, mode, cursor_at):
        win = FakePinWindow(editing=True)
        cursor_at(400, 250)
        m = mode(win)
        m.toggle()
        assert win.canvas.deactivated == 1

    def test_entering_resyncs_hover_controls_once_the_mode_is_on(self, mode, cursor_at):
        cursor_at(400, 250)
        win = FakePinWindow()
        mode(win).toggle()

        assert win.hover_controls.synced_with_active == [True]

    def test_entering_works_without_a_toolbar(self, mode, cursor_at):
        win = FakePinWindow(toolbar=False)
        cursor_at(400, 250)
        m = mode(win)
        m.toggle()
        assert m.active is True


class TestWholeImageView:
    """缩略图显示整张图（等比缩放），不再有取景裁剪与夹取。"""

    def _enter_at(self, mode, cursor_at, cursor, orig=QSize(800, 600)):
        win = FakePinWindow(orig_size=orig)
        cursor_at(*cursor)
        m = mode(win)
        m.toggle()
        return win, m

    def test_the_whole_image_is_fitted(self, mode, cursor_at):
        win, _m = self._enter_at(mode, cursor_at, (400, 250))
        rect = win.view.fitted_rects[-1]
        assert rect.width() == pytest.approx(800)
        assert rect.height() == pytest.approx(600)

    def test_small_images_are_fitted_the_same_way(self, mode, cursor_at):
        """原图比缩略图还小时同样整图显示"""
        win, _m = self._enter_at(mode, cursor_at, (400, 250), orig=QSize(50, 40))
        rect = win.view.fitted_rects[-1]
        assert rect.width() == pytest.approx(50)
        assert rect.height() == pytest.approx(40)

    def test_scene_centre_is_the_image_centre(self, mode, cursor_at):
        _win, m = self._enter_at(mode, cursor_at, (201, 101),
                                 orig=QSize(50, 40))
        assert m._scene_center.x() == pytest.approx(25)
        assert m._scene_center.y() == pytest.approx(20)


class TestExit:
    def test_toggle_twice_returns_to_normal(self, mode, cursor_at):
        cursor_at(400, 250)
        m = mode()
        m.toggle()
        m.toggle()
        assert m.active is False

    def test_original_size_is_restored(self, mode, cursor_at):
        cursor_at(400, 250)
        win = FakePinWindow(geometry=QRect(200, 100, 400, 300))
        m = mode(win)

        m.toggle()
        m.toggle()

        assert win.geometry().size() == QSize(400, 300)

    def test_exiting_restores_the_ocr_layer(self, mode, cursor_at):
        cursor_at(400, 250)
        win = FakePinWindow()
        m = mode(win)

        m.toggle()
        m.toggle()

        assert win._ocr_mgr.enabled_calls == [False, True]
        assert len(win._ocr_mgr.geometry_updates) == 1

    def test_exiting_resyncs_hover_controls_once_the_mode_is_off(self, mode, cursor_at):
        cursor_at(400, 250)
        win = FakePinWindow()
        m = mode(win)

        m.toggle()
        m.toggle()

        assert win.hover_controls.synced_with_active == [True, False]
        assert win.button_position_updates == 1

    def test_exit_state_is_cleared(self, mode, cursor_at):
        cursor_at(400, 250)
        m = mode()
        m.toggle()
        m.toggle()
        assert m._scene_center is None
        assert m._prev_geometry is None

    def test_exiting_without_having_entered_is_safe(self, mode):
        """状态不完整时直接退出只应复位标志，不能抛异常"""
        m = mode()
        m._active = True
        m._exit()
        assert m.active is False

    def test_round_trip_restores_size_and_anchors_the_image_centre(self, mode, cursor_at):
        """
        缩起来再放开：恢复原始大小，且图片中心落回缩略图中心的位置——
        整图视图下缩略图中心显示的就是图片中心，锚点语义自洽。
        """
        win = FakePinWindow(geometry=QRect(200, 100, 400, 300),
                            orig_size=QSize(800, 600))
        cursor_at(200 + 100, 100 + 75)
        m = mode(win)

        m.toggle()
        thumb_centre = win.geometry().center()
        m.toggle()

        restored = win.geometry()
        assert restored.size() == QSize(400, 300)
        # 图片中心应落在缩略图中心所在的位置
        assert restored.center().x() == pytest.approx(thumb_centre.x(), abs=1)
        assert restored.center().y() == pytest.approx(thumb_centre.y(), abs=1)
