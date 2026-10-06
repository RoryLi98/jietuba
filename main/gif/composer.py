# -*- coding: utf-8 -*-
"""
GIF 合成器 + 进度对话框

技术路线：
  gifrecorder.FrameStore.export_gif()
  Rust 侧并行解码 → 缩放 → 光标叠加 → GIF 编码，进度通过回调报告。
  全部由 Rust gifrecorder 完成，无外部依赖。
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Qt, QEventLoop
from PySide6.QtWidgets import (QApplication, QFrame,
                              QProgressBar)
from ui.dialogs import show_warning_dialog

from ._widgets import PROGRESS_BAR_STYLE
from core.logger import log_info, log_error, log_exception, log_warning, T
from core.i18n import make_tr

_tr = make_tr("GifRecorder")

try:
    import gifrecorder
    _gifrecorder_available = True
except ImportError:
    gifrecorder = None
    _gifrecorder_available = False

# == 默认导出参数 ==
DEFAULT_GIF_WIDTH  = 0


# ── 合成线程 Worker ──────────────────────────────────

class _ComposeWorker(QObject):
    progress = Signal(int, int)      # (done, total)
    finished = Signal(bool, object)  # (ok, result: str|bytes|None)

    def __init__(self,
                 path: Optional[str],
                 store=None,
                 gif_width: int = DEFAULT_GIF_WIDTH,
                 frame_start: int = 0,
                 frame_end: int = 0,
                 cursor_sprites: Optional[dict] = None,
                 cursor_infos: Optional[list] = None,
                 speed_multiplier: float = 1.0):
        super().__init__()
        self._path = path
        self._store = store                     # gifrecorder.FrameStore 实例
        self._gif_width = gif_width
        self._frame_start = frame_start
        self._frame_end = frame_end
        self._cursor_sprites = cursor_sprites   # sprite 集合 dict
        self._cursor_infos = cursor_infos       # list of (x,y,press,scroll,burst_frame,burst_side)
        self._speed_multiplier = max(0.1, speed_multiplier)
        self._cancel = False

    def cancel(self):
        self._cancel = True
        if self._store is not None:
            self._store.cancel_export()

    def run(self):
        try:
            if not _gifrecorder_available or self._store is None:
                self.finished.emit(False, "gifrecorder_not_found")
                return
            result = self._compose()
            self.finished.emit(not self._cancel, result)
        except Exception as e:
            log_error(T("GIF 合成失败: {e}", e=e), "GIF")
            self.finished.emit(False, None)

    def _compose(self):
        if not callable(getattr(self._store, "export_gif_bytes", None)):
            # 旧版 gifrecorder 兜底：走临时文件
            need_bytes = self._path is None
            if need_bytes:
                tmp_fd, out_path = tempfile.mkstemp(suffix=".gif", prefix="jietuba_")
                os.close(tmp_fd)
            else:
                out_path = self._path

            ok = self._do_compose(out_path)

            if not ok or self._cancel:
                if need_bytes:
                    try:
                        os.unlink(out_path)
                    except Exception as e:
                        log_exception(e, T("GIF 取消后删除临时文件"))
                return None

            if need_bytes:
                try:
                    with open(out_path, "rb") as f:
                        return f.read()
                finally:
                    try:
                        os.unlink(out_path)
                    except Exception as e:
                        log_exception(e, T("GIF 读取后删除临时文件"))
            else:
                return out_path

        # gifrecorder ≥ 直出字节版：贴剪贴板不再经临时文件一写一读
        if self._path is None:
            if self._cancel:
                return None
            data = self._do_compose_bytes()
            return data if (data and not self._cancel) else None

        ok = self._do_compose(self._path)
        if not ok or self._cancel:
            return None
        return self._path

    def _do_compose(self, out_path: str) -> bool:
        """使用 gifrecorder.FrameStore.export_gif() 导出"""
        try:
            self._store.export_gif(path=out_path, **self._export_kwargs())
        except Exception as e:
            err_str = str(e)
            if "cancelled" in err_str:
                return False
            log_error(T("export_gif 失败: {e}", e=e), "GIF")
            return False

        file_size = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
        log_info(
            T("GIF 导出完成: {out_path} ({size_kb:.1f} KB)",
              out_path=out_path, size_kb=file_size / 1024),
            "GIF",
        )
        return True

    def _do_compose_bytes(self):
        """使用 export_gif_bytes() 直出字节；失败/取消返回 None。"""
        try:
            data = self._store.export_gif_bytes(**self._export_kwargs())
        except Exception as e:
            err_str = str(e)
            if "cancelled" in err_str:
                return None
            log_error(T("export_gif_bytes 失败: {e}", e=e), "GIF")
            return None

        log_info(
            T("GIF 导出完成: 内存 {size_kb:.1f} KB", size_kb=len(data) / 1024),
            "GIF",
        )
        return data

    def _export_kwargs(self) -> dict:
        """计算输出尺寸并组装 export_gif / export_gif_bytes 的公共参数"""
        gif_width = self._gif_width
        gif_height = 0
        if gif_width > 0 and self._store.width > 0:
            ratio = gif_width / self._store.width
            gif_height = max(2, int(self._store.height * ratio))
            gif_height = gif_height if gif_height % 2 == 0 else gif_height - 1
        else:
            gif_width = self._store.width
            gif_height = self._store.height

        log_info(
            T("使用 gifrecorder 导出 GIF: {gif_width}x{gif_height}",
              gif_width=gif_width, gif_height=gif_height),
            "GIF",
        )

        def _progress(done, total):
            self.progress.emit(done, total)
            return not self._cancel   # 返回 False 取消

        export_kwargs = dict(
            width=gif_width,
            height=gif_height,
            repeat=0,
            frame_start=self._frame_start,
            frame_end=self._frame_end,
            progress_callback=_progress,
            speed=self._speed_multiplier,
        )
        if self._cursor_sprites and self._cursor_infos:
            export_kwargs["cursor_sprites"] = self._cursor_sprites
            export_kwargs["cursor_infos"] = self._cursor_infos
        return export_kwargs


# ── 进度浮层（无边框纯进度条） ──────────────────────────

class ComposerProgressDialog(QFrame):
    # 进度静默看门狗阈值：分批导出每批都会报一次进度，60s 无进度基本可判卡死
    _PROGRESS_SILENCE_TIMEOUT_MS = 60_000
    """
    无边框纯进度条浮层，样式与录制结束等待进度条保持一致。
    居中于 parent 窗口；合成完成/取消后 loop.quit() 解除阻塞。
    """

    def __init__(self, parent=None):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(260, 14)

        self._result = None
        self._ok = False
        self._loop = QEventLoop()

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        self._bar.setGeometry(0, 0, 260, 14)
        self._bar.setStyleSheet(PROGRESS_BAR_STYLE)

        self._thread: Optional[QThread] = None
        self._worker: Optional[_ComposeWorker] = None
        # 进度静默看门狗：合成卡死（原生侧既不报错也不取消）时 _loop.exec()
        # 会永久挂起 GUI 线程。超过阈值没有任何进度就取消导出并强制退出。
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_progress_silence)

    def _center_on(self, parent, center_pos=None):
        if center_pos is not None:
            self.move(center_pos.x() - self.width() // 2,
                      center_pos.y() - self.height() // 2)
            return
        if parent is not None:
            try:
                c = parent.geometry().center()
                self.move(c.x() - self.width() // 2, c.y() - self.height() // 2)
                return
            except Exception as e:
                log_exception(e, T("居中进度对话框"))
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.center().x() - self.width() // 2,
                  screen.center().y() - self.height() // 2)

    def start(self,
              path: Optional[str] = None,
              store=None,
              gif_width: int = DEFAULT_GIF_WIDTH,
              frame_start: int = 0,
              frame_end: int = 0,
              cursor_sprites: Optional[dict] = None,
              cursor_infos: Optional[list] = None,
              speed_multiplier: float = 1.0):
        self._thread = QThread()
        self._worker = _ComposeWorker(
            path,
            store=store,
            gif_width=gif_width,
            frame_start=frame_start,
            frame_end=frame_end,
            cursor_sprites=cursor_sprites,
            cursor_infos=cursor_infos,
            speed_multiplier=speed_multiplier,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()
        self._watchdog.start(self._PROGRESS_SILENCE_TIMEOUT_MS)

    def _on_progress(self, done: int, total: int):
        self._watchdog.start(self._PROGRESS_SILENCE_TIMEOUT_MS)
        if total <= 0:
            if self._bar.maximum() != 0:
                self._bar.setRange(0, 0)
        else:
            if self._bar.maximum() == 0:
                self._bar.setRange(0, 100)
            self._bar.setValue(int(done / total * 100))

    def _on_finished(self, ok: bool, result):
        self._watchdog.stop()
        self._ok = ok
        self._result = result
        # 先释放 worker 对 store 的引用，避免多余的 Arc 引用延迟释放
        if self._worker:
            self._worker._store = None
            self._worker._cursor_sprites = None
            self._worker._cursor_infos = None
        if self._thread:
            self._thread.quit()
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        self.hide()
        self._loop.quit()

        if not ok and result == "gifrecorder_not_found":
            show_warning_dialog(
                None,
                _tr("gifrecorder unavailable"),
                _tr("The gifrecorder module was not found, so the GIF cannot be exported.\n\n"
                    "Make sure gifrecorder is installed correctly."),
            )

    def _on_progress_silence(self):
        """进度静默超时：先取消导出（正常取消会经 finished 走完整清理），
        同时强制退出事件循环兜底——原生侧真卡死时 finished 不会再来了。

        强制退出路径不动 self._thread：线程还卡在原生调用里，deleteLater/
        wait 都不安全；线程对象泄漏一次属于可接受的病理情形，日志会说明。
        """
        log_warning(
            T("GIF 导出 {timeout_ms}ms 无任何进度，疑似卡死：已请求取消并关闭进度框",
              timeout_ms=self._PROGRESS_SILENCE_TIMEOUT_MS),
            "GIF",
        )
        if self._worker is not None:
            try:
                self._worker.cancel()
            except Exception as e:
                log_exception(e, T("看门狗取消 GIF 导出"))
        self._loop.quit()

    @staticmethod
    def run_compose(path: Optional[str] = None,
                    store=None,
                    gif_width: int = DEFAULT_GIF_WIDTH,
                    parent=None,
                    center_pos=None,
                    frame_start: int = 0,
                    frame_end: int = 0,
                    cursor_sprites: Optional[dict] = None,
                    cursor_infos: Optional[list] = None,
                    speed_multiplier: float = 1.0):
        """
        显示无边框进度条并阻塞直到合成完成/失败。
        返回 str(路径) / bytes / None(取消或失败)。
        center_pos: QPoint，直接指定进度条居中坐标（优先于 parent）。
        frame_start/frame_end: 修剪范围（含），0 表示不限制（导出全部帧）。
        speed_multiplier: 导出速度倍率（1.0=原速）。
        """
        dlg = ComposerProgressDialog(parent)
        dlg._center_on(parent, center_pos=center_pos)
        dlg.start(path,
                  store=store,
                  gif_width=gif_width,
                  frame_start=frame_start,
                  frame_end=frame_end,
                  cursor_sprites=cursor_sprites,
                  cursor_infos=cursor_infos,
                  speed_multiplier=speed_multiplier)
        dlg.show()
        QApplication.processEvents()
        dlg._loop.exec()
        dlg.deleteLater()

        return dlg._result if dlg._ok else None
 