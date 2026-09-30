"""
钉图缩略图模式

负责缩略图模式的进入、退出、视图更新逻辑。
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QCursor
from core import log_debug
from core.logger import T


class PinThumbnailMode:
    """
    钉图缩略图模式管理器

    状态数据和操作逻辑全部内聚在此类中，
    PinWindow 只需调用 toggle() 并在 resizeEvent 中调用 update_view()。
    """

    DEFAULT_THUMBNAIL_HEIGHT = 100

    def __init__(self, pin_window):
        self._win = pin_window
        self._active = False
        self._size = self._load_height()   # 缩略图高度（像素），宽度按图片宽高比等比得出
        self._prev_geometry = None      # 进入前的窗口几何
        self._scene_center = None       # 缩略图显示的场景中心点
        self._region_rect = None

    def _load_height(self) -> int:
        """从设置读取缩略图高度；读不到就用默认值。"""
        try:
            config = getattr(self._win, "config_manager", None)
            if config is None:
                from settings import get_tool_settings_manager
                config = get_tool_settings_manager()
            return int(config.get_pin_thumbnail_height())
        except Exception:
            return self.DEFAULT_THUMBNAIL_HEIGHT

    def compute_thumbnail_size(self, orig_width: int, orig_height: int) -> tuple:
        """整张图等比缩放到高度 _size，返回 (宽, 高)。"""
        if orig_height <= 0 or orig_width <= 0:
            return (self._size, self._size)
        width = max(3, round(orig_width / orig_height * self._size))
        return (width, self._size)

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def size(self) -> int:
        return self._size

    # ------------------------------------------------------------------
    # 切换
    # ------------------------------------------------------------------

    def toggle(self):
        """切换缩略图模式"""
        if not self._active:
            self._enter()
        else:
            self._exit()

    # ------------------------------------------------------------------
    # 进入缩略图模式
    # ------------------------------------------------------------------

    def enter_region(self, rect):
        """Show a user-selected scene region without changing the source image."""
        if self._active:
            return
        rect = rect.intersected(self._win.canvas.scene.sceneRect())
        if not rect.isEmpty():
            self._enter(rect)

    def _enter(self, region=None):
        win = self._win
        # 保存当前窗口几何
        self._prev_geometry = win.geometry()

        # 获取鼠标全局位置；缩略图以它为中心摆放
        cursor_pos = QCursor.pos()
        window_rect = win.geometry()
        if not window_rect.contains(cursor_pos):
            cursor_pos = window_rect.center()

        # 整张图等比缩放：缩略图中心显示的就是图片中心。
        # 还原时以「缩略图当前位置」为锚放大，场景中心取图片中心才能对得上。
        self._scene_center = QPointF(
            win._orig_size.width() / 2, win._orig_size.height() / 2
        )

        thumb_w, thumb_h = self.compute_thumbnail_size(
            win._orig_size.width(), win._orig_size.height()
        )
        new_x = cursor_pos.x() - thumb_w // 2
        new_y = cursor_pos.y() - thumb_h // 2

        self._region_rect = QRectF(region) if region is not None else None
        if self._region_rect is not None:
            local_rect = win.view.mapFromScene(self._region_rect).boundingRect()
            origin = win.view.viewport().mapToGlobal(local_rect.topLeft())
            self._scene_center = self._region_rect.center()
            win.setGeometry(origin.x(), origin.y(), max(3, local_rect.width()), max(3, local_rect.height()))
        else:
            win.setGeometry(new_x, new_y, thumb_w, thumb_h)

        # 更新视图
        self.update_view()

        # 禁用 OCR 层
        if hasattr(win, '_ocr_mgr'):
            win._ocr_mgr.set_enabled(False)

        # 退出绘制/编辑状态
        if win.canvas and win.canvas.is_editing:
            win.canvas.deactivate_tool()
            # 同步工具栏按钮状态
            if win.toolbar and hasattr(win.toolbar, 'current_tool') and win.toolbar.current_tool:
                for btn in win.toolbar.tool_buttons.values():
                    btn.setChecked(False)
                win.toolbar.current_tool = None

        self._active = True
        win.hover_controls.sync()
        log_debug(
            T("进入缩略图模式: {w}x{h}, 高度 {size}",
              w=thumb_w, h=thumb_h, size=self._size),
            "PinWindow",
        )

    # ------------------------------------------------------------------
    # 退出缩略图模式
    # ------------------------------------------------------------------

    def _exit(self):
        if not self._prev_geometry or not self._scene_center:
            self._active = False
            return

        win = self._win
        prev_size = self._prev_geometry.size()
        current_center = win.geometry().center()

        rel_x = self._scene_center.x() / win._orig_size.width()
        rel_y = self._scene_center.y() / win._orig_size.height()

        new_x = int(current_center.x() - rel_x * prev_size.width())
        new_y = int(current_center.y() - rel_y * prev_size.height())

        # 先重置标志，让 resizeEvent 正常处理
        self._active = False
        self._scene_center = None
        self._prev_geometry = None
        self._region_rect = None

        win.setGeometry(new_x, new_y, prev_size.width(), prev_size.height())
        win._update_view_transform()

        # 恢复 OCR 层
        if hasattr(win, '_ocr_mgr'):
            win._ocr_mgr.set_enabled(True)
            cr = win.content_rect()
            win._ocr_mgr.update_geometry(cr.toRect())

        win.hover_controls.sync()
        win.update_button_positions()

        log_debug(T("退出缩略图模式"), "PinWindow")

    # ------------------------------------------------------------------
    # 视图更新（resizeEvent 中调用）
    # ------------------------------------------------------------------

    def update_view(self):
        """更新缩略图视图，显示以场景中心点为中心的区域"""
        win = self._win
        if not win.view or not self._scene_center:
            return

        if self._region_rect is not None:
            win.view.resetTransform()
            win.view.fitInView(self._region_rect, Qt.AspectRatioMode.KeepAspectRatio)
            return

        # 整张图等比缩放适配缩略图窗口。窗口宽高比与图片一致，
        # fitInView 恰好铺满，不会留边也不会裁切。
        win.view.resetTransform()
        scene_rect = QRectF(
            0, 0, win._orig_size.width(), win._orig_size.height()
        )
        win.view.fitInView(scene_rect, Qt.AspectRatioMode.KeepAspectRatio)
