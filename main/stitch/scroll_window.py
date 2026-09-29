"""
jietuba_scroll.py - 滚动截图窗口模块

实现滚动长截图功能的窗口类,用于捕获滚动页面的多张截图。

主要功能:
- 显示半透明边框窗口标识截图区域
- 监听鼠标滚轮事件自动触发截图
- 实时显示已捕获的截图数量
- 支持手动/自动截图控制

主要类:
- ScrollCaptureWindow: 滚动截图窗口类

特点:
- 窗口透明,不拦截鼠标事件
- 内容感知:监视定时器持续对比框内画面的降采样签名,检测到变化并
  稳定后自动抓帧拼接——用户以任意速度滚动、随时停顿都能正确出帧,
  不再依赖滚轮事件
- 拼接/哈希/预览缩略图在串行工作线程完成,主线程只抓屏
- 支持取消和完成截图操作;页面静止数秒自动收尾

依赖模块:
- PySide6: GUI框架
- PIL: 图像处理
- ctypes: Windows API调用

使用方法:
    window = ScrollCaptureWindow(capture_rect, parent)
    window.finished.connect(on_finished)
    window.show()
"""

import time
import ctypes
import threading
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QApplication
from PySide6.QtCore import Qt, QRect, QTimer, Signal, QPoint
from PySide6.QtGui import QPainter, QPen, QColor, QPixmap, QGuiApplication, QImage
from typing import Optional
from PIL import Image

# 导入长截图拼接统一接口
from .jietuba_long_stitch_unified import (
    configure as long_stitch_configure,
    normalize_engine_value,
)

from settings import get_tool_settings_manager
from core.save import SaveService
from core import log_debug, log_info, safe_event
from core.logger import log_exception, T, LogMsg
from .scroll_toolbar import FloatingToolbar  # 浮动工具栏（独立模块）
from core.ui_theme import set_own_style

_MODULE_TAG = "LongStitch"


def _log_stitch(*args, force: bool = False):
    """长截图模块的日志输出。

    force=True 记为 INFO（始终保留），否则记为 DEBUG（由全局日志级别过滤）。

    此前这里是 `print = _long_stitch_print`，在模块作用域覆盖了内置 print，
    读代码的人很容易把满屏的 print 误判成标准输出；而 DEBUG 分支还被一个
    恒为 False 的开关挡着，那批调试输出实际上永远不会执行。
    现在统一交给日志系统按级别过滤，调整日志级别即可看到。

    args 里可以混用普通字符串和 T() 构造的可翻译消息（LogMsg），
    LogMsg 在拼接前会先 render() 成当前语言的文本。
    """
    message = " ".join(arg.render() if isinstance(arg, LogMsg) else str(arg) for arg in args)
    if force:
        log_info(message, module=_MODULE_TAG)
    else:
        log_debug(message, module=_MODULE_TAG)


def _load_long_stitch_config():
    """从配置文件加载长截图参数（当前引擎仅支持 hash_rust，只保留其实际用到的参数）"""
    config_mgr = get_tool_settings_manager()

    raw_engine = config_mgr.get_long_stitch_engine()
    engine = normalize_engine_value(raw_engine)

    if engine != raw_engine:
        config_mgr.set_long_stitch_engine(engine)
        _log_stitch(T("📖 检测到长截图引擎旧值 {raw_engine}，已自动转换为 {engine}", raw_engine=raw_engine, engine=engine))

    config = {
        'engine': engine,
        'verbose': False,  # 传给拼接引擎，控制 Rust 侧的输出
        'ignore_top_pixels': config_mgr.get_long_stitch_ignore_top_pixels(),
    }

    _log_stitch(T(
        "📖 从配置加载长截图参数: 引擎={engine}, 顶部忽略={ignore_top_pixels}px",
        engine=config['engine'], ignore_top_pixels=config['ignore_top_pixels'],
    ))

    return config


# 配置拼接引擎（从配置文件读取）
_long_stitch_config = _load_long_stitch_config()
long_stitch_configure(
    engine=_long_stitch_config['engine'],
    verbose=_long_stitch_config['verbose'],
    ignore_top_pixels=_long_stitch_config['ignore_top_pixels'],
)

# Windows API 常量
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000


