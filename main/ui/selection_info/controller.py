# -*- coding: utf-8 -*-
"""
选区信息面板 —— 逻辑控制器

职责：
  1. 监听 SelectionModel 信号，驱动 panel 跟随 / 显示隐藏
  2. 将按钮操作委托给独立逻辑模块：
     - 锁定纵横比 → lock_ratio.LockRatioLogic
     - 圆角截图   → rounded_corners.RoundedCornersLogic
     - 描边/阴影  → border_shadow.BorderShadowLogic
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QThread, QTimer, Signal
from PySide6.QtGui import QImage
from core import log_debug, T
from capture.capture_service import CaptureService

from .hook_manager import HookManager
from .lock_ratio import LockRatioLogic
from .rounded_corners import RoundedCornersLogic
from .border_shadow import BorderShadowLogic


class _BackgroundRefreshWorker(QThread):
    """长按刷新的抓帧线程：同步截屏不再占死 GUI 线程。

    CaptureService 线程安全（HDR 会话在专用线程、mss BitBlt 可任意线程），
    quick capture 与主截图均已验证同一路子。空图表示本轮失败，调用方据此
    释放 in-flight 标记。
    """

    captured = Signal(object)

    def __init__(self, cursor):
        super().__init__()
        self._cursor = cursor
        self._service = CaptureService()

    def run(self):
        try:
            image, _rect = self._service.capture_all_screens(self._cursor)
        except Exception as e:
            log_debug(T("刷新背景失败: {e}", e=e))
            image = QImage()
        self.captured.emit(image)


class SelectionInfoController:
    """选区信息面板的逻辑控制器"""

    def __init__(self, panel, selection_model, selection_item,
                 export_service, mask_overlay, parent_widget,
                 config_manager=None):
        """
        Args:
            panel:           SelectionInfoPanel  (UI)
            selection_model:  SelectionModel
            selection_item:   SelectionItem       (手柄 + 拖拽约束)
            export_service:   ExportService       (导出后处理)
            mask_overlay:     MaskOverlayWidget   (圆角预览)
            parent_widget:    ScreenshotWindow    (弹出面板的父窗口)
            config_manager:   ToolSettingsManager (持久化设置)
        """
        self.panel = panel
        self.model = selection_model
        self.selection_item = selection_item
        self.export_service = export_service
        self.mask_overlay = mask_overlay
        self._config = config_manager

        # ── 共享 Hook 管理器（解决链式 monkey-patch 卸载顺序问题）──
        self._hook_mgr = HookManager()

        # ── 子模块 ──
        self._lock_ratio = LockRatioLogic(selection_item)
        self._rounded_corners = RoundedCornersLogic(
            parent_widget=parent_widget,
            btn_rounded=panel.btn_rounded,
            selection_item=selection_item,
            mask_overlay=mask_overlay,
            export_service=export_service,
            selection_model=selection_model,
            config_manager=config_manager,
            hook_manager=self._hook_mgr,
        )
        self._border_shadow = BorderShadowLogic(
            parent_widget=parent_widget,
            btn_border=panel.btn_border,
            selection_item=selection_item,
            mask_overlay=mask_overlay,
            export_service=export_service,
            selection_model=selection_model,
            config_manager=config_manager,
            rounded_corners_logic=self._rounded_corners,
            hook_manager=self._hook_mgr,
        )

        # ── 刷新背景 ──
        self._parent_window = parent_widget      # ScreenshotWindow
        self._refresh_timer = QTimer()
        # 单次全虚拟桌面同步截屏实测约 20ms（峰值 24ms，见性能分析），16ms 会导致
        # 定时器背靠背触发、主线程无缝隙可用；30ms 留出余量，让事件循环有空档。
        self._refresh_timer.setInterval(30)       # ~33fps (30ms)
        self._refresh_timer.timeout.connect(self._do_refresh_background)
        self._long_press_timer = QTimer()
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(300)   # 按住 300ms 进入连续模式
        self._long_press_timer.timeout.connect(self._start_continuous_refresh)
        self._refresh_pending = False             # 单击标记
        self._refresh_in_flight = False           # 后台抓帧在途标记（背压）
        self._refresh_worker = None

        self._connect()

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------
    def _connect(self):
        # model → 面板跟随
        self.model.rectChanged.connect(self._on_rect_changed)
        self.model.draggingChanged.connect(self._on_dragging_changed)
        self.model.confirmed.connect(self._on_confirmed)

        # panel 按钮 → 子模块
        self.panel.border_toggled.connect(self._on_border_toggled)
        self.panel.rounded_toggled.connect(self._on_rounded_toggled)
        self.panel.lock_ratio_toggled.connect(self._on_lock_ratio_toggled)

        # 刷新背景
        self.panel.refresh_pressed.connect(self._on_refresh_pressed)
        self.panel.refresh_released.connect(self._on_refresh_released)

    # ------------------------------------------------------------------
    # 选区信号回调
    # ------------------------------------------------------------------
    def _on_rect_changed(self, rect: QRectF):
        if rect.isEmpty():
            self.panel.hide()
            return
        self.panel.update_info_text(rect)
        self.panel.follow_rect(rect)
        # 未确认阶段（智能选区悬停）：只显示坐标文字，隐藏功能按钮
        if not self.model.is_confirmed:
            self.panel.set_confirmed(False)
        if not self.panel.isVisible() and not self.model.is_dragging:
            self.panel.show()
            self.panel.raise_()

    def _on_dragging_changed(self, is_dragging: bool):
        if is_dragging:
            # 根据设置决定是否在拖拽时隐藏面板
            hide_on_drag = self._config.get_app_setting(
                "screenshot_info_hide_on_drag", False
            )
            if hide_on_drag:
                self.panel.hide()
            else:
                # 不隐藏，但跟随选区更新位置
                if not self.model.rect().isEmpty():
                    self.panel.update_info_text(self.model.rect())
                    self.panel.follow_rect(self.model.rect())
        else:
            # 拖拽结束，恢复显示
            if not self.model.rect().isEmpty():
                self.panel.update_info_text(self.model.rect())
                self.panel.follow_rect(self.model.rect())
                self.panel.show()
                self.panel.raise_()

    def _on_confirmed(self, rect: QRectF):
        # 确认后显示面板及所有功能按钮
        if not rect.isEmpty():
            self.panel.update_info_text(rect)
            self.panel.follow_rect(rect)
            self.panel.set_confirmed(True)
            self.panel.show()
            self.panel.raise_()

    # ------------------------------------------------------------------
    # 按钮回调 → 委托给子模块
    # ------------------------------------------------------------------
    def _on_border_toggled(self, checked: bool):
        """描边/阴影开关 → BorderShadowLogic"""
        self._border_shadow.set_enabled(checked)

    def _on_rounded_toggled(self, checked: bool):
        """圆角截图开关 → RoundedCornersLogic"""
        self._rounded_corners.set_enabled(checked)

    def _on_lock_ratio_toggled(self, checked: bool):
        """锁定纵横比开关 → LockRatioLogic"""
        rect = self.model.rect() if checked else None
        self._lock_ratio.set_enabled(checked, rect)

    # ------------------------------------------------------------------
    # 刷新背景回调
    # ------------------------------------------------------------------
    def _on_refresh_pressed(self):
        """鼠标按下刷新按钮：立即刷新一次，同时启动长按检测"""
        self._refresh_pending = True
        self._do_refresh_background()          # 立即刷新一帧
        self._long_press_timer.start()          # 开始检测长按

    def _on_refresh_released(self):
        """鼠标释放刷新按钮：停止所有定时器"""
        self._long_press_timer.stop()
        self._refresh_timer.stop()
        self._refresh_pending = False

    def _start_continuous_refresh(self):
        """长按 300ms 后进入连续刷新模式 (~60fps)"""
        self._refresh_pending = False
        self._refresh_timer.start()

    def _do_refresh_background(self):
        """长按刷新的一拍：截屏在工作线程完成，GUI 线程只收图上屏。

        原先 30ms 一拍的同步截屏（实测 20ms+，含引擎设置读取与全屏拷贝）
        把 GUI 线程占死；挪到线程后用 in-flight 标记做背压——上一帧没
        回来就跳过本拍，节奏自适应帧率。
        """
        try:
            win = self._parent_window
            # 窗口已关闭或正在关闭时不再刷新
            if win is None or getattr(win, '_is_closing', False):
                self._refresh_timer.stop()
                self._long_press_timer.stop()
                return
            # 首次刷新时让截图窗口对截屏 API 不可见，之后保持
            if not getattr(win, '_exclude_from_capture_set', False):
                win._set_exclude_from_capture(True)
                win._exclude_from_capture_set = True
            if self._refresh_in_flight:
                return  # 上一帧还在路上：背压跳过本拍
            self._refresh_in_flight = True
            worker = _BackgroundRefreshWorker(getattr(win, '_capture_cursor', None))
            # 以窗口（QWidget）为接收者：信号队列投递回 GUI 线程
            worker.captured.connect(win._on_background_refresh_captured)
            worker.finished.connect(worker.deleteLater)
            self._refresh_worker = worker  # 持引用防 GC
            worker.start()
        except Exception as e:
            self._refresh_in_flight = False
            log_debug(T("刷新背景失败: {e}", e=e))

    def _on_refresh_delivered(self, image):
        """工作线程送回新背景（经窗口槽转发，此刻已在 GUI 线程）。"""
        self._refresh_in_flight = False
        self._refresh_worker = None
        if image.isNull():
            return
        win = self._parent_window
        if win is None or getattr(win, '_is_closing', False):
            return
        win.original_image = image
        win.scene.background.update_image(image)

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def cleanup(self):
        """卸载所有 hook，停止定时器，恢复原始行为"""
        # 停止定时器（必须最先，防止回调访问已销毁对象）
        self._long_press_timer.stop()
        self._refresh_timer.stop()

        # 断开信号（防止延迟排队的信号触发已销毁对象的回调）
        from core.qt_utils import safe_disconnect
        safe_disconnect(self.model.rectChanged, self._on_rect_changed)
        safe_disconnect(self.model.draggingChanged, self._on_dragging_changed)
        safe_disconnect(self.model.confirmed, self._on_confirmed)

        # 卸载子模块 hook
        self._lock_ratio.uninstall()
        self._rounded_corners.uninstall()
        self._border_shadow.uninstall()

        # 兜底：确保所有 hook 都被恢复（即使子模块漏掉）
        self._hook_mgr.unregister_all()

        # RoundedCornersLogic/BorderShadowLogic 构造时以 parent_widget
        # （ScreenshotWindow）为 QObject 父对象；截图窗口是跨会话复用的，
        # 不会自己被销毁，这两个 logic 若不主动 deleteLater()，会话一多
        # 就会在窗口上越攒越多。uninstall() 只处理了它们各自的 popup 和
        # hook，这里补上 logic 对象本身的清理。
        self._rounded_corners.setParent(None)
        self._rounded_corners.deleteLater()
        self._border_shadow.setParent(None)
        self._border_shadow.deleteLater()

        # 置空引用，帮助 GC
        self._parent_window = None
 