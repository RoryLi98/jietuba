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

import io
import time
import ctypes
import threading
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QApplication
from PySide6.QtCore import Qt, QRect, QTimer, Signal, QPoint, QThread
from PySide6.QtGui import QPainter, QPen, QColor, QPixmap, QGuiApplication, QImage
from typing import Optional
from PIL import Image, ImageChops, ImageStat

# 导入长截图拼接统一接口
from .jietuba_long_stitch_unified import (
    configure as long_stitch_configure,
    normalize_engine_value,
)

from settings import get_tool_settings_manager
from capture.capture_service import grab_region_hdr, grab_region_mss, uses_hdr_engine
from core.save import SaveService
from core import log_debug, log_info, safe_event
from core.logger import log_exception, T, LogMsg
from .scroll_toolbar import FloatingToolbar  # 浮动工具栏（独立模块）
from core.ui_theme import set_own_style
from core.shortcut_manager import ShortcutHandler, ShortcutManager

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

        qimage 由拼接工作线程生成：已按显示方向修正（横向模式旋转）、已缩小
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
    工作：QImage→PIL 转换、方向变换、感知哈希、行签名对齐与合成、预览
    缩略图（PIL 与 Rust 均释放 GIL，线程化有效）。

    拼接的规范状态是内存画布（_canvas，PIL RGB）+ 逐行分段签名
    （_canvas_sig，每行 24 字节）。新帧的落位由 Python 侧
    自己对齐判定（_align_candidates 召回 + _choose_dy 像素裁决），偏移是**算出来**
    的而不是让算法猜出来的，所以"重复拼接"在结构上不可能发生：

      * 新帧整幅落在画布内（往回滚过已截取的画面）→ 不动画布，跳过；
      * 新帧只在下方越出 → 画布向下增长；
      * 新帧在上方越出 → 画布向上增长（行号整体下移）。

    方向由此自动得出（新帧顶行 vs 上一帧顶行），不再依赖 Rust 的
    detect_direction，也不再需要"画布存翻转态、收尾再翻回来"的约定——
    画布始终是自然朝向，预览/收尾/钉图都不必翻转。

    Rust 的 longstitch 只作为兜底：行签名投票一个候选都凑不齐时（整页
    近乎纯色等）才交回去做一次自动方向拼接，结果若为 reverse 则翻回自然
    朝向后并入画布。

    状态权威在本线程：画布、帧计数、方向、去重哈希都在这里维护，处理完
    通过 emit 回调（窗口的 stitch_result_ready 信号，QueuedConnection）
    把结果载荷交回主线程。载荷不携带全图，只带预览缩略图与尺寸——主线程
    的 stitched_result 在收尾时才落地。整图级操作只剩两处：节流后的预览
    缩略图（默认 0.4s 一次）与收尾时的最终成图（finalize 任务取一次）。
    """

    # 预览缩略图长边上限（面板固定边 210px 的 2 倍，HiDPI 下仍清晰）
    _PREVIEW_LONG_SIDE = 420
    # 预览全图解码的节流间隔（O(结果高度) 的解码，快速连拍时不逐帧做）
    _PREVIEW_MIN_INTERVAL_S = 0.4

    def __init__(self, scroll_direction, locked_direction, duplicate_threshold, emit_fn):
        super().__init__(name="StitchWorker", daemon=True)
        import queue
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._emit = emit_fn
        self.scroll_direction = scroll_direction
        self._locked = locked_direction
        self.duplicate_threshold = duplicate_threshold
        # 规范拼接状态：内存画布 + 逐行分段签名（每行 24 字节，忽略右侧
        # 滚动条区域后计算，供落位对齐用）
        self._canvas = None
        self._canvas_sig = None
        self._canvas_png_cache = None  # 画布 PNG 字节缓存（见 _canvas_png），画布变更时置空
        self._stitched_w = 0
        self._stitched_h = 0
        # 上一帧的顶行与相邻两次落位的行差（仅用于候选平局时的惯性裁决）
        self._prev_row = 0
        self._prev_delta = 0
        self._count = 0
        self._prev_hash = None
        self._duplicate_count = 0
        self._final_image = None    # finalize 任务取出的最终成图
        self._last_preview_at = 0.0
        # 候选投票与行校验的原生实现（j-stitch ≥ Aligner）：全画布键索引每帧
        # 重建是 O(画布行数) 的 Python 字典循环，画布上万行后是帧管线大头。
        # 旧版 j-stitch 没有该类时回落纯 Python 实现（_align_candidates 的
        # Python 分支保留为行为基准）。
        self._aligner = None
        try:
            import longstitch
            self._aligner = longstitch.Aligner(
                sig_bytes=self._SIG_BYTES,
                key_seg_a=self._KEY_SEG_A,
                key_seg_b=self._KEY_SEG_B,
                bucket_cap=self._BUCKET_CAP,
                n_candidates=self._CANDIDATES,
                pix_candidates=self._PIX_CANDIDATES,
                verify_rows=self._SIG_VERIFY_ROWS,
                match_min_rows=self._MATCH_MIN_ROWS,
                match_min_ratio=self._MATCH_MIN_RATIO,
                row_match_max=self._ROW_MATCH_MAX,
                min_overlap=self._MIN_OVERLAP,
            )
        except AttributeError:
            pass
        # 积压计数（submit 加、处理完减），供主线程背压判断
        self._pending_lock = threading.Lock()
        self._pending = 0
        self._processing = False

    def submit(self, qimage, direction_hint):
        """提交一帧。qimage 的所有权移交给本线程，主线程不得再修改。"""
        with self._pending_lock:
            self._pending += 1
        self._queue.put({
            "qimage": qimage,
            "direction_hint": direction_hint,
        })
        if not self.is_alive():
            self.start()

    def submit_preview_refresh(self):
        """请求一次预览刷新（只解码当前拼接状态出缩略图，不动帧计数）。"""
        with self._pending_lock:
            self._pending += 1
        self._queue.put({"preview_only": True})
        if not self.is_alive():
            self.start()

    def submit_finalize(self):
        """请求解码最终成图（收尾握手：之后经 get_final_image 取用）。"""
        with self._pending_lock:
            self._pending += 1
        self._queue.put({"finalize": True})
        if not self.is_alive():
            self.start()

    def pending(self) -> int:
        """排队中 + 处理中的任务数（主线程背压依据）。"""
        with self._pending_lock:
            return self._pending

    def is_busy(self) -> bool:
        with self._pending_lock:
            return self._pending > 0 or self._processing

    def get_final_image(self):
        """收尾握手的结果：finalize 任务完成后可取（未完成时为 None）。"""
        return self._final_image

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
            with self._pending_lock:
                self._processing = True
            try:
                self._process(job)
            except Exception as e:
                log_exception(e, T("拼接工作线程处理帧失败"))
                _log_stitch(T("[ERROR] 拼接线程异常: {e}", e=e), force=True)
            finally:
                with self._pending_lock:
                    self._processing = False
                    self._pending = max(0, self._pending - 1)

    def _process(self, job):
        if job.get("finalize"):
            # 收尾握手：把规范画布取成最终成图（拷一份，画布归本线程所有）
            canvas = self._decode_stitched()
            self._final_image = None if canvas is None else canvas.copy()
            return
        if job.get("preview_only"):
            # preview_only 必须透传到回包：主线程 _apply_stitch_result 靠它
            # 走纯预览早退（不动计数/增益/到底提示）。漏了这个键，空闲补刷
            # 会被当成"又拼了一帧"——重复打 📸 日志、往 scroll_distances 里
            # 塞 0 增益，还会把"即将自动完成"的提示提前抹掉。
            self._emit(self._build_payload(ok=True, preview_force=True, preview_only=True))
            return

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

        # 方向变换：横向旋转成"竖向"以复用对齐/合成算法。
        # 画布不做翻转——自然朝向下落位方向本身就说明了滚动方向。
        if self.scroll_direction == "horizontal" and not is_first_image:
            pil_image = pil_image.rotate(-90, expand=True)

        # 到底检测：帧间相似度与拼接成败无关，本帧截取后立即计算
        current_hash = ScrollCaptureWindow._calculate_image_hash(self, pil_image)
        prev_hash = self._prev_hash
        self._prev_hash = current_hash
        frame_is_duplicate = (
            prev_hash is not None
            and ScrollCaptureWindow._images_are_similar(self, prev_hash, current_hash)
        )

        ok = True
        # 整帧落在已有画布内（往回滚过已截取的画面）：无新内容可加，
        # 画布原样保留、这帧不计数，但也不算失败
        no_new_content = False
        error_detail = None
        try:
            if is_first_image:
                self._set_canvas(pil_image)
            else:
                if screenshot_count == 2 and self.scroll_direction == "horizontal":
                    # 横向模式：首帧旋成竖向，此后所有帧同一坐标系。
                    # （与旧行为一致：只在第 2 帧补这一次旋转）
                    self._set_canvas(self._canvas.rotate(-90, expand=True))

                status = self._place_frame(pil_image)
                if status == "skip":
                    no_new_content = True
                elif status != "ok":
                    _log_stitch(T("[WARN] 第 {screenshot_count} 张拼接失败，未找到重叠区域", screenshot_count=screenshot_count), force=True)
                    ok = False
                    error_detail = "未找到可靠的重叠区域"

        except Exception as e:
            _log_stitch(T("[WARN] 第 {screenshot_count} 张拼接出错: {e}", screenshot_count=screenshot_count, e=e), force=True)
            import traceback
            traceback.print_exc()
            ok = False
            error_detail = f"算法异常：{e}"
            if self._canvas is None:
                self._set_canvas(pil_image)

        if not ok or no_new_content:
            # 失败帧 / 无新内容的帧不计入总数
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

        self._emit(self._build_payload(
            ok,
            preview_force=not ok or auto_finish,
            error_detail=error_detail,
            failed_frame_no=screenshot_count if not ok else 0,
            auto_finish=auto_finish,
        ))

    # ── 对齐与合成 ──

    # 行签名：把每行横向切成 _SIG_SEGMENTS 段、逐段取 RGB 均值（BOX 缩放，
    # C 级实现）。纯「整行均值」在文字页上只有十来个取值，撞桶撞得一塌糊涂
    # ——Rust 的 LCS 正是因此选出错位，表现为重复拼接；分段之后行与行的差异
    # 体现在墨迹的横向分布上，信息量完全不同。
    _SIG_SEGMENTS = 8
    _SIG_BYTES = _SIG_SEGMENTS * 3          # 每行签名字节数
    # 候选键取第 2、5 段的 RGB、量化到 16 级（>>4）：共 6 个半字节。
    # 桶宽 16 容忍 ±5 的抗锯齿抖动；只取 2 段（而非全部 24 个值）是为了不让
    # 抖动把键打散——键一散召回就没了，噪声却仍只有 (1/16)^6 量级。
    _KEY_SEG_A = 2
    _KEY_SEG_B = 5
    # 同一个键在画布里最多登记的行数：投票代价锁死在「帧高 × 上限」内、与
    # 画布长度无关；纯白这类高频行因此不再发言（它们只会造出噪声候选）
    _BUCKET_CAP = 64
    # 进入逐行复核的候选偏移数（按票数取前 N，再补一个惯性估计）
    _CANDIDATES = 12
    # 复核：一行 _SIG_BYTES 个分段均值的绝对差之和 ≤ 此值（均值偏差 ≤12）
    _ROW_MATCH_MAX = 12 * _SIG_SEGMENTS * 3
    # 复核通过所需的最少匹配行数与占比
    _MATCH_MIN_ROWS = 12
    _MATCH_MIN_RATIO = 0.5
    # 重叠太短就不信这个对齐结果（交回 Rust 兜底）
    _MIN_OVERLAP = 20
    # 逐行复核最多检查多少行（重叠区**均匀抽样**，不足则全查）。候选数 ×
    # 重叠长度是纯 Python 成本的大头，画布越长越慢；抽样之后每帧的复核开销
    # 与画布长度脱钩——这是"越拼越卡"的根源。
    _SIG_VERIFY_ROWS = 240

    # ── 像素级裁决 ──
    # 签名只负责「召回」（把可能的偏移列出来），对不对必须由真实像素说了算：
    # 分段均值是行的压缩描述，文字页上相邻行、同一行的左右两半、卡片间隔行
    # 经常有完全相同的分段均值——签名会把差一两行的错位判成「对上了」，这正
    # 是用户看到的错缝。判分用重叠区的平均单通道绝对差：
    #   · 对得准 → 两次抓屏的重叠像素几乎逐位相同，分数 ≈ 0；
    #   · 差一两行 → 文字整体错开，分数动辄几十，一望即知。
    # 判分先用小窗（中心列带）粗筛全部候选，再用大窗（整宽）复核前几名，
    # 两次都是 PIL 的 C 实现，成本与画布长度无关。
    _PIX_CANDIDATES = 3              # 进入像素裁决的签名候选数（按得分取前 K）
    _PIX_REFINE = 2                  # 冠军候选上下各试这么多行（捞回差一两行的答案）
    _PIX_WIN_ROWS = 160              # 粗筛窗口行数（取重叠区中部）
    _PIX_WIN_WIDTH = 400             # 粗筛窗口宽度（水平居中）
    _PIX_FINAL_ROWS = 320            # 复核窗口行数（整宽，右侧滚动条除外）
    _PIX_FINALISTS = 4               # 进入复核的候选数（3 个签名候选各一 + 1 个细化余量）
    _PIX_ACCEPT = 16.0               # 复核分数上限：超过就不信这个偏移
    _PIX_AMBIG_ABS = 1.5             # 与最高分相差在此以内 → 视为「一样好」
    _PIX_AMBIG_REL = 1.35            # 或不超过最高分的此倍数（取两者中更宽的）

    @staticmethod
    def _ignore_right() -> int:
        """右侧有多少像素不参与比较（滚动条/窗口阴影），取不到配置时用 20。"""
        try:
            from .jietuba_long_stitch_unified import config
            return int(config.ignore_right_pixels or 20)
        except Exception:
            return 20

    @classmethod
    def _row_signatures(cls, im) -> bytes:
        """逐行分段均值签名，每行 _SIG_BYTES 字节。右侧滚动条不参与均值。"""
        w, h = im.size
        ignore = cls._ignore_right()
        if ignore and w > ignore:
            im = im.crop((0, 0, w - ignore, h))
        return im.resize((cls._SIG_SEGMENTS, h), Image.Resampling.BOX).tobytes()

    @classmethod
    def _key_of(cls, sig: bytes, row: int) -> int:
        """该行的候选键：两段 RGB 各取高 4 位，拼成 6 个半字节。"""
        a = row * cls._SIG_BYTES + cls._KEY_SEG_A * 3
        b = row * cls._SIG_BYTES + cls._KEY_SEG_B * 3
        return (
            ((sig[a] >> 4) << 20) | ((sig[a + 1] >> 4) << 16) | ((sig[a + 2] >> 4) << 12)
            | ((sig[b] >> 4) << 8) | ((sig[b + 1] >> 4) << 4) | (sig[b + 2] >> 4)
        )

    @classmethod
    def _row_matches(cls, frame_sig: bytes, j: int, canvas: bytes, i: int) -> bool:
        """帧的第 j 行与画布的第 i 行是不是同一行内容（带容差，超预算即否）。"""
        a = j * cls._SIG_BYTES
        b = i * cls._SIG_BYTES
        total = 0
        for k in range(cls._SIG_BYTES):
            d = frame_sig[a + k] - canvas[b + k]
            total += d if d > 0 else -d
            if total > cls._ROW_MATCH_MAX:
                return False
        return True

    def _set_canvas(self, im):
        """设定规范画布（首帧 / 横向旋转 / Rust 兜底并入）。"""
        self._canvas = im
        self._canvas_sig = self._row_signatures(im)
        self._canvas_png_cache = None
        self._stitched_w, self._stitched_h = im.size
        # 画布换了坐标系，行号基准与滚动惯性重新起算
        self._prev_row = 0
        self._prev_delta = 0

    def _canvas_png(self) -> bytes:
        """画布 PNG 字节（缓存）。

        兜底路径可能连续多帧走到这里（近纯色页面签名凑不出候选），画布没变
        就不必每帧重新编码整张图——画布随帧数线性变大，不缓存的话那是 O(n²)
        的编码开销。仅拼接工作线程访问，无需加锁。
        """
        if self._canvas_png_cache is None:
            self._canvas_png_cache = self._encode_png(self._canvas)
        return self._canvas_png_cache

    def _align_candidates(self, frame_sig: bytes, skip_top: int = 0) -> list:
        """在画布行签名里找出新帧顶行的候选偏移 dy（画布行号，可为负）。

        候选生成靠量化键投票：每对「键相同」的行对 (画布 i, 帧 j) 都为偏移
        i−j 投一票——一次遍历就得到所有偏移的匹配计数，不用按偏移扫描，代价
        O(帧高 × 桶上限)。候选裁决再把重叠区逐行复核（带容差，均匀抽样封顶
        _SIG_VERIFY_ROWS 行），得分 = 匹配行数 − 未匹配行数的一半。

        返回排序后的前 _PIX_CANDIDATES 个 (dy, score)；一个都不合格时返回空表
        ——是否真的放弃由调用方结合像素裁决决定。

        **排序不看分数绝对值**：得分公式「匹配行 − 0.5×未匹配行」天然偏爱长
        重叠，重复版式里往回挪一个周期的错位偏移分数反而更高，真答案会在进
        像素裁决前就被挤出前 K。改按命中率（对重叠长度不敏感）排序，同命中
        率时按离惯性估计的距离排——滚动是连续的，等价的偏移里离「上一帧 +
        上一步步长」最近的那个才是本帧的真身。
        """
        canvas = self._canvas_sig
        if not canvas or not frame_sig:
            return []
        ch = len(canvas) // self._SIG_BYTES
        fh = len(frame_sig) // self._SIG_BYTES
        if ch <= 0 or fh <= 0:
            return []

        # 原生路径：投票与校验在 j-stitch 内完成（语义与下方 Python 分支一致）
        aligner = getattr(self, "_aligner", None)
        if aligner is not None:
            return aligner.find_candidates(
                canvas, frame_sig, skip_top, self._prev_row + self._prev_delta,
            )

        # ── 纯 Python 分支（旧版 j-stitch 的行为基准，勿改语义） ──
        # 画布：键 → 行号（每键封顶）
        index = {}
        for i in range(ch):
            key = self._key_of(canvas, i)
            pos = index.get(key)
            if pos is None:
                index[key] = [i]
            elif len(pos) < self._BUCKET_CAP:
                pos.append(i)

        # 本帧：键 → 出现次数（帧内高频行不发言，纯白行只造噪声候选）
        freq = {}
        for j in range(fh):
            key = self._key_of(frame_sig, j)
            freq[key] = freq.get(key, 0) + 1

        votes = {}
        for j in range(max(0, skip_top), fh):
            key = self._key_of(frame_sig, j)
            if freq[key] > self._BUCKET_CAP:
                continue
            for i in index.get(key, ()):
                d = i - j
                votes[d] = votes.get(d, 0) + 1

        # 惯性估计：滚动是连续的，上一步的步长是「一个票都没有」时最靠谱的猜
        expected = self._prev_row + self._prev_delta
        ranked = [dy for dy, _ in sorted(
            votes.items(),
            key=lambda kv: (-kv[1], abs(kv[0] - expected)),
        )[:self._CANDIDATES]]
        if expected not in ranked:
            ranked.append(expected)

        out = []
        for dy in ranked:
            # 固定标题（ignore_top_pixels）是叠在帧顶上的，它下面的画布行是
            # 另一处页面内容，本来就不该算进复核
            lo = max(0, -dy, skip_top)
            hi = min(fh, ch - dy)
            overlap = hi - lo
            if overlap < min(self._MIN_OVERLAP, fh):
                continue
            step = max(1, overlap // self._SIG_VERIFY_ROWS)
            matches = checked = 0
            for j in range(lo, hi, step):
                checked += 1
                if self._row_matches(frame_sig, j, canvas, dy + j):
                    matches += 1
            if matches < min(self._MATCH_MIN_ROWS, checked) or matches < checked * self._MATCH_MIN_RATIO:
                continue
            # 未匹配行按半票扣分：免得「短重叠 100%」压过「长重叠 95%」
            score = matches - 0.5 * (checked - matches)
            out.append((matches / checked, abs(dy - expected), score, dy))

        out.sort(key=lambda t: (-t[0], t[1], -t[2]))
        return [(dy, s) for _ratio, _dist, s, dy in out[:self._PIX_CANDIDATES]]

    def _pixel_score(self, frame, dy: int, skip_top: int, final: bool) -> Optional[float]:
        """偏移 dy 的像素级可信度：重叠区平均单通道绝对差（越小越可信）。

        final=False 用中心列带小窗粗筛（快），final=True 用整宽大窗复核（准）。
        重叠不足时返回 None，表示这个偏移没法判。
        """
        canvas = self._canvas
        if canvas is None:
            return None
        cw, ch = canvas.size
        fh = frame.size[1]
        lo = max(0, -dy, skip_top)
        hi = min(fh, ch - dy)
        if hi - lo < min(self._MIN_OVERLAP, fh):
            return None

        usable = cw - self._ignore_right() if cw > self._ignore_right() else cw
        if usable <= 0:
            return None
        rows = min(self._PIX_FINAL_ROWS if final else self._PIX_WIN_ROWS, hi - lo)
        y0 = lo + max(0, (hi - lo - rows) // 2)
        y1 = y0 + rows
        if final:
            x0, x1 = 0, usable
        else:
            band = min(self._PIX_WIN_WIDTH, usable)
            x0 = max(0, (usable - band) // 2)
            x1 = x0 + band
        a = frame.crop((x0, y0, x1, y1))
        b = canvas.crop((x0, dy + y0, x1, dy + y1))
        return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3.0

    def _choose_dy(self, frame, candidates: list, skip_top: int) -> Optional[int]:
        """像素级裁决：从签名候选里挑出真正对得上的偏移，对不上返回 None。

        四件事：

        1. **局部细化** —— 冠军候选上下各试 _PIX_REFINE 行。签名给的是「哪些行
           的分段均值撞上了」，文字页上它经常整体偏一两行；真实像素的局部极小
           值才是答案。
        2. **复核名额** —— 每个签名候选先送一个粗筛冠军进大窗复核，名额有余再
           补细筛里的次优偏移。重复版式里所有偏移的像素分都≈0，若按分数直接取
           前几名，冠军的细化窗口会独占名额，别的候选挤不进来，惯性裁决也就无从
           谈起——这正是「往回挪一个周期」的错位被选中的通道。
        3. **真伪判定** —— 全部候选的分数都超过 _PIX_ACCEPT 就不采纳，交回
           Rust 兜底（宁可少拼一张，也不要把错位写进画布）。
        4. **同分裁决** —— 重复版式（列表、卡片、表格）里两个偏移的像素分几乎
           一样，此时按滚动惯性取离「上一帧位置 + 上一步步长」近的那个。选错了
           画布会长高却重复一段已有内容，那是用户最容易看见的错。
        """
        if not candidates:
            return None
        expected = self._prev_row + self._prev_delta

        trials = []
        seen = set()
        for rank, (dy, _score) in enumerate(candidates):
            span = self._PIX_REFINE if rank == 0 else 0
            for delta in range(-span, span + 1):
                trial = dy + delta
                if trial not in seen:
                    seen.add(trial)
                    trials.append((trial, rank, abs(delta)))

        coarse = []
        for trial, rank, near in trials:
            score = self._pixel_score(frame, trial, skip_top, final=False)
            if score is not None:
                coarse.append((score, near, rank, trial))
        if not coarse:
            return None
        # 同分时先取签名原点（near 小的）：细化偏移只有真的更准时才顶掉它
        coarse.sort(key=lambda t: (t[0], t[1], t[2]))

        champions = {}          # rank → 该候选粗筛最好的那条
        for score, _near, rank, trial in coarse:
            if rank not in champions:
                champions[rank] = (score, rank, trial)
        finalists = sorted(champions.values(), key=lambda t: (t[0], t[1]))
        picked = {(t[1], t[2]) for t in finalists}
        for score, _near, rank, trial in coarse:    # 名额有余：补细化偏移的次优解
            if len(finalists) >= self._PIX_FINALISTS:
                break
            if (rank, trial) not in picked:
                picked.add((rank, trial))
                finalists.append((score, rank, trial))

        scored = []
        for score, rank, trial in finalists:
            final_score = self._pixel_score(frame, trial, skip_top, final=True)
            scored.append((final_score if final_score is not None else score, rank, trial))
        scored.sort(key=lambda t: (t[0], t[1]))

        best_score = scored[0][0]
        if best_score > self._PIX_ACCEPT:
            return None
        band = [
            t for t in scored
            if t[0] <= max(best_score + self._PIX_AMBIG_ABS, best_score * self._PIX_AMBIG_REL)
        ]
        if self._prev_delta and len(band) > 1:
            # 惯性已经建立（不是第一帧）：同分时信惯性
            return min(band, key=lambda t: abs(t[2] - expected))[2]
        return band[0][2]

    def _place_frame(self, pil_image) -> str:
        """把新帧放进取向自然的画布。返回 "ok" / "skip" / "fail"。

        落位由 _align 算出的偏移直接决定：整幅被覆盖就什么都不做（结构上
        不可能产生重复内容），否则按偏移向上补行 / 向下延展后 paste。
        """
        canvas = self._canvas
        if canvas is None:
            self._set_canvas(pil_image)
            return "ok"

        cw, chh = canvas.size
        fw, fh = pil_image.size
        if fw != cw:
            # 抓取区域宽度变了：等比缩到画布宽度，保证逐行可比
            pil_image = pil_image.resize(
                (cw, max(1, int(round(fh * cw / fw)))),
                Image.Resampling.LANCZOS,
            )
            fw, fh = pil_image.size

        frame_sig = self._row_signatures(pil_image)
        skip_top = 0
        try:
            from .jietuba_long_stitch_unified import config
            skip_top = int(config.ignore_top_pixels or 0)
        except Exception:
            skip_top = 0

        candidates = self._align_candidates(frame_sig, skip_top)
        if not candidates:
            # 签名一条候选都没凑出来（抓屏抖动把分段均值打散、整页近乎纯色、
            # 或重叠太短）时，仍给惯性估计一次机会：真实像素判得动就用它，
            # 判不动再交回 Rust。_pixel_score 对「重叠不够」返回 None，
            # 所以这条兜路不会把明显不成立的偏移放进来。
            candidates = [(self._prev_row + self._prev_delta, 0.0)]

        dy = self._choose_dy(pil_image, candidates, skip_top)
        if dy is None:
            return self._rust_fallback(pil_image)
        prev_row = self._prev_row
        if dy >= 0 and dy + fh <= chh:
            # 整幅新帧都已被画布覆盖（往回滚过已截取的画面）：画布原样保留
            self._prev_delta = dy - prev_row
            self._prev_row = dy
            return "skip"

        ext = -dy if dy < 0 else 0                 # 上方需要补出的行数
        new_h = ext + max(chh, dy + fh)            # 补上 + 向下延展后的总高
        grown = Image.new("RGB", (cw, new_h))
        grown.paste(canvas, (0, ext))
        grown.paste(pil_image, (0, dy + ext))

        # 行签名与像素同构：先放旧画布，再用新帧覆盖它所在的区段
        sb = self._SIG_BYTES
        sig = bytearray(new_h * sb)
        sig[ext * sb:(ext + chh) * sb] = self._canvas_sig
        sig[(dy + ext) * sb:(dy + ext + fh) * sb] = frame_sig

        self._canvas = grown
        self._canvas_sig = bytes(sig)
        self._canvas_png_cache = None
        self._stitched_w, self._stitched_h = cw, new_h
        self._prev_delta = dy - prev_row
        self._prev_row = dy + ext

        # 滚动方向自动判定：新帧顶行低于上一帧 → 向下滚动
        direction = "down" if dy >= prev_row else "up"
        if direction != self._locked:
            if self._locked is None:
                _log_stitch(T(
                    "{arrow} 自动检测到滚动方向，已锁定（结果 {w}x{h}）",
                    arrow="⬇️" if direction == "down" else "⬆️", w=cw, h=new_h,
                ))
            else:
                _log_stitch(T(
                    "检测到滚动方向反转，已切换为 {dir} 向拼接（结果 {w}x{h}）",
                    dir="上" if direction == "up" else "下", w=cw, h=new_h,
                ), force=True)
            self._locked = direction
        return "ok"

    def _rust_fallback(self, pil_image) -> str:
        """行签名对齐凑不出可信候选时的兜底：交给 Rust 做一次自动方向拼接。

        Rust 的 reverse 结果是翻转态（对翻转图片拼接的产物），翻回自然朝向
        再并入画布——画布始终自然朝向，这是与旧实现的关键差异。

        返回 "ok" / "skip" / "fail"。
        """
        import longstitch
        from .jietuba_long_stitch_unified import config

        auto = longstitch.stitch(
            self._canvas_png(),
            self._encode_png(pil_image),
            detect_direction=True,
            # config 默认 0 一向被当作「用库的默认值 20」处理
            ignore_right_pixels=config.ignore_right_pixels or 20,
            ignore_top_pixels=config.ignore_top_pixels,
        )
        if auto is None:
            return "fail"

        result = Image.open(io.BytesIO(auto.png)).convert("RGB")
        if auto.direction == "reverse":
            result = result.transpose(Image.FLIP_TOP_BOTTOM)
        if result.size[1] <= self._stitched_h:
            # 结论把画布裁短或没长高（等价于没有新内容）：一律不采纳
            return "skip"

        self._set_canvas(result)
        self._locked = "up" if auto.direction == "reverse" else "down"
        _log_stitch(T(
            "⚠️ 行签名对齐未命中，已改用 Rust 自动方向拼接（结果 {w}x{h}）",
            w=result.size[0], h=result.size[1],
        ), force=True)
        return "ok"

    # ── 编解码与载荷辅助 ──

    @staticmethod
    def _encode_png(pil_image) -> bytes:
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return buf.getvalue()

    def _decode_stitched(self):
        """当前拼接状态：内存画布本身（无需解码，调用方不得原地修改）。"""
        return self._canvas

    def _build_payload(self, ok: bool, preview_force: bool = False,
                       error_detail=None, failed_frame_no: int = 0,
                       auto_finish: bool = False, preview_only: bool = False) -> dict:
        """组装回主线程的载荷。全图不进载荷——预览缩略图按节流解码。"""
        now = time.monotonic()
        preview_qimage = None
        if ok and (preview_force
                   or now - self._last_preview_at >= self._PREVIEW_MIN_INTERVAL_S):
            self._last_preview_at = now
            preview_qimage = self._make_preview_image()
        return {
            "ok": ok,
            "preview_only": preview_only,
            "preview_qimage": preview_qimage,
            "locked_direction": self._locked,
            "screenshot_count": self._count,
            "failed_frame_no": failed_frame_no,
            "auto_finish": auto_finish,
            "error_detail": error_detail,
            "width": self._stitched_w,
            "height": self._stitched_h,
        }

    def _make_preview_image(self):
        """生成显示方向正确的预览缩略图（QImage）。

        画布是自然朝向，这里只剩横向模式的旋转；缩略 + 旋转都发生在
        节流后的这一次。resize 返回新图且 reducing_gap 分级降采样，
        不必像 thumbnail() 那样先整幅拷贝画布（长图一次 170MB 纯 memcpy）。
        """
        display = self._decode_stitched()
        if display is None:
            return None
        if self.scroll_direction == "horizontal" and self._count >= 2:
            display = display.rotate(90, expand=True)

        target = self._PREVIEW_LONG_SIDE
        scale = min(target / display.width, target / display.height, 1.0)
        thumb_w = max(1, round(display.width * scale))
        thumb_h = max(1, round(display.height * scale))
        thumb = display.resize(
            (thumb_w, thumb_h),
            Image.Resampling.BILINEAR,
            reducing_gap=2.0,
        )
        rgba = thumb.convert("RGBA")
        data = rgba.tobytes("raw", "RGBA")
        return QImage(
            data, rgba.width, rgba.height, rgba.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()  # copy() 脱离 data 的生存期


class ScrollCaptureShortcutHandler(ShortcutHandler):
    """长截图窗口的键盘入口（优先级 90）。

    长截图启动时截图窗口已经关闭，优先级 100 的 ScreenshotShortcutHandler
    随之注销，这里就是整个会话里唯一活跃的键盘 handler：

      * ESC    → 取消（同「取消」按钮；收尾进行中时只记取消请求）
      * Ctrl+C → 结束并复制（同「完成」按钮：落地在途帧 → 拼出最终图 →
                 自动保存 → 复制到剪贴板 → 关窗）
      * 确认键 → 同 Ctrl+C（默认 Enter，可在设置里改）

    没有 handler 时长截图是「只能点按钮」的：Ctrl+C 是系统级复制热键，
    不被消费就永远到不了窗口，用户想按它结束截图只会按下一份「复制」。
    """

    def __init__(self, window):
        self._window = window
        from core.shortcut_manager import load_inapp_bindings
        # 一次性读取：长截图会话只有几十秒，期间配置不会变
        self._bindings = load_inapp_bindings(["inapp_confirm"])

    @property
    def priority(self) -> int:
        return 90

    @property
    def handler_name(self) -> str:
        return "ScrollCaptureWindow"

    def is_active(self) -> bool:
        w = self._window
        if w is None:
            return False
        try:
            if not w.isVisible():
                return False
        except RuntimeError:
            # C++ 对象已被 WA_DeleteOnClose 销毁，迟到的按键走到这里
            return False
        # 长截图里弹出的模态对话框要让出键盘，否则在对话框里按 ESC 会
        # 把整次截图取消掉
        return QApplication.activeModalWidget() is None

    def handle_key(self, event) -> bool:
        from core.shortcut_manager import event_is_auto_repeat, event_key, match_inapp_binding

        # 按住不放只算一次：_on_finish/_on_cancel 各有防重入，但没必要
        # 让后续重复事件再走一遍分发
        if event_is_auto_repeat(event):
            return False
        key = event_key(event)
        w = self._window

        # ESC — 同截图会话，固定不可自定义
        if key == Qt.Key.Key_Escape:
            w._on_cancel()
            return True

        # Ctrl+C — 只认裸 Ctrl+C，别顺手吃掉 Ctrl+Shift+C / Ctrl+Alt+C
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key == Qt.Key.Key_C and mods == Qt.KeyboardModifier.ControlModifier:
            w._on_finish()
            return True

        # 确认键（默认 Enter）— 与 Ctrl+C 同义
        if match_inapp_binding(event, "inapp_confirm", self._bindings):
            w._on_finish()
            return True

        return False


class _WatchGrabber(QThread):
    """长截图的内容监视线程：抓帧与签名挪出 GUI 线程。

    GUI 线程忙（收尾保存、OCR、翻译）时定时器会延迟，监视节奏跟着拖，
    快速滚动就漏帧。抓帧走 HDR 会话或 mss BitBlt——两者都能在任意线程
    调用（QScreen.grabWindow 不行，所以回退路径用 mss）；签名在 QImage
    上算，同样可跨线程。只保留最新一帧：GUI 追上时拿到的就是当前画面，
    与主线程的背压语义一致，中间帧本就该跳过。
    """

    def __init__(self, capture_rect, signature_size, idle_ms, parent=None):
        super().__init__(parent)
        self._capture_rect = QRect(capture_rect)
        self._signature_size = signature_size
        self._use_hdr = uses_hdr_engine()
        self._lock = threading.Lock()
        self._interval_ms = max(20, int(idle_ms))
        self._sample = None  # (QImage, sig)

    def set_interval(self, ms):
        with self._lock:
            self._interval_ms = max(20, int(ms))

    def latest(self):
        """取走最新样本；没有新样本（抓帧失败或未轮转完）返回 None。"""
        with self._lock:
            sample, self._sample = self._sample, None
        return sample

    def run(self):
        # 任何一轮抓帧/签名抛意外异常都只丢弃本轮，监视线程不能悄悄死掉——
        # 它一死整个内容感知长截图就断了采集来源，且无人察觉
        last_error_log = 0.0
        while not self.isInterruptionRequested():
            with self._lock:
                interval = self._interval_ms
            try:
                image_sig = self._grab_and_sign()
                if image_sig is not None:
                    with self._lock:
                        self._sample = image_sig
            except Exception as e:
                # HDR→mss 回退在 _grab 内部处理；到这里的是意外异常
                # （显示器拔掉、驱动报错等）。限流记日志，按当前节奏重试。
                now = time.monotonic()
                if now - last_error_log > 3.0:
                    last_error_log = now
                    _log_stitch(T("[WARN] 监视抓帧异常，本轮跳过: {e}", e=e), force=True)
            self._interruptible_sleep(interval)

    def _interruptible_sleep(self, interval_ms):
        """分片睡眠：停止请求与「间隔调小」都能及时生效。

        空闲档（最长 1200ms）里内容突然动起来时，_set_watch_interval 会把
        间隔调回 90ms——入睡后对比当前值，发现调小就立即醒来抓帧，首帧
        延迟从秒级回到 ~50ms；调大不打断，免得节奏被频繁变更搅乱。
        """
        end = time.monotonic() + interval_ms / 1000.0
        while not self.isInterruptionRequested():
            with self._lock:
                current = self._interval_ms
            if current < interval_ms:
                break
            remain = end - time.monotonic()
            if remain <= 0:
                break
            self.msleep(min(50, max(1, int(remain * 1000))))

    def _grab_and_sign(self):
        image = self._grab()
        if image is None:
            return None
        return image, self._signature(image)

    def _grab(self):
        """抓一帧 capture_rect；HDR 优先，失败本线程内退回 mss。测试从这注入假帧。"""
        image = None
        if self._use_hdr:
            try:
                image = grab_region_hdr(self._capture_rect)
            except Exception as e:
                self._use_hdr = False
                _log_stitch(T("HDR 抓帧失败，本会话改用 GDI: {error}", error=e), force=True)
        if image is None or image.isNull():
            try:
                image = grab_region_mss(self._capture_rect)
            except Exception as e:
                _log_stitch(T("[WARN] 监视抓帧失败: {e}", e=e), force=True)
                return None
        return None if image.isNull() else image

    def _signature(self, image):
        """内容的降采样签名：32x32 ARGB32 的全部字节拼成一个大整数（与主线程 _signature 同构）。

        正常路径收 QImage；测试注入的替身可能是 QPixmap，一并兼容。
        """
        size = self._signature_size
        small = image.scaled(
            size, size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        if isinstance(small, QPixmap):
            small = small.toImage()
        if small.format() != QImage.Format.Format_ARGB32:
            small = small.convertToFormat(QImage.Format.Format_ARGB32)
        bits = small.constBits()
        return int.from_bytes(bytes(bits[: size * size * 4]), "little")


class ScrollCaptureWindow(QWidget):
    """滚动长截图窗口

    特性：
    - 带边框的透明窗口，不拦截鼠标事件（滚动直接作用于后面的页面）
    - 内容感知抓帧：监视框内画面与上一张已采集帧不同就立刻采集拼接
    - 拼接管线在串行工作线程完成，主线程只抓屏
    - 底部有完成和取消按钮；页面静止数秒自动收尾
    """
    
    finished = Signal()  # 完成信号
    cancelled = Signal()  # 取消信号
    stitch_result_ready = Signal(object)  # 拼接工作线程 → 主线程的结果载荷

    # ── 内容感知抓帧参数 ──
    # 不再依赖滚轮事件触发：监视定时器持续对框内内容做降采样签名，
    # 与上一张已采集帧不同 → 立刻抓帧拼接（worker 积压 ≥2 时跳过，
    # 由背压控帧）。用户以任意速度滚动都能正确出帧。
    _WATCH_IDLE_MS = 250     # 内容与上帧一致时的轮询间隔（省 CPU）
    _WATCH_IDLE_SLOW_MS = 600    # 静止 1.5s 后：进一步降频
    _WATCH_IDLE_SLOWEST_MS = 1200  # 静止 4s 后：最低档（一有变化立即回 90ms）
    _WATCH_ACTIVE_MS = 90    # 检测到变化后的轮询间隔（快速出帧）
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
        self._last_stitch_height = 0  # 上一帧载荷报告的拼接结果高度

        # 保存目录（由外部设置）
        self.save_directory = None
        self.save_service = SaveService()

        # 截图方向: "vertical"(竖向) 或 "horizontal"(横向)
        self.scroll_direction = "vertical"

        # 滚动方向: None=未判定, "down"/"up"。由工作线程按新帧在画布里的
        # 落位自动判定（不来自滚轮），纯由画面内容决定，仅用于日志与载荷。
        self.scroll_locked_direction = None

        # 实时拼接相关
        self.stitched_result = None  # 当前拼接的结果图
        self.preview_warning_active = False

        # 去重相关（worker 内部亦有一份，手动抓帧路径仍会用）
        self.duplicate_threshold = 0.95

        # 截图引擎按设置走 HDR（auto 且屏开 HDR）；本会话抓帧失败则退回 GDI
        self._use_hdr = uses_hdr_engine()

        # 自动收尾
        self._auto_finish_scheduled = False
        # 会话收尾重入守卫：完成/钉图执行期间，挂着的自动收尾定时器
        # （经 flush 的 processEvents 落地）不得再次触发收尾——二次
        # _on_finish 会重复保存、重复复制到剪贴板
        self._finishing = False

        # ── 内容感知监视状态 ──
        self._last_captured_sig = None   # 上一张已采集帧的内容签名
        self._idle_started_at = None     # 内容静止开始时刻（monotonic）
        self._preview_stale = False      # 预览因节流滞后，空闲时需补刷新
        self._cancel_requested = False   # 取消按钮：中止进行中的收尾流程

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

        # 内容监视定时器：初始采集完成后再启动（见 _capture_initial_screenshot）。
        # 抓帧与签名在 _WatchGrabber 线程完成，定时器只在 GUI 线程应用最新样本
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(self._WATCH_IDLE_MS)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_grabber = _WatchGrabber(
            self.capture_rect, self._SIGNATURE_SIZE, self._WATCH_IDLE_MS, parent=self
        )

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

        self._register_shortcut_handler()

        # 验证窗口位置是否正确
        self._verify_window_position()

        # 浮动 UI 及其 DWM 投影整会话排除在屏幕捕获之外（必须在第一帧
        # 抓取之前生效，否则投影会被烘进拼接结果，见该方法注释）
        self._set_floating_ui_capture_excluded(True)

        # 延迟一次事件循环后强制将所有浮动子窗口提到 TOPMOST 栈顶，
        # 避免初始显示时被系统任务栏（同为 HWND_TOPMOST）压在下方。
        QTimer.singleShot(0, self, self._raise_all_topmost)

        # 使用QTimer延迟执行，确保窗口完全显示后再截图
        QTimer.singleShot(100, self, self._capture_initial_screenshot)

    def _register_shortcut_handler(self):
        """注册长截图的键盘入口（ESC 取消 / Ctrl+C 完成并复制）。

        register 按 identity 去重，窗口被重新 show 时不会重复入列；
        handler 惰性创建，与 _cleanup 的注销/断引用正好成对。
        """
        if getattr(self, "_shortcut_handler", None) is None:
            self._shortcut_handler = ScrollCaptureShortcutHandler(self)
        ShortcutManager.instance().register(self._shortcut_handler)

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
            if not self._watch_grabber.isRunning():
                self._watch_grabber.start()
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

    def _set_floating_ui_capture_excluded(self, exclude: bool):
        """把浮动 UI（工具栏 / 预览面板）从屏幕捕获中排除或恢复。

        必须整会话保持，不能只在「矩形与截图区域相交」时才排除：这两个
        窗口被刻意放在截图区域**外侧**（间距仅 14px），矩形永不相交，但
        它们的 DWM 投影会向内扩散百余像素，正好落在截图区域右/左边缘。
        投影是屏幕固定的、页面却在滚动——每一帧都会把同一片投影烘到不同
        的内容行上，拼接缝处就出现一道随缝移动的阴影。实测给面板加
        WDA_EXCLUDEFROMCAPTURE 后，投影对截图区域的影响降为 0。
        """
        from core.platform_utils import set_window_exclude_from_capture
        panel = getattr(self, 'preview_panel', None)
        if panel is not None:
            panel.set_capture_excluded(exclude)
        toolbar = getattr(self, 'toolbar', None)
        if toolbar is not None:
            try:
                set_window_exclude_from_capture(int(toolbar.winId()), exclude)
            except Exception as e:
                _log_stitch(T("[WARN] 排除浮动工具栏截图失败: {e}", e=e))

    def _grab_region(self):
        """截取截图区域，返回 (QPixmap, 签名)，失败时返回 (None, None)。"""
        image = self._grab_capture_rect()
        if image is None:
            return None, None
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return None, None
        return pixmap, self._signature(pixmap)

    def _grab_capture_rect(self) -> Optional[QImage]:
        """截取 capture_rect（物理像素）；引擎要求 HDR 时走 HDR，失败后本会话退回 GDI grabWindow。"""
        if self._use_hdr:
            try:
                return grab_region_hdr(self.capture_rect)
            except Exception as e:
                self._use_hdr = False
                _log_stitch(T("HDR 抓帧失败，本会话改用 GDI: {error}", error=e), force=True)

        # 获取所在屏幕并转换为相对坐标
        app = QGuiApplication.instance()
        capture_center_x = self.capture_rect.x() + self.capture_rect.width() // 2
        capture_center_y = self.capture_rect.y() + self.capture_rect.height() // 2
        center_point = QPoint(capture_center_x, capture_center_y)

        screen = app.screenAt(center_point)
        if screen is None:
            _log_stitch(T("[WARN] 截图区域不在任何显示器范围内，使用主显示器"), force=True)
            screen = app.primaryScreen()

        screen_geometry = screen.geometry()

        # 跨屏时把绝对坐标转成相对目标屏幕的坐标
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
            return None
        return pixmap.toImage()

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
        grabber = getattr(self, "_watch_grabber", None)
        if grabber is not None:
            grabber.set_interval(ms)

    def _idle_watch_interval(self, idle_for: float):
        """静止后的轮询间隔自适应拉长：越闲越省 CPU（监视线程的抓帧是大头），
        一有变化立即回 _WATCH_ACTIVE_MS。"""
        if idle_for >= 4.0:
            return self._WATCH_IDLE_SLOWEST_MS
        if idle_for >= 1.5:
            return self._WATCH_IDLE_SLOW_MS
        return self._WATCH_IDLE_MS

    def _watch_tick(self):
        """内容感知监视的一次采样应用：样本由 _WatchGrabber 在后台采集。

        与上一张已采集帧一致 → 内容没动（阅读停顿/已到底），计入静止
        时长作为自动收尾依据；不一致 → 立即抓帧提交拼接。快速滚动时
        每 90ms 一帧，相邻帧间隔小、重叠必然存在，成功率由此保证。
        worker 积压时跳过采集（背压），内存有界。
        """
        if not self.isVisible():
            return

        grabber = getattr(self, "_watch_grabber", None)
        sample = grabber.latest() if grabber is not None else None
        if sample is None:
            return
        image, sig = sample
        now = time.monotonic()

        if self._sig_similar(sig, self._last_captured_sig):
            # 内容与上一张已采集帧一致：静止计时
            idle_for = now - self._idle_started_at if self._idle_started_at is not None else 0.0
            self._set_watch_interval(self._idle_watch_interval(idle_for))
            if self._idle_started_at is None:
                self._idle_started_at = now
            # 预览因节流而滞后时，趁空闲补一次刷新
            if self._preview_stale and not self._stitch_worker.is_busy():
                self._preview_stale = False
                self._stitch_worker.submit_preview_refresh()
            self._maybe_auto_finish(now - self._idle_started_at)
            return

        self._idle_started_at = None
        self._set_watch_interval(self._WATCH_ACTIVE_MS)

        # 背压：worker 积压过多时跳过本帧（有界内存；极快滚动的代价，
        # 待 worker 追上后下一拍会继续采集当前内容）
        if self._stitch_worker.pending() >= 2:
            return

        self._last_captured_sig = sig
        # 测试替身可能直接给 QPixmap；正常路径这里是 QImage
        pixmap = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
        self._do_capture(pixmap)

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

    def _do_capture(self, pixmap=None):
        """立即抓取一帧并提交给拼接工作线程（手动抓帧/监视器稳定点共用）。

        Args:
            pixmap: 监视器在稳定判定时已经抓到的帧。传入则直接使用——
                稳定点和提交之间不再有二次抓帧的间隙，避免基线签名与
                实际提交的帧错位；None 则现抓一帧（手动路径）。

        像素管线见 _StitchWorker。提交后更新内容签名基线，监视器据此
        判断后续变化。
        """
        # 截图前的 UI 排除已在 showEvent 中整会话设置（含窗口投影）
        try:
            if pixmap is None:
                pixmap, sig = self._grab_region()
                if pixmap is None:
                    _log_stitch(T("[ERROR] 截图失败"), force=True)
                    return
            else:
                sig = self._signature(pixmap)

            self._last_captured_sig = sig
            self._idle_started_at = None

            # toImage() 返回独立缓冲的 QImage，移交工作线程后主线程不再触碰。
            self._stitch_worker.submit(pixmap.toImage(), self.scroll_direction)

        except Exception as e:
            _log_stitch(T("[ERROR] 截图时出错: {e}", e=e), force=True)
            import traceback
            traceback.print_exc()

    def _flush_pending_stitch(self, budget_s: float = 20.0):
        """收尾握手：排空积压帧 → 解码最终成图。

        逐帧等 worker 追平（期间泵事件循环：UI 保持响应、计数与预览
        实时推进），随后提交 finalize 任务解码最终 PNG 成图——全图解码
        只此一次。取消请求会中止排水（结果作废，不保存）。
        """
        if hasattr(self, "_watch_timer"):
            self._watch_timer.stop()
        worker = getattr(self, "_stitch_worker", None)
        if worker is None:
            return
        _log_stitch(T("等待剩余帧拼接完成…"), force=False)
        deadline = time.monotonic() + budget_s
        while worker.is_busy() and time.monotonic() < deadline:
            if self._cancel_requested:
                return
            QApplication.processEvents()
            time.sleep(0.03)
        if self._cancel_requested:
            return
        worker.submit_finalize()
        deadline = time.monotonic() + 10.0
        while worker.is_busy() and time.monotonic() < deadline:
            if self._cancel_requested:
                return
            QApplication.processEvents()
            time.sleep(0.03)
        # finalize 的回包不在队列里（经属性交接），泵一轮事件兜底
        QApplication.processEvents()

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
        if payload.get("preview_only"):
            # 仅预览刷新：不更新计数与增益
            if payload["preview_qimage"] is not None and getattr(self, 'preview_panel', None):
                self.preview_panel.update_preview(
                    payload["preview_qimage"],
                    self.scroll_direction,
                    payload["screenshot_count"],
                )
                self._position_preview_panel()
            return

        count = payload["screenshot_count"]

        # 滚动方向镜像：worker 按落位判定的结果是权威值
        self.scroll_locked_direction = payload["locked_direction"]

        if payload["ok"]:
            gain = max(0, payload["height"] - self._last_stitch_height)
            self._last_stitch_height = payload["height"]
            self.scroll_distances.append(gain)
            while len(self.screenshots) < count:
                self.screenshots.append(None)

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
        elif payload["preview_qimage"] is None:
            # 本帧预览被节流跳过：空闲时补一次刷新
            self._preview_stale = True

        if payload["auto_finish"] and not self._auto_finish_scheduled:
            self._auto_finish_scheduled = True
            _log_stitch(T("检测到页面已到边缘（连续 2 帧无变化），自动完成拼接"), force=True)
            # 先排自动收尾，再动提示 UI：收尾是功能，提示只是装饰——
            # 提示 UI 再出异常也不能拖住 900ms 后的自动完成
            QTimer.singleShot(900, self, self._auto_finish_if_alive)
            if hasattr(self, 'preview_panel') and self.preview_panel:
                # 必须走 _show_preview_warning 置位 preview_warning_active，
                # 否则后续 _clear_preview_warning 早退、感叹号清不掉
                self._show_preview_warning(
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
        if getattr(self, "_finishing", False):
            return
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
        if getattr(self, "_finishing", False):
            return
        self._finishing = True
        # 在途帧先落地并解码最终成图：worker 是异步的，快速点完成时
        # 最后一帧可能还没拼完
        self._flush_pending_stitch()
        if self._cancel_requested:
            self._abandon_as_cancelled()
            return
        worker = getattr(self, "_stitch_worker", None)
        self.stitched_result = worker.get_final_image() if worker else None
        _log_stitch(T("[OK] 完成长截图，共 {count} 张图片", count=len(self.screenshots)), force=True)
        
        # 横向模式：将拼接结果逆时针旋转90度还原
        # 只有在有2张及以上图片（发生了拼接）时才旋转
        # 如果只有1张图片，不需要旋转（第1张图片没有被旋转）
        # 上滚/左滚无需翻转还原：画布自始至终是自然朝向。
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
        """将拼接结果复制到剪贴板（CF_DIBV5 + PNG，与截图流程同一条写入路径）"""
        if self.stitched_result is None:
            return

        try:
            # 转换为 QImage
            image = self.stitched_result.convert("RGBA")
            width, height = image.size
            data = image.tobytes("raw", "RGBA")

            # 创建 QImage (引用 data)；copy() 脱离 data 的生存期
            qimage = QImage(
                data, width, height, width * 4, QImage.Format.Format_RGBA8888
            ).copy()
        except Exception as e:
            _log_stitch(T("[ERROR] 拼接结果转 QImage 失败: {e}", e=e))
            return

        try:
            from core.clipboard_utils import copy_image_to_clipboard
            copy_image_to_clipboard(qimage)
            _log_stitch(T("长截图已复制到剪贴板"))
        except Exception as e:
            # copy_image_to_clipboard 内部已经带 Qt 回退；这里再兜一层，
            # 保证「Ctrl+C 松手就有结果」的约定不因一次写入失败而食言
            _log_stitch(T("[WARN] 剪贴板 Win32 写入失败，改用 Qt: {e}", e=e))
            try:
                QApplication.clipboard().setImage(qimage)
                _log_stitch(T("长截图已复制到剪贴板 (Qt)"))
            except Exception as e2:
                _log_stitch(T("[ERROR] 复制到剪贴板失败: {e}", e=e2))
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
        if getattr(self, "_finishing", False):
            return
        self._finishing = True
        # 同 _on_finish：在途帧先落地并解码最终成图
        self._flush_pending_stitch()
        if self._cancel_requested:
            self._abandon_as_cancelled()
            return
        worker = getattr(self, "_stitch_worker", None)
        self.stitched_result = worker.get_final_image() if worker else None
        _log_stitch(T("钉图长截图结果..."))

        # 检查 config_manager
        if self.config_manager is None:
            _log_stitch(T("[ERROR] config_manager 未设置，无法创建钉图"))
            self._resume_watching_after_failed_pin()
            return

        # 获取拼接结果
        result_image = self.stitched_result

        if result_image is None:
            _log_stitch(T("[WARN] 没有拼接结果，无法钉图"))
            self._resume_watching_after_failed_pin()
            return
        
        # 横向模式：旋转结果（画布是自然朝向，上滚/左滚无需翻转还原）
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
            # 钉图没做成：不把 _finishing 永久挂在 True 上，否则三个按钮
            # 全部静默失效，窗口看着还在、其实已经死了
            self._resume_watching_after_failed_pin()

    def _resume_watching_after_failed_pin(self):
        """钉图中止：把窗口恢复成可以继续截图的状态。"""
        self._finishing = False
        self._auto_finish_scheduled = False
        if hasattr(self, "_watch_timer"):
            self._watch_timer.start(self._WATCH_IDLE_MS)
        grabber = getattr(self, "_watch_grabber", None)
        if grabber is not None and not grabber.isRunning():
            grabber.start()

    def _abandon_as_cancelled(self):
        """按取消收场：丢弃已拼内容、停 worker、发 cancelled 并关窗。

        _on_cancel（正常点取消）与"收尾途中点取消"两条路径共用。
        """
        _log_stitch(T("[ERROR] 取消长截图"), force=True)
        self.screenshots.clear()
        self._cleanup()
        self.cancelled.emit()
        self.close()

    def _on_cancel(self):
        """取消按钮点击"""
        if getattr(self, "_finishing", False):
            # 收尾正阻塞在 _flush_pending_stitch 的事件泵里（最长 20+10 秒），
            # 期间事件照常分发、取消按钮仍然可点。旧逻辑在这里直接 return——
            # 取消被静默吞掉，_cancel_requested 永远置不上，收尾随后照常
            # 保存 + 复制 + 关窗。这里只记请求，由进行中的收尾观察到后
            # 走 _abandon_as_cancelled（_on_finish / _on_pin 已接好）。
            self._cancel_requested = True
            return
        self._finishing = True
        self._cancel_requested = True
        self._abandon_as_cancelled()
    
    def _cleanup(self):
        """清理资源"""
        try:
            # 先注销快捷键：下面任何一步抛异常，都不能把 handler 留在
            # 管理器里指向一个已关闭的窗口。unregister 自身按 identity
            # 去重，_on_finish 与 closeEvent 各调一次 _cleanup 也安全。
            handler = getattr(self, "_shortcut_handler", None)
            if handler is not None:
                ShortcutManager.instance().unregister(handler)
                self._shortcut_handler = None

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

            # 停止监视抓帧线程：requestInterruption 后分片睡眠最多 50ms 就会退出，
            # wait 只是兜底
            grabber = getattr(self, '_watch_grabber', None)
            if grabber is not None:
                grabber.requestInterruption()
                grabber.wait(2000)
                self._watch_grabber = None

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
                
            # 会话结束：恢复浮动 UI 的截图排除，它们重新可被捕获
            self._set_floating_ui_capture_excluded(False)

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
 