class PreviewPanel(QWidget):
    """实时预览面板，仅以透明背景展示拼接缩略图"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._fixed_side = 190  # 固定边长度
        self._last_panel_size = None
        self.setFixedWidth(self._fixed_side)
        self.setFixedHeight(self._fixed_side)  # 初始正方形占位
        self._build_ui()
        self.set_placeholder()
        
        # 设置鼠标穿透，防止拦截滚轮事件
        self._setup_mouse_transparent()
    
    def _setup_mouse_transparent(self):
        """设置窗口鼠标穿透，不拦截滚轮事件"""
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_TRANSPARENT | WS_EX_LAYERED)
            _log_stitch(T("[OK] PreviewPanel 已设置为鼠标穿透模式"))
        except Exception as e:
            _log_stitch(T("[WARN] 设置 PreviewPanel 鼠标穿透失败: {e}", e=e))
        self._capture_excluded = False

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFixedSize(self.width(), self.height())
        set_own_style(self.preview_label,
            "background: rgba(0, 0, 0, 0.25);"
            "border: 1px solid rgba(0, 0, 0, 0.8);"
            "border-radius: 8px;"
            "color: rgba(255, 255, 255, 0.85);"
            "font-size: 10pt;"
            "padding: 6px;"
        )
        layout.addWidget(self.preview_label)
        
        # 截图计数标签（左上角）
        self.count_label = QLabel("0", self.preview_label)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label.setFixedSize(40, 24)
        set_own_style(self.count_label, """
            background: rgba(33, 150, 243, 0.9);
            color: white;
            border: 1px solid rgba(33, 150, 243, 1);
            border-radius: 4px;
            font-weight: bold;
            font-size: 11pt;
            padding: 2px;
        """)
        self.count_label.move(8, 8)
        
        self.warning_icon = QLabel("!", self.preview_label)
        self.warning_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.warning_icon.setFixedSize(32, 32)
        set_own_style(self.warning_icon,
            "background: rgba(255, 255, 255, 0.9);"
            "color: #ff4d4f;"
            "border: 1px solid rgba(255, 77, 79, 0.65);"
            "border-radius: 16px;"
            "font-weight: 700;"
            "font-size: 20px;"
        )
        self.warning_icon.move(self.preview_label.width() - self.warning_icon.width() - 10, 10)
        self.warning_icon.hide()

    def set_capture_excluded(self, exclude: bool):
        """根据是否与截图区域重叠，设置截图排除"""
        if exclude == self._capture_excluded:
            return
        try:
            from core.platform_utils import set_window_exclude_from_capture
            set_window_exclude_from_capture(int(self.winId()), exclude)
            self._capture_excluded = exclude
        except Exception:
            pass

    def closeEvent(self, event):
        """关闭时还原截图排除"""
        if self._capture_excluded:
            self.set_capture_excluded(False)
        super().closeEvent(event)

    def set_placeholder(self, scroll_direction="vertical", screenshot_count=0):
        self.preview_label.clear()
        self.preview_label.setText("")

    def update_preview(self, qimage, scroll_direction, screenshot_count):
        """展示拼接结果预览。

        qimage 由拼接工作线程生成：已按显示方向修正（翻转/旋转）、已缩小
        到缩略图级别，主线程只剩 QPixmap 转换与一次小图缩放。
        """
        if qimage is None or qimage.isNull():
            self.set_placeholder(scroll_direction, screenshot_count)
            return

        pixmap = QPixmap.fromImage(qimage)
        img_w = pixmap.width()
        img_h = pixmap.height()
        if img_w <= 0 or img_h <= 0:
            return

        F = self._fixed_side  # 210

        # 获取屏幕可用空间用于限制面板最大尺寸
        screen = QApplication.primaryScreen()
        if self.parent() and hasattr(self.parent(), 'screen') and self.parent().screen():
            screen = self.parent().screen()
        max_screen = screen.geometry().height() - 60 if screen else 800

        if scroll_direction == "vertical":
            # 竖向：宽度固定F，高度按比例，但不超过屏幕
            panel_w = F
            panel_h = max(F, int(img_h * (F / img_w)))
            if panel_h > max_screen:
                panel_h = max_screen  # 超出屏幕则限制，缩放显示
        else:
            # 横向：高度固定F，宽度按比例，但不超过屏幕宽度
            max_screen_w = screen.geometry().width() - 60 if screen else 1400
            panel_h = F
            panel_w = max(F, int(img_w * (F / img_h)))
            if panel_w > max_screen_w:
                panel_w = max_screen_w

        if self._last_panel_size != (panel_w, panel_h):
            self.setFixedSize(panel_w, panel_h)
            self.preview_label.setFixedSize(panel_w, panel_h)
            self._last_panel_size = (panel_w, panel_h)

        if pixmap.size() == self.preview_label.size():
            self.preview_label.setPixmap(pixmap)
        else:
            scaled = pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.preview_label.setPixmap(scaled)
        self.preview_label.setText("")

        # 更新警告图标位置
        self.warning_icon.move(panel_w - self.warning_icon.width() - 10, 10)

    def show_warning(self, message: Optional[str] = None):
        self.warning_icon.raise_()
        # T() 返回 LogMsg（懒翻译消息），setToolTip 只接受 str——历史上
        # 自动完成路径把 T() 直接传进来，TypeError 会把 900ms 后的自动收尾
        # 一起炸掉，页面到底后永远不出图
        if message is not None and not isinstance(message, str):
            message = message.render()
        if message:
            self.warning_icon.setToolTip(message)
        else:
            self.warning_icon.setToolTip("")
        self.warning_icon.show()

    def clear_warning(self):
        self.warning_icon.hide()
        self.warning_icon.setToolTip("")
    
    def update_count(self, count):
        """更新截图计数"""
        self.count_label.setText(str(count))

class _StitchWorker(threading.Thread):
    """长截图像素处理工作线程（串行 FIFO）。

    主线程只负责 grabWindow（必须在 GUI 线程），本线程完成其余全部像素
    工作：QImage→PIL 转换、方向变换、感知哈希、Rust 拼接、预览缩略图。
    拼接结果是随帧数增长的大图，PNG 编解码成本随之增长——留在主线程会让
    滚动截图越滚越卡（longstitch 与 PIL 编解码均释放 GIL，线程化有效）。

    状态权威在本线程：stitched_result、帧计数、方向锁、去重哈希都在
    这里维护，处理完通过 emit 回调（窗口的 stitch_result_ready 信号，
    QueuedConnection）把结果交回主线程。方向锁不再来自滚轮：第 2 帧由
    Rust 自动检测判定（reverse → 翻转态拼接），此后沿用锁定的方向。

    方向判定必须发生在这里而不是提交前：快速连续滚动时，第 N 帧的
    自动检测结果要先于第 N+1 帧的翻转决策生效——串行队列天然保证顺序。
    """

    # 预览缩略图长边上限（面板固定边 210px 的 2 倍，HiDPI 下仍清晰）
    _PREVIEW_LONG_SIDE = 420

    def __init__(self, scroll_direction, locked_direction, duplicate_threshold, emit_fn):
        super().__init__(name="StitchWorker", daemon=True)
        import queue
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._emit = emit_fn
        self.scroll_direction = scroll_direction
        self._locked = locked_direction
        self.duplicate_threshold = duplicate_threshold
        self._stitched = None
        self._count = 0
        self._prev_hash = None
        self._duplicate_count = 0

    def submit(self, qimage, direction_hint):
        """提交一帧。qimage 的所有权移交给本线程，主线程不得再修改。"""
        self._queue.put({
            "qimage": qimage,
            "direction_hint": direction_hint,
        })
        if not self.is_alive():
            self.start()

    def stop(self):
        """请求退出：处理完队首当前任务后即停（剩余帧丢弃）。"""
        self._stop_event.set()
        self._queue.put(None)

    def run(self):
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except Exception:
                continue
            if job is None:
                break
            try:
                self._process(job)
            except Exception as e:
                log_exception(e, T("拼接工作线程处理帧失败"))
                _log_stitch(T("[ERROR] 拼接线程异常: {e}", e=e), force=True)

    def _process(self, job):
        qimage = job["qimage"]
        # 方向随任务走：方向切换后，飞行中的旧帧仍按提交时的方向处理
        self.scroll_direction = job["direction_hint"]

        # QImage → PIL RGB（与旧主线程路径完全相同的字节序假设：BGRA）
        buffer = bytes(qimage.bits())
        pil_image = Image.frombytes(
            'RGBA',
            (qimage.width(), qimage.height()),
            buffer,
            'raw',
            'BGRA'
        ).convert('RGB')

        count_before = self._count
        self._count += 1
        screenshot_count = self._count
        is_first_image = count_before == 0

        # 方向变换：横向旋转成"竖向"以复用拼接算法；向上滚动翻转
        if self.scroll_direction == "horizontal" and not is_first_image:
            pil_image = pil_image.rotate(-90, expand=True)
        if self._locked == "up":
            pil_image = pil_image.transpose(Image.FLIP_TOP_BOTTOM)

        # 到底检测：帧间相似度与拼接成败无关，本帧截取后立即计算
        current_hash = ScrollCaptureWindow._calculate_image_hash(self, pil_image)
        prev_hash = self._prev_hash
        self._prev_hash = current_hash
        frame_is_duplicate = (
            prev_hash is not None
            and ScrollCaptureWindow._images_are_similar(self, prev_hash, current_hash)
        )

        ok = True
        error_detail = None
        try:
            from .jietuba_long_stitch_unified import stitch_images, stitch_images_auto

            if is_first_image:
                self._stitched = pil_image
            else:
                # 横向模式：第2张图片时需要先把第1张也旋转
                if self.scroll_direction == "horizontal" and screenshot_count == 2:
                    self._stitched = self._stitched.rotate(-90, expand=True)
                # 向上/向左滚动模式：第2张图片时需要先把第1张也翻转
                if self._locked == "up" and screenshot_count == 2:
                    self._stitched = self._stitched.transpose(Image.FLIP_TOP_BOTTOM)

                # 自动方向检测：方向未锁定且是第二次拼接时，用 Rust auto 接口
                if self._locked is None and screenshot_count == 2:
                    result, direction = stitch_images_auto(
                        self._stitched, pil_image, debug=False
                    )
                    if result is not None and direction == "reverse":
                        self._locked = "up"
                        # Rust auto 返回的是翻转态（对翻转图片拼接的产物），
                        # 与全程翻转态约定一致，直接存储即可
                        self._stitched = result
                        arrow = "⬆️" if self.scroll_direction == "vertical" else "⬅️"
                        _log_stitch(T("{arrow} 自动检测到反向滚动，已锁定", arrow=arrow))
                        result = "HANDLED"
                    elif result is not None:
                        self._locked = "down"
                        self._stitched = result
                        result = "HANDLED"
                else:
                    # 方向已锁定，正常拼接。
                    # 忽略 img1 一定区域以排除顶部固定标题栏干扰。判断依据是
                    # "Rust 收到的图是否翻转态"，不是屏幕滚动方向。
                    ignore_top_ratio = 0.0
                    ignore_bottom_ratio = 0.0
                    if self.scroll_direction != "horizontal":
                        if self._locked == "up":
                            ignore_bottom_ratio = 0.05
                        else:
                            ignore_top_ratio = 0.15
                    result = stitch_images(
                        [self._stitched, pil_image],
                        ignore_img1_top_ratio=ignore_top_ratio,
                        ignore_img1_bottom_ratio=ignore_bottom_ratio,
                    )

                if result == "HANDLED":
                    pass  # 已在上面处理
                elif result:
                    self._stitched = result
                else:
                    _log_stitch(T("[WARN] 第 {screenshot_count} 张拼接失败，未找到重叠区域", screenshot_count=screenshot_count), force=True)
                    ok = False
                    error_detail = "未找到可靠的重叠区域"

        except Exception as e:
            _log_stitch(T("[WARN] 第 {screenshot_count} 张拼接出错: {e}", screenshot_count=screenshot_count, e=e), force=True)
            import traceback
            traceback.print_exc()
            ok = False
            error_detail = f"算法异常：{e}"
            if self._stitched is None:
                self._stitched = pil_image

        if not ok:
            # 与旧逻辑的 append→pop 等效：失败帧不计入总数
            self._count -= 1

        # 到底自动完成判定：必须已有有效拼接（≥3 帧）且连续 2 帧画面不变。
        # 是否真正收尾由主线程的 _auto_finish_scheduled 决定，这里只上报。
        auto_finish = False
        if frame_is_duplicate and self._count >= 3:
            self._duplicate_count += 1
            if self._duplicate_count >= 2:
                auto_finish = True
                _log_stitch(T("检测到页面已到边缘（连续 2 帧无变化）"), force=True)
        else:
            self._duplicate_count = 0

        width, height = self._stitched.size if self._stitched is not None else (0, 0)
        self._emit({
            "ok": ok,
            "stitched": self._stitched,
            "preview_qimage": self._make_preview_image(),
            "locked_direction": self._locked,
            "screenshot_count": self._count,
            "failed_frame_no": screenshot_count if not ok else 0,
            "auto_finish": auto_finish,
            "error_detail": error_detail,
            "width": width,
            "height": height,
        })

    def _make_preview_image(self):
        """生成显示方向正确的预览缩略图（QImage）。

        旧实现在主线程每帧对整张拼接结果做翻转/旋转 + 全尺寸 RGBA 转换 +
        Smooth 缩放；这里只在缩略图级别做一次，成本与拼接图尺寸解耦。
        """
        display = self._stitched
        if display is None:
            return None
        # 顺序：先翻转再旋转（与主窗口完成时的还原顺序一致，方向相反）
        if self._locked == "up" and self._count >= 2:
            display = display.transpose(Image.FLIP_TOP_BOTTOM)
        if self.scroll_direction == "horizontal" and self._count >= 2:
            display = display.rotate(90, expand=True)

        thumb = display.copy()
        thumb.thumbnail(
            (self._PREVIEW_LONG_SIDE, self._PREVIEW_LONG_SIDE),
            Image.Resampling.BILINEAR,
        )
        rgba = thumb.convert("RGBA")
        data = rgba.tobytes("raw", "RGBA")
        return QImage(
            data, rgba.width, rgba.height, rgba.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()  # copy() 脱离 data 的生存期


class ScrollCaptureWindow(QWidget):
    """滚动长截图窗口
    
    特性：
    - 带边框的透明窗口
    - 不拦截鼠标滚轮事件（鼠标可以直接操作后面的网页）
    - 监听全局滚轮事件，每次滚轮后1秒截图
    - 底部有完成和取消按钮
    """
    
    finished = Signal()  # 完成信号
    cancelled = Signal()  # 取消信号
    stitch_result_ready = Signal(object)  # 拼接工作线程 → 主线程的结果载荷

    # ── 内容感知抓帧参数 ──
    # 不再依赖滚轮事件触发：监视定时器持续对框内内容做降采样签名，
    # 内容变化→等它稳定→自动抓帧拼接。用户以任意速度滚动、随时停顿
    # 都能正确出帧。
    _WATCH_IDLE_MS = 250     # 内容与上帧一致时的轮询间隔（省 CPU）
    _WATCH_ACTIVE_MS = 90    # 检测到变化后的轮询间隔（快速捕捉稳定点）
    _STABLE_TICKS = 2        # 连续 N 次采样一致即认为内容已稳定
    _SIGNATURE_SIZE = 32     # 内容签名边长（32x32 ARGB32）
    # 签名差异位比例阈值：容忍光标闪烁、抗锯齿抖动这类微变化（32x32x4
    # 字节 = 32768 位，0.5% ≈ 163 位）
    _SIGNATURE_MAX_DIFF_BITS = 160
    _CHANGE_FORCE_CAPTURE_S = 0.7   # 内容持续变化超过 N 秒强拍一帧（动画页兜底）
    _AUTO_FINISH_IDLE_S = 5.0       # 内容静止 N 秒且已拼≥2帧 → 自动收尾
    _AUTO_FINISH_WARN_S = 3.0       # 静止到 N 秒时先给出提示

    def __init__(self, capture_rect, parent=None, config_manager=None):
        """初始化滚动截图窗口

        Args:
            capture_rect: QRect，截图区域（屏幕坐标）
            parent: 父窗口
            config_manager: 配置管理器（用于钉图功能）
        """
        super().__init__(parent)

        self.capture_rect = capture_rect
        self.config_manager = config_manager  # 保存配置管理器
        self.screenshots = []  # 存储截图的列表（只计帧数）
        self.scroll_distances = []  # 每帧的拼接增益（等效滚动距离，像素）

        # 保存目录（由外部设置）
        self.save_directory = None
        self.save_service = SaveService()

        # 截图方向: "vertical"(竖向) 或 "horizontal"(横向)
        self.scroll_direction = "vertical"

        # 滚动方向锁定: None=未锁定（由第 2 帧的自动检测锁定）,
        # "down"/"up"。不再来自滚轮，纯由画面内容判定。
        self.scroll_locked_direction = None

        # 实时拼接相关
        self.stitched_result = None  # 当前拼接的结果图
        self.preview_warning_active = False

        # 去重相关（worker 内部亦有一份，手动抓帧路径仍会用）
        self.duplicate_threshold = 0.95

        # 自动收尾
        self._auto_finish_scheduled = False

        # ── 内容感知监视状态 ──
        self._last_captured_sig = None   # 上一张已采集帧的内容签名
        self._candidate_sig = None       # 变化中内容的最近一次签名
        self._stable_count = 0           # 候选签名连续一致的次数
        self._change_started_at = None   # 本轮变化开始时刻（monotonic）
        self._idle_started_at = None     # 内容静止开始时刻（monotonic）

        # 拼接工作线程：主线程只抓帧，像素管线在后台串行完成。
        # 结果经 QueuedConnection 回主线程，见 _on_stitch_result。
        self._stitch_worker = _StitchWorker(
            scroll_direction=self.scroll_direction,
            locked_direction=self.scroll_locked_direction,
            duplicate_threshold=self.duplicate_threshold,
            emit_fn=self.stitch_result_ready.emit,
        )
        self.stitch_result_ready.connect(
            self._on_stitch_result,
            Qt.ConnectionType.QueuedConnection
        )

        # 内容监视定时器：初始采集完成后再启动（见 _capture_initial_screenshot）
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(self._WATCH_IDLE_MS)
        self._watch_timer.timeout.connect(self._watch_tick)

        self._setup_window()
        self._setup_ui()
        self._setup_mouse_hook()
        
        # 创建独立的浮动工具栏
        self._setup_floating_toolbar()

        # 创建实时拼接预览面板
        self._setup_preview_panel()
        
        # 添加窗口定位检查定时器
        self._position_fix_timer = QTimer()
        self._position_fix_timer.setSingleShot(True)
        self._position_fix_timer.timeout.connect(self._force_fix_window_position)
        self._position_fix_timer.start(200)  # 200ms后再次检查并修复
    
    def _get_correct_window_position(self, border_width):
        """获取正确的窗口位置。
        
        capture_rect 已经是真实屏幕坐标，窗口只需在此基础上向外扩展 border_width
        即可与截图区域完全对齐。不做任何单屏 clamp——跨屏选区本来就应该跨屏显示。
        曾有的 clamp 逻辑会在跨屏捕获时把窗口强制推到单个屏幕内，造成位置错误。
        """
        return (self.capture_rect.x() - border_width,
                self.capture_rect.y() - border_width)
        
    def _setup_window(self):
        """设置窗口属性"""
        # 设置窗口标志：无边框、置顶
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool
        )
        
        # 设置窗口透明度和背景
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 设置关闭时自动销毁，防止内存泄漏
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        
        # 设置窗口位置和大小（基于截图区域）
        # 窗口区域 = 截图区域 + 底部按钮栏
        
        # 为边框预留空间（但截图区域不包含边框）
        border_width = 3
        
        window_x, window_y = self._get_correct_window_position(border_width)
        
        final_width = self.capture_rect.width() + border_width * 2
        final_height = self.capture_rect.height() + border_width * 2
        
        # 不再包含按钮栏高度（工具栏已独立）
        self.setGeometry(
            window_x,
            window_y,
            final_width,
            final_height
        )
        
    def _setup_ui(self):
        """设置UI界面 - 只保留透明边框区域"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)  # 为边框预留空间
        layout.setSpacing(0)
        
        # 透明区域（用于显示边框）
        self.transparent_area = QWidget()
        self.transparent_area.setFixedSize(
            self.capture_rect.width(),
            self.capture_rect.height()
        )
        layout.addWidget(self.transparent_area)
    
    def _setup_floating_toolbar(self):
        """创建并设置独立的浮动工具栏"""
        self.toolbar = FloatingToolbar(self)
        
        # 连接工具栏信号
        self.toolbar.direction_changed.connect(self._toggle_direction)
        self.toolbar.manual_capture.connect(self._on_manual_capture)
        self.toolbar.pin_clicked.connect(self._on_pin)
        self.toolbar.finish_clicked.connect(self._on_finish)
        self.toolbar.cancel_clicked.connect(self._on_cancel)
        
        self._position_floating_toolbar()
        self.toolbar.show()

    def _position_floating_toolbar(self):
        """根据屏幕边界将工具栏对齐到截图区域上方居中，支持上/下/左/右四向智能回退"""
        if not hasattr(self, 'toolbar') or self.toolbar is None:
            return
        from core.ui_scale import scaled
        margin = scaled(10)
        screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        screen_geometry = screen.geometry()
        tw = self.toolbar.width()
        th = self.toolbar.height()

        # 水平居中 x（供上下方案使用）
        x_center = self.x() + (self.width() - tw) // 2
        x_center = max(screen_geometry.left() + margin,
                       min(x_center, screen_geometry.right() - margin - tw))

        # 策略 1：截图区上方
        y_above = self.y() - th - margin
        if y_above >= screen_geometry.top() + margin:
            self.toolbar.move(x_center, y_above)
            return

        # 策略 2：截图区下方
        y_below = self.y() + self.height() + margin
        if y_below + th <= screen_geometry.bottom() - margin:
            self.toolbar.move(x_center, y_below)
            return

        # 策略 3/4：左侧 / 右侧（截图区占满纵向时）
        y_mid = self.y() + (self.height() - th) // 2
        y_mid = max(screen_geometry.top() + margin,
                    min(y_mid, screen_geometry.bottom() - margin - th))
        if self.x() - tw - margin >= screen_geometry.left() + margin:
            self.toolbar.move(self.x() - tw - margin, y_mid)
        else:
            x_right = min(self.x() + self.width() + margin,
                          screen_geometry.right() - margin - tw)
            x_right = max(screen_geometry.left() + margin, x_right)
            self.toolbar.move(x_right, y_mid)

    def _setup_preview_panel(self):
        """创建拼接结果预览面板"""
        self.preview_panel = PreviewPanel(self)
        self._position_preview_panel()
        self.preview_panel.show()
        self._refresh_preview_panel()

    def _position_preview_panel(self):
        """根据窗口位置调整预览面板，尽量贴近截图区域且避免进入截图区域和工具栏"""
        if not hasattr(self, 'preview_panel') or self.preview_panel is None:
            return
        panel = self.preview_panel
        margin = 14
        # PyQt6: 使用 screen() 代替 desktop()
        screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        screen_geometry = screen.geometry()
        screen_left = screen_geometry.x()
        screen_top = screen_geometry.y()
        screen_right = screen_geometry.x() + screen_geometry.width()
        screen_bottom = screen_geometry.y() + screen_geometry.height()
        
        # 截图区域的边界
        capture_left = self.x()
        capture_right = self.x() + self.width()
        capture_top = self.y()
        capture_bottom = self.y() + self.height()
        
        # 获取工具栏位置（用于避让）
        toolbar_rect = None
        if hasattr(self, 'toolbar') and self.toolbar is not None:
            toolbar_rect = QRect(
                self.toolbar.x(),
                self.toolbar.y(),
                self.toolbar.width(),
                self.toolbar.height()
            )
        
        def is_overlapping_toolbar(x, y):
            """检查预览面板是否与工具栏重叠"""
            if toolbar_rect is None:
                return False
            panel_rect = QRect(int(x), int(y), panel.width(), panel.height())
            return toolbar_rect.intersects(panel_rect)
        
        scroll_dir = getattr(self, 'scroll_direction', 'vertical')

        if scroll_dir == "vertical":
            # ===== 竖向截图：优先放左右，下边对齐 =====
            
            # 尝试1: 右边，下边对齐
            x_right = capture_right + margin
            if x_right + panel.width() <= screen_right - margin:
                x = x_right
                y = capture_bottom - panel.height()
                y = max(screen_top + margin, min(y, screen_bottom - panel.height() - margin))
                if not is_overlapping_toolbar(x, y):
                    panel.move(int(x), int(y))
                    return
            
            # 尝试2: 左边，下边对齐
            x_left = capture_left - panel.width() - margin
            if x_left >= screen_left + margin:
                x = x_left
                y = capture_bottom - panel.height()
                y = max(screen_top + margin, min(y, screen_bottom - panel.height() - margin))
                if not is_overlapping_toolbar(x, y):
                    panel.move(int(x), int(y))
                    return
            
            # 尝试3: 上边，水平居中
            y_top = capture_top - panel.height() - margin
            if toolbar_rect and toolbar_rect.bottom() >= y_top - margin:
                y_top = toolbar_rect.y() - panel.height() - margin
            if y_top >= screen_top + margin:
                x = capture_left + (self.width() - panel.width()) // 2
                x = max(screen_left + margin, min(x, screen_right - panel.width() - margin))
                if not is_overlapping_toolbar(x, y_top):
                    panel.move(int(x), int(y_top))
                    return
            
            # 尝试4: 下边，水平居中
            y_bottom = capture_bottom + margin
            if toolbar_rect and toolbar_rect.top() <= y_bottom + panel.height() + margin:
                y_bottom = toolbar_rect.bottom() + margin
            if y_bottom + panel.height() <= screen_bottom - margin:
                x = capture_left + (self.width() - panel.width()) // 2
                x = max(screen_left + margin, min(x, screen_right - panel.width() - margin))
                if not is_overlapping_toolbar(x, y_bottom):
                    panel.move(int(x), int(y_bottom))
                    return

        else:
            # ===== 横向截图：优先放上下，右边对齐 =====
            
            # 尝试1: 上边
            y_top = capture_top - panel.height() - margin
            if y_top >= screen_top + margin:
                x = capture_right - panel.width()
                x = max(screen_left + margin, min(x, screen_right - panel.width() - margin))
                if not is_overlapping_toolbar(x, y_top):
                    panel.move(int(x), int(y_top))
                    return
            
            # 尝试2: 下边（工具栏也在下面则再往下）
            y_bottom = capture_bottom + margin
            if toolbar_rect:
                tb_bottom = toolbar_rect.y() + toolbar_rect.height()
                if toolbar_rect.y() >= capture_bottom:
                    # 工具栏在截图区域下方，面板放到工具栏下面
                    y_bottom = max(y_bottom, tb_bottom + margin)
            if y_bottom + panel.height() <= screen_bottom - margin:
                x = capture_right - panel.width()
                x = max(screen_left + margin, min(x, screen_right - panel.width() - margin))
                panel.move(int(x), int(y_bottom))
                return
            
            # 尝试3: 右边
            x_right = capture_right + margin
            if x_right + panel.width() <= screen_right - margin:
                x = x_right
                y = capture_top + (self.height() - panel.height()) // 2
                y = max(screen_top + margin, min(y, screen_bottom - panel.height() - margin))
                if not is_overlapping_toolbar(x, y):
                    panel.move(int(x), int(y))
                    return
            
            # 尝试4: 左边
            x_left = capture_left - panel.width() - margin
            if x_left >= screen_left + margin:
                x = x_left
                y = capture_top + (self.height() - panel.height()) // 2
                y = max(screen_top + margin, min(y, screen_bottom - panel.height() - margin))
                if not is_overlapping_toolbar(x, y):
                    panel.move(int(x), int(y))
                    return
        
        # 兜底: 放在屏幕右上角（避免进入截图区域和工具栏）
        x = screen_right - panel.width() - margin
        y = screen_top + margin
        if is_overlapping_toolbar(x, y) and toolbar_rect:
            y = toolbar_rect.bottom() + margin
            if y + panel.height() > screen_bottom - margin:
                x = screen_left + margin
                y = screen_top + margin
        panel.move(int(x), int(y))

    def _refresh_preview_panel(self):
        """把预览面板重置为占位状态。

        常规的预览刷新由拼接工作线程的结果回包驱动（_on_stitch_result），
        这里只处理方向切换/引擎重配置这类"已有预览失效"的时刻：清空显示，
        下一帧拼接结果回来后自动恢复。
        """
        if not hasattr(self, 'preview_panel') or self.preview_panel is None:
            return
        self.preview_panel.set_placeholder(self.scroll_direction, len(self.screenshots))

    def _show_preview_warning(self, message: str):
        self.preview_warning_active = True
        if hasattr(self, 'preview_panel') and self.preview_panel is not None:
            self.preview_panel.show_warning(message)

    def _clear_preview_warning(self):
        if not self.preview_warning_active:
            return
        self.preview_warning_active = False
        if hasattr(self, 'preview_panel') and self.preview_panel is not None:
            self.preview_panel.clear_warning()

    def _setup_mouse_hook(self):
        """设置窗口鼠标穿透（不拦截页面滚动；抓帧由内容监视器驱动）"""
        try:
            # 使用Windows API设置窗口透明鼠标事件（需在主线程执行）
            hwnd = int(self.transparent_area.winId())
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_TRANSPARENT | WS_EX_LAYERED)
            _log_stitch(T("[OK] 窗口已设置为鼠标穿透模式"))
        except Exception as e:
            _log_stitch(T("[ERROR] 设置窗口鼠标穿透时出错: {e}", e=e), force=True)
            import traceback
            traceback.print_exc()


    def _toggle_direction(self):
        """切换截图方向（竖向/横向）"""
        if self.scroll_direction == "vertical":
            self.scroll_direction = "horizontal"
            self.toolbar.update_direction("horizontal")
            _log_stitch(T("🔄 切换到横向截图模式"))
        else:
            self.scroll_direction = "vertical"
            self.toolbar.update_direction("vertical")
            _log_stitch(T("🔄 切换到竖向截图模式"))
        
        # 重新配置拼接引擎
        self._reconfigure_stitch_engine()
        self._refresh_preview_panel()
        
    
    def _reconfigure_stitch_engine(self):
        """重新配置拼接引擎（哈希匹配算法只支持竖向拼接，横向截图会先旋转90度再拼接后旋转回来）"""
        try:
            from .jietuba_long_stitch_unified import configure, config

            configure(
                engine=config.engine,
                verbose=True,
                ignore_top_pixels=config.ignore_top_pixels,
            )

            if self.scroll_direction == "horizontal":
                _log_stitch(T("[OK] 拼接引擎已重新配置: 横向截图（图片旋转90度+竖向拼接）"))
            else:
                _log_stitch(T("[OK] 拼接引擎已重新配置: 竖向截图（竖向拼接）"))

            self._refresh_preview_panel()

        except Exception as e:
            _log_stitch(T("[ERROR] 重新配置拼接引擎失败: {e}", e=e), force=True)
            import traceback
            traceback.print_exc()
    
    @safe_event
    def showEvent(self, event):
        """窗口显示事件 - 立即截取第一张图"""
        super().showEvent(event)
        
        # 验证窗口位置是否正确
        self._verify_window_position()

        # 延迟一次事件循环后强制将所有浮动子窗口提到 TOPMOST 栈顶，
        # 避免初始显示时被系统任务栏（同为 HWND_TOPMOST）压在下方。
        QTimer.singleShot(0, self, self._raise_all_topmost)

        # 使用QTimer延迟执行，确保窗口完全显示后再截图
        QTimer.singleShot(100, self, self._capture_initial_screenshot)

    def _raise_all_topmost(self):
        """将主窗口及所有浮动子窗口推到 TOPMOST z-order 顶部。"""
        self.raise_()
        if hasattr(self, 'toolbar') and self.toolbar is not None:
            self.toolbar.raise_()
        if hasattr(self, 'preview_panel') and self.preview_panel is not None:
            self.preview_panel.raise_()
    
    def _verify_window_position(self):
        """验证窗口位置是否正确"""
        try:
            app = QApplication.instance()
            
            # 获取窗口当前位置
            window_x = self.x()
            window_y = self.y()
            window_center = QPoint(window_x + self.width() // 2, window_y + self.height() // 2)
            
            # PyQt6: 找到窗口所在的显示器
            current_screen = app.screenAt(window_center)
            if current_screen is None:
                current_screen = app.primaryScreen()
            screen_geometry = current_screen.geometry()
            
            _log_stitch(T("窗口位置验证:"))
            _log_stitch(T("   窗口位置: x={window_x}, y={window_y}", window_x=window_x, window_y=window_y))
            _log_stitch(T("   窗口中心: x={cx}, y={cy}", cx=window_center.x(), cy=window_center.y()))
            _log_stitch(T("   所在显示器: {current_screen}", current_screen=current_screen))
            _log_stitch(T(
                "   显示器范围: x={x_min}-{x_max}, y={y_min}-{y_max}",
                x_min=screen_geometry.x(), x_max=screen_geometry.x() + screen_geometry.width(),
                y_min=screen_geometry.y(), y_max=screen_geometry.y() + screen_geometry.height(),
            ))

            # 检查截图区域中心所在的显示器
            capture_center_x = self.capture_rect.x() + self.capture_rect.width() // 2
            capture_center_y = self.capture_rect.y() + self.capture_rect.height() // 2
            capture_center = QPoint(capture_center_x, capture_center_y)
            # PyQt6: 使用 screenAt() 代替 desktop.screenNumber()
            expected_screen = app.screenAt(capture_center)

            _log_stitch(T("   截图区域中心: x={capture_center_x}, y={capture_center_y}", capture_center_x=capture_center_x, capture_center_y=capture_center_y))
            _log_stitch(T("   期望显示器: {expected_screen}", expected_screen=expected_screen))

            if expected_screen and current_screen != expected_screen:
                _log_stitch(T("[WARN] 警告: 窗口显示在显示器 {screen_name}，但截图区域在不同的显示器", screen_name=current_screen.name()))
                
                # 尝试移动窗口到截图区域所在的显示器
                capture_center_x = self.capture_rect.x() + self.capture_rect.width() // 2
                capture_center_y = self.capture_rect.y() + self.capture_rect.height() // 2
                capture_center = QPoint(capture_center_x, capture_center_y)
                target_screen = app.screenAt(capture_center)
                if target_screen is None:
                    target_screen = app.primaryScreen()
                
                target_screen_geometry = target_screen.geometry()
                # 计算在目标显示器上的相对位置
                relative_x = self.capture_rect.x() - 3  # border_width = 3
                relative_y = self.capture_rect.y() - 3
                
                # 确保不超出边界
                if (relative_x >= target_screen_geometry.x() and 
                    relative_y >= target_screen_geometry.y() and
                    relative_x + self.width() <= target_screen_geometry.x() + target_screen_geometry.width() and
                    relative_y + self.height() <= target_screen_geometry.y() + target_screen_geometry.height()):
                    
                    _log_stitch(T("[FIX] 尝试移动窗口到正确位置: x={relative_x}, y={relative_y}", relative_x=relative_x, relative_y=relative_y))
                    self.move(relative_x, relative_y)
                    self.raise_()
                    self.activateWindow()
                else:
                    _log_stitch(T("[WARN] 无法移动窗口到目标位置，可能会超出显示器边界"))
            else:
                _log_stitch(T("[OK] 窗口位置正确"))

        except Exception as e:
            _log_stitch(T("[ERROR] 验证窗口位置时出错: {e}", e=e), force=True)
    
    def _force_fix_window_position(self):
        """强制调整窗口位置。"""
        try:
            # 如果窗口不可见，先让它可见
            if not self.isVisible():
                _log_stitch(T("[WARN] 检测到窗口不可见，强制显示"))
                self.show()
                self.raise_()
                self.activateWindow()
                return
            
            app = QApplication.instance()
            
            # 获取窗口当前位置
            window_rect = self.geometry()
            
            # PyQt6: 检查窗口是否在任何显示器上可见
            visible_on_any_screen = False
            for screen in app.screens():
                screen_geometry = screen.geometry()
                if screen_geometry.intersects(window_rect):
                    visible_on_any_screen = True
                    break
            
            if not visible_on_any_screen:
                _log_stitch(T("🚨 检测到窗口在所有显示器外，执行强制修复..."))
                
                # 找到截图区域所在的显示器
                capture_center_x = self.capture_rect.x() + self.capture_rect.width() // 2
                capture_center_y = self.capture_rect.y() + self.capture_rect.height() // 2
                capture_center = QPoint(capture_center_x, capture_center_y)
                
                target_screen = app.screenAt(capture_center)
                if target_screen is None:
                    target_screen = app.primaryScreen()
                    _log_stitch(T("[WARN] 截图区域不在任何显示器内，使用主显示器"))
                
                target_geometry = target_screen.geometry()
                
                # 将窗口移动到目标显示器的中央
                new_x = target_geometry.x() + (target_geometry.width() - self.width()) // 2
                new_y = target_geometry.y() + (target_geometry.height() - self.height()) // 2
                
                _log_stitch(T("[FIX] 强制移动窗口到显示器 {target_screen} 中央: x={new_x}, y={new_y}", target_screen=target_screen, new_x=new_x, new_y=new_y))
                self.move(new_x, new_y)
                self.raise_()
                self.activateWindow()
                
                # 更新窗口标题以提示用户
                self.setWindowTitle(self.tr("Long screenshot - window position corrected"))
            else:
                _log_stitch(T("[OK] 窗口位置验证通过"))

        except Exception as e:
            _log_stitch(T("[ERROR] 强制修复窗口位置时出错: {e}", e=e), force=True)
    
    def _capture_initial_screenshot(self):
        """截取初始截图（窗口显示时的区域内容），随后启动内容监视"""
        _log_stitch(T("🎬 截取初始截图（第1张）..."))
        self._do_capture()
        _log_stitch(T("   初始截图已提交，当前共 {count} 张", count=len(self.screenshots)))

        # 内容监视从这里开始：之后框内画面一有变化并稳定，就自动采集拼接
        if not self._watch_timer.isActive():
            self._watch_timer.start()
            _log_stitch(T("[OK] 内容监视已启动（检测到画面变化并稳定后自动采集）"), force=True)

    def _calculate_image_hash(self, pil_image):
        """计算图片的感知哈希值（用于相似度比较）"""

        # 缩小图片到16x16用于快速比较。BOX 采样对"画面是否变了"这一判定
        # 足够（旧实现用 LANCZOS，是对整帧最慢的重采样，帧越大越明显）。
        small_img = pil_image.resize((16, 16), Image.Resampling.BOX)
        # 转为灰度
        gray_img = small_img.convert('L')
        # 计算平均值
        pixels = list(gray_img.getdata())
        avg = sum(pixels) / len(pixels)
        # 生成哈希（大于平均值为1，小于为0）
        hash_str = ''.join('1' if p > avg else '0' for p in pixels)
        return hash_str
    
    def _images_are_similar(self, hash1, hash2):
        """比较两个哈希值的相似度"""
        if hash1 is None or hash2 is None:
            return False
        
        # 计算汉明距离（不同位的数量）
        diff_bits = sum(c1 != c2 for c1, c2 in zip(hash1, hash2))
        similarity = 1 - (diff_bits / len(hash1))
        
        return similarity >= self.duplicate_threshold

    def _exclude_overlapping_ui(self, exclude: bool):
        """检测 UI 窗口是否与截图区域重叠，按需排除/恢复截图捕获"""
        from core.platform_utils import set_window_exclude_from_capture
        for widget in (getattr(self, 'toolbar', None), getattr(self, 'preview_panel', None)):
            if widget is None or not widget.isVisible():
                continue
            widget_rect = QRect(widget.x(), widget.y(), widget.width(), widget.height())
            if widget_rect.intersects(self.capture_rect):
                set_window_exclude_from_capture(int(widget.winId()), exclude)
    
    def _grab_region(self):
        """抓取截图区域，返回 (QPixmap, 签名)。失败时返回 (None, None)。"""
        app = QGuiApplication.instance()
        capture_center_x = self.capture_rect.x() + self.capture_rect.width() // 2
        capture_center_y = self.capture_rect.y() + self.capture_rect.height() // 2
        center_point = QPoint(capture_center_x, capture_center_y)

        screen = app.screenAt(center_point)
        if screen is None:
            _log_stitch(T("[WARN] 截图区域不在任何显示器范围内，使用主显示器"), force=True)
            screen = app.primaryScreen()

        screen_geometry = screen.geometry()

        # 将虚拟桌面坐标转换为相对于目标屏幕的坐标
        relative_x = self.capture_rect.x() - screen_geometry.x()
        relative_y = self.capture_rect.y() - screen_geometry.y()

        pixmap = screen.grabWindow(
            0,
            relative_x,
            relative_y,
            self.capture_rect.width(),
            self.capture_rect.height()
        )
        if pixmap.isNull():
            return None, None
        return pixmap, self._signature(pixmap)

    def _signature(self, pixmap):
        """内容的降采样签名：32x32 ARGB32 的全部字节拼成一个大整数。

        只在 QPixmap 上直接缩小（Qt C++ 完成，全分辨率数据不进 Python），
        签名比较用大整数相等 + XOR 位差（微秒级），供监视定时器每帧调用。
        """
        size = self._SIGNATURE_SIZE
        small = pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        ).toImage()
        if small.format() != QImage.Format.Format_ARGB32:
            small = small.convertToFormat(QImage.Format.Format_ARGB32)
        bits = small.constBits()
        return int.from_bytes(bytes(bits[: size * size * 4]), "little")

    def _sig_similar(self, a, b) -> bool:
        """两个签名是否代表同一画面。相等走快路径；否则按差异位比例判定，
        容忍光标闪烁、抗锯齿抖动这类不影响拼接的微变化。"""
        if a is None or b is None:
            return False
        if a == b:
            return True
        return (a ^ b).bit_count() <= self._SIGNATURE_MAX_DIFF_BITS

    def _set_watch_interval(self, ms):
        if self._watch_timer.interval() != ms:
            self._watch_timer.setInterval(ms)

    def _watch_tick(self):
        """内容感知监视的一次采样：变化→等稳定→自动抓帧。

        与上一张已采集帧一致 → 内容没动（阅读停顿/已到底），计入静止时长；
        与最近一次候选签名一致 → 内容在变化后稳定了，连续稳定 N 次即抓帧；
        否则 → 内容仍在滚动，记下候选签名并切换到高频轮询。
        """
        if not self.isVisible():
            return

        self._exclude_overlapping_ui(True)
        try:
            pixmap, sig = self._grab_region()
            if pixmap is None:
                return
            now = time.monotonic()

            if self._sig_similar(sig, self._last_captured_sig):
                # 内容与上一张已采集帧一致：静止计时，作为到底自动收尾的依据
                self._candidate_sig = None
                self._stable_count = 0
                self._set_watch_interval(self._WATCH_IDLE_MS)
                if self._idle_started_at is None:
                    self._idle_started_at = now
                self._maybe_auto_finish(now - self._idle_started_at)
                return

            self._idle_started_at = None

            if (self._candidate_sig is not None
                    and self._sig_similar(sig, self._candidate_sig)):
                self._stable_count += 1
                if self._stable_count >= self._STABLE_TICKS:
                    # 内容已稳定 → 采集这一帧
                    self._last_captured_sig = sig
                    self._candidate_sig = None
                    self._stable_count = 0
                    self._change_started_at = None
                    self._set_watch_interval(self._WATCH_IDLE_MS)
                    _log_stitch(T("🎞️ 检测到画面变化并已稳定，自动采集"), force=False)
                    self._do_capture()
                    return

                # 尚未达到稳定次数：保持高频轮询
                self._set_watch_interval(self._WATCH_ACTIVE_MS)
                return

            # 内容仍在变化（滚动中）：记候选签名。持续变化超过阈值时强拍一帧，
            # 避免持续动画/惯性滚动页面永远等不到稳定点（拼不上的帧会被
            # worker 的失败路径安全丢弃）。
            if self._candidate_sig is None:
                self._change_started_at = now
            self._candidate_sig = sig
            self._stable_count = 0
            self._set_watch_interval(self._WATCH_ACTIVE_MS)

            if (self._change_started_at is not None
                    and now - self._change_started_at > self._CHANGE_FORCE_CAPTURE_S):
                self._last_captured_sig = sig
                self._candidate_sig = None
                self._change_started_at = None
                self._set_watch_interval(self._WATCH_IDLE_MS)
                _log_stitch(T("🎞️ 画面持续变化，强制采集当前帧"), force=False)
                self._do_capture()
        finally:
            self._exclude_overlapping_ui(False)

    def _maybe_auto_finish(self, idle_seconds: float):
        """内容静止足够久且已有有效拼接 → 自动收尾（用户也可随时手动完成）。"""
        if self._auto_finish_scheduled:
            return
        if len(self.screenshots) < 2:
            return
        if idle_seconds < self._AUTO_FINISH_WARN_S:
            return
        self._auto_finish_scheduled = True
        _log_stitch(T("页面内容已静止 {idle:.1f} 秒，自动完成拼接", idle=idle_seconds), force=True)
        self._show_preview_warning(T("页面已无变化，即将自动完成拼接…"))
        QTimer.singleShot(600, self, self._auto_finish_if_alive)

    def _do_capture(self):
        """立即抓取一帧并提交给拼接工作线程（手动抓帧/监视器稳定点共用）。

        像素管线见 _StitchWorker。提交后更新内容签名基线，监视器据此判断
        后续变化。
        """
        # 截图前：排除与截图区域重叠的 UI 窗口
        self._exclude_overlapping_ui(True)
        try:
            pixmap, sig = self._grab_region()
            if pixmap is None:
                _log_stitch(T("[ERROR] 截图失败"), force=True)
                return

            self._last_captured_sig = sig
            self._idle_started_at = None

            # toImage() 返回独立缓冲的 QImage，移交工作线程后主线程不再触碰。
            self._stitch_worker.submit(pixmap.toImage(), self.scroll_direction)

        except Exception as e:
            _log_stitch(T("[ERROR] 截图时出错: {e}", e=e), force=True)
            import traceback
            traceback.print_exc()
        finally:
            # 帧已到手，恢复 UI 窗口可被截图（像素处理不再需要排除）
            self._exclude_overlapping_ui(False)

    def _on_stitch_result(self, payload):
        """拼接工作线程的结果回包（QueuedConnection，主线程执行）。

        整体包 try/except：这是排队槽，任何未处理异常都会直接顶到
        sys.excepthook（崩溃对话框），把后面的自动收尾、预览更新全部打断。
        出错记日志继续，用户最多丢一帧的预览更新。
        """
        try:
            self._apply_stitch_result(payload)
        except Exception as e:
            log_exception(e, T("处理拼接结果失败"))

    def _apply_stitch_result(self, payload):
        count = payload["screenshot_count"]

        # 方向锁镜像：worker 的自动检测结果是权威值
        self.scroll_locked_direction = payload["locked_direction"]

        if payload["ok"]:
            prev_height = (
                self.stitched_result.size[1]
                if self.stitched_result is not None else 0
            )
            self.stitched_result = payload["stitched"]
            while len(self.screenshots) < count:
                self.screenshots.append(None)

            # 记录本帧带来的拼接增益（等效滚动距离，像素）
            self.scroll_distances.append(max(0, payload["height"] - prev_height))

            if hasattr(self, 'preview_panel') and self.preview_panel:
                self.preview_panel.update_count(count)

            # 只输出一行关键信息（force：冻结包里按 INFO 落盘，链路诊断依赖它）
            _log_stitch(T(
                "📸 第 {screenshot_count} 张 → 拼接结果: {w}x{h}",
                screenshot_count=len(self.screenshots),
                w=payload["width"], h=payload["height"],
            ), force=True)
            self._clear_preview_warning()
        else:
            self._show_stitch_failure(payload["failed_frame_no"], payload["error_detail"])

        if payload["preview_qimage"] is not None and getattr(self, 'preview_panel', None):
            self.preview_panel.update_preview(
                payload["preview_qimage"],
                self.scroll_direction,
                count,
            )
            # 面板大小可能变化，重新定位到不遮挡截图区域的位置
            self._position_preview_panel()

        if payload["auto_finish"] and not self._auto_finish_scheduled:
            self._auto_finish_scheduled = True
            _log_stitch(T("检测到页面已到边缘（连续 2 帧无变化），自动完成拼接"), force=True)
            # 先排自动收尾，再动提示 UI：收尾是功能，提示只是装饰——
            # 提示 UI 再出异常也不能拖住 900ms 后的自动完成
            QTimer.singleShot(900, self, self._auto_finish_if_alive)
            if hasattr(self, 'preview_panel') and self.preview_panel:
                self.preview_panel.show_warning(
                    T("已到达页面边缘，即将自动完成拼接…")
                )

    def _show_stitch_failure(self, frame_no: int, detail: str):
        """拼接失败提示（帧计数由工作线程维护，这里不再增减列表）。"""
        detail = detail or "拼接失败"
        message = f"第 {frame_no} 张图片拼接失败：{detail}"
        _log_stitch(T("🗑️ 忽略第 {frame_no} 张截图，等待下一次滚动", frame_no=frame_no))
        if hasattr(self, 'preview_panel') and self.preview_panel:
            self.preview_panel.update_count(len(self.screenshots))
        self._show_preview_warning(message)

    def _auto_finish_if_alive(self):
        """定时器到点后收尾；用户若已手动点过完成则窗口已不在，直接跳过。

        收尾前再做一次内容确认：如果画面又变了（用户恰好在超时点继续
        滚动），撤销收尾回到监视状态——误收尾会保存半成品，宁可晚一步。
        """
        if not (self._auto_finish_scheduled and self.isVisible()):
            return
        try:
            pixmap, sig = self._grab_region()
        except Exception:
            pixmap, sig = None, None
        if pixmap is not None and not self._sig_similar(sig, self._last_captured_sig):
            self._auto_finish_scheduled = False
            self._idle_started_at = None
            self._clear_preview_warning()
            _log_stitch(T("自动收尾前检测到画面变化，已撤销，继续监视"), force=True)
            return
        self._on_finish()
    
    @safe_event
    def paintEvent(self, event):
        """绘制窗口边框"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # 绘制半透明边框（在窗口边缘，不影响截图区域）
        pen = QPen(QColor(0, 120, 215), 3)  # 蓝色边框，3像素
        painter.setPen(pen)
        
        # 边框应该绘制在整个窗口的边缘
        # 窗口大小 = capture_rect + 边框(3px * 2)
        border_rect = QRect(
            1,  # 从窗口边缘开始
            1,
            self.width() - 2,  # 整个窗口宽度 - 2px（线宽的一半）
            self.height() - 2  # 整个窗口高度 - 2px
        )
        painter.drawRect(border_rect)
        
        painter.end()

    @safe_event
    def moveEvent(self, event):
        super().moveEvent(event)
        self._position_preview_panel()
        self._position_floating_toolbar()

    @safe_event
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_preview_panel()
        self._position_floating_toolbar()
    
    def _on_finish(self):
        """完成按钮点击"""
        _log_stitch(T("[OK] 完成长截图，共 {count} 张图片", count=len(self.screenshots)), force=True)
        
        # 横向模式：将拼接结果逆时针旋转90度还原
        # 只有在有2张及以上图片（发生了拼接）时才旋转
        # 如果只有1张图片，不需要旋转（第1张图片没有被旋转）
        # 向上/向左滚动模式：先翻转还原（必须在横向旋转之前）
        if (self.scroll_locked_direction == "up" and 
            self.stitched_result is not None and
            len(self.screenshots) >= 2):
            self.stitched_result = self.stitched_result.transpose(Image.FLIP_TOP_BOTTOM)
        
        # 横向模式：将拼接结果逆时针旋转90度还原
        if (self.scroll_direction == "horizontal" and 
            self.stitched_result is not None and 
            len(self.screenshots) >= 2):
            _log_stitch(T("🔄 横向模式：将拼接结果逆时针旋转90度还原（共{count}张）", count=len(self.screenshots)))
            _log_stitch(T("   旋转前尺寸: {w}x{h}", w=self.stitched_result.size[0], h=self.stitched_result.size[1]))
            self.stitched_result = self.stitched_result.rotate(90, expand=True)
            _log_stitch(T("   旋转后尺寸: {w}x{h}", w=self.stitched_result.size[0], h=self.stitched_result.size[1]))
        elif self.scroll_direction == "horizontal" and len(self.screenshots) == 1:
            _log_stitch(T("📸 横向模式：只有1张图片，无需旋转"))
        
        # 自动保存文件
        self._save_result()
        
        # 复制到剪贴板
        self._copy_to_clipboard()
        
        self._cleanup()
        self.finished.emit()
        self.close()
    
    def set_save_directory(self, directory):
        """设置保存目录"""
        self.save_directory = directory
    
    def _save_result(self):
        """提交拼接结果的异步保存任务"""
        if self.stitched_result is None:
            _log_stitch(T("[WARN] 没有拼接结果，跳过保存"))
            return

        direction_suffix = "横" if self.scroll_direction == "horizontal" else "縦"
        target_dir = self.save_directory

        try:
            task_path = self.save_service.save_pil_async(
                self.stitched_result,
                directory=target_dir,
                prefix="長スクショ",
                suffix=direction_suffix,
                image_format="PNG"
            )
            if task_path:
                _log_stitch(T("[SAVE] 长截图保存任务已提交: {task_path}", task_path=task_path))
            else:
                _log_stitch(T("[ERROR] 无法提交长截图保存任务"))
        except Exception as exc:
            _log_stitch(T("[ERROR] 提交长截图保存任务失败: {exc}", exc=exc))
            import traceback
            traceback.print_exc()

    def _copy_to_clipboard(self):
        """将拼接结果复制到剪贴板"""
        if self.stitched_result is None:
            return
            
        try:
            # 转换为 QImage
            image = self.stitched_result.convert("RGBA")
            width, height = image.size
            data = image.tobytes("raw", "RGBA")
            
            # 创建 QImage (引用 data)
            qimage = QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888)
            
            # 复制到剪贴板（必须使用 copy() 创建深拷贝，避免 data 被回收后崩溃）
            clipboard = QApplication.clipboard()
            clipboard.setImage(qimage.copy())
            _log_stitch(T("长截图已复制到剪贴板"))
        except Exception as e:
            _log_stitch(T("[ERROR] 复制到剪贴板失败: {e}", e=e))
            import traceback
            traceback.print_exc()
    
    def _on_manual_capture(self):
        """手动截图（从工具栏触发）"""
        try:
            _log_stitch(T("🖱️ 用户手动触发截图..."))
            # 立即执行截图
            self._do_capture()
        except Exception as e:
            _log_stitch(T("[ERROR] 手动截图失败: {e}", e=e), force=True)
            import traceback
            traceback.print_exc()
    
    def _on_pin(self):
        """钉图按钮点击 - 将当前拼接结果钉到桌面，然后结束长截图"""
        _log_stitch(T("钉图长截图结果..."))

        # 检查 config_manager
        if self.config_manager is None:
            _log_stitch(T("[ERROR] config_manager 未设置，无法创建钉图"))
            return

        # 获取拼接结果
        result_image = self.stitched_result

        if result_image is None:
            _log_stitch(T("[WARN] 没有拼接结果，无法钉图"))
            return
        
        # 向上/向左滚动模式：先翻转还原（必须在横向旋转之前）
        if (self.scroll_locked_direction == "up" and
            len(self.screenshots) >= 2):
            result_image = result_image.transpose(Image.FLIP_TOP_BOTTOM)
        
        # 横向模式：旋转结果
        if (self.scroll_direction == "horizontal" and 
            len(self.screenshots) >= 2):
            _log_stitch(T("🔄 横向模式：旋转图片..."))
            result_image = result_image.rotate(90, expand=True)
        
        # 转换 PIL Image 到 QImage（使用原图，不缩放）
        try:
            from PySide6.QtGui import QImage
            from PySide6.QtCore import QPoint
            
            image_rgba = result_image.convert("RGBA")
            width, height = image_rgba.size
            data = image_rgba.tobytes("raw", "RGBA")
            
            qimage = QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
            
            # 获取当前长截图窗口所在的屏幕（支持多屏幕）
            # 以长截图区域的中心为基准定位钉图窗口
            capture_center = self.capture_rect.center()
            pin_x = capture_center.x() - width // 2
            pin_y = capture_center.y() - height // 2
            
            position = QPoint(pin_x, pin_y)
            
            # 创建钉图（使用原图）
            from pin.pin_manager import PinManager
            pin_manager = PinManager.instance()
            
            # 不保留返回值：PinManager 自身会持有窗口引用，这里不需要
            pin_manager.create_pin(
                image=qimage,
                position=position,
                config_manager=self.config_manager,
                drawing_items=None,  # 长截图不继承绘制项目
                selection_offset=None
            )
            
            log_debug(T("钉图已创建，位置: ({pin_x}, {pin_y})", pin_x=pin_x, pin_y=pin_y), module=_MODULE_TAG)
            
            # 钉图完成后，清理并关闭长截图窗口
            self._cleanup()
            self.finished.emit()
            self.close()
            
        except Exception as e:
            _log_stitch(T("[ERROR] 创建钉图失败: {e}", e=e))
            import traceback
            traceback.print_exc()
    
    def _on_cancel(self):
        """取消按钮点击"""
        _log_stitch(T("[ERROR] 取消长截图"), force=True)
        self.screenshots.clear()
        self._cleanup()
        self.cancelled.emit()
        self.close()
    
    def _cleanup(self):
        """清理资源"""
        try:
            # 停止拼接工作线程。必须先断开结果信号再 join：窗口随后会被
            # 销毁（WA_DeleteOnClose），worker 处理完手头帧后会 emit——
            # 向已销毁的 QObject emit 是段错误。断开后迟到的回包无处可去，
            # join（限时）保证线程干净退出，不带着 Qt 引用存活。
            if hasattr(self, '_stitch_worker') and self._stitch_worker is not None:
                from core.qt_utils import safe_disconnect
                safe_disconnect(self.stitch_result_ready)
                self._stitch_worker.stop()
                self._stitch_worker.join(timeout=3.0)
                self._stitch_worker = None

            if hasattr(self, 'screenshots'):
                self.screenshots.clear()
                self.screenshots = []
            
            if hasattr(self, '_last_screenshot'):
                self._last_screenshot = None
            if hasattr(self, '_screenshot_count'):
                self._screenshot_count = 0
            
            if hasattr(self, 'stitched_result'):
                self.stitched_result = None
            
            import gc
            gc.collect()
                
            # 关闭浮动工具栏
            if hasattr(self, 'toolbar') and self.toolbar:
                try:
                    self.toolbar.close()
                    _log_stitch(T("[OK] 浮动工具栏已关闭"))
                except Exception as e:
                    _log_stitch(T("[WARN] 关闭工具栏时出错: {e}", e=e))
            
            # 关闭预览面板
            if hasattr(self, 'preview_panel') and self.preview_panel:
                try:
                    self.preview_panel.close()
                    _log_stitch(T("[OK] 预览面板已关闭"))
                except Exception as e:
                    _log_stitch(T("[WARN] 关闭预览面板时出错: {e}", e=e))
                finally:
                    self.preview_panel = None

            # 停止内容监视定时器
            if hasattr(self, '_watch_timer'):
                self._watch_timer.stop()

            if hasattr(self, '_position_fix_timer'):
                self._position_fix_timer.stop()

        except Exception as e:
            _log_stitch(T("[WARN] 清理资源时出错: {e}", e=e))
    
    @safe_event
    def closeEvent(self, event):
        """窗口关闭事件"""
        self._cleanup()
        super().closeEvent(event)
    
    def get_screenshots(self):
        """获取所有截图"""
        return self.screenshots
    
    def get_stitched_result(self):
        """获取实时拼接的结果图
        
        Returns:
            PIL.Image: 拼接好的完整图片，如果没有截图则返回None
            
        注意：
            - 竖向模式：返回原始拼接结果
            - 横向模式：返回旋转后的结果（在_on_finish中已处理）
        """
        return self.stitched_result
    
    def get_scroll_distances(self):
        """获取所有滚动距离记录
        
        Returns:
            List[int]: 滚动距离列表，每个元素表示相邻两张截图之间的估计滚动距离（像素）
        """
        return self.scroll_distances
 
