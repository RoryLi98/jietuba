"""
全局崩溃捕获模块 - 统一异常捕获

覆盖四层异常来源：
1. sys.excepthook          — 主线程未捕获的 Python 异常
2. threading.excepthook    — 子线程未捕获的 Python 异常（Python 3.8+）
3. sys.unraisablehook      — 析构函数 / __del__ / 回调中被静默吞掉的异常
4. faulthandler            — C 层面的 segfault / abort（ctypes、Rust 库等崩溃）
5. Windows 首次机会异常探针 — faulthandler 覆盖不到的致命异常码（qFatal/abort 的
                             0x40000015 等），补记异常码、故障地址与所属模块

"""

import os
import sys
import threading
import traceback
import functools
from datetime import datetime


# ============================================================================
# 日志目录（与 logger.py 保持一致）
# ============================================================================
from core.constants import get_log_dir

_LOG_DIR = get_log_dir()
_CRASH_FILE = "crash.log"

# faulthandler 输出文件句柄（模块级持有，防止被 GC）
_faulthandler_fp = None


def _ensure_log_dir():
    """确保日志目录存在"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _write_crash(tag: str, msg: str):
    """
    将崩溃信息同时输出到：
      1. 终端 stdout（开发时可见）
      2. crash.log 独立文件（即使 Logger 没初始化也能留痕）
      3. Logger 系统日志（如果已初始化）
    """
    # --- 终端 ---
    text = (
        f"\n{'='*60}\n"
        f"❌ {tag}:\n"
        f"{msg}"
        f"{'='*60}\n"
    )
    try:
        sys.__stderr__.write(text)      # 用 __stderr__ 绕过可能被重定向的 stderr
        sys.__stderr__.flush()
    except Exception:
        pass

    # --- crash.log（独立于 Logger，最小依赖） ---
    _ensure_log_dir()
    try:
        crash_path = _LOG_DIR / _CRASH_FILE
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {tag}\n{msg}{'='*60}\n\n")
    except Exception:
        pass

    # --- Logger 系统日志（如果可用） ---
    try:
        from core.logger import get_logger
        logger = get_logger()
        if logger and logger._ready:
            logger.error(f"{tag}:\n{msg}", "CRASH")
    except Exception:
        pass


# ============================================================================
# 四层钩子
# ============================================================================

def _excepthook(exc_type, exc_value, exc_tb):
    """① 主线程未捕获异常"""
    error_msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _write_crash("未处理的异常（主线程）", error_msg)


def _threading_excepthook(args):
    """② 子线程未捕获异常（Python 3.8+）
    
    threading.Thread 里未捕获的异常默认只打印到 stderr 然后线程静默退出，
    主线程完全不知道。
    """
    error_msg = ''.join(traceback.format_exception(
        args.exc_type, args.exc_value, args.exc_traceback
    ))
    thread_name = args.thread.name if args.thread else "Unknown"
    _write_crash(f"未处理的异常（子线程: {thread_name}）", error_msg)


def _unraisablehook(unraisable):
    """③ 不可抛出异常（__del__ / 回调 / finalizer 中的异常）
    
    PyQt 的信号回调、对象析构中的异常会被 Python 静默吞掉。
    """
    exc = unraisable.exc_value
    if exc is None:
        return
    error_msg = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    obj_info = f" (对象: {unraisable.object!r})" if unraisable.object else ""
    _write_crash(f"不可抛出异常{obj_info}", error_msg)


def _install_faulthandler():
    """④ C 层面 segfault / abort 堆栈追踪
    
    ctypes 调用出错、Rust 库崩溃等导致的进程闪退，
    faulthandler 会在进程死亡前把 C 堆栈写入文件。
    这是最后的防线。
    """
    global _faulthandler_fp
    import faulthandler

    _ensure_log_dir()
    try:
        _faulthandler_fp = open(
            _LOG_DIR / "faulthandler.log", "a", encoding="utf-8"
        )
        # 写入分隔线，方便区分不同次崩溃
        _faulthandler_fp.write(f"\n--- session {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        _faulthandler_fp.flush()
        faulthandler.enable(file=_faulthandler_fp)
    except Exception:
        # 至少输出到 stderr
        faulthandler.enable()


# ============================================================================
# Windows 首次机会异常探针
# ============================================================================
#
# faulthandler 只认少数几个异常码：access violation 能拿到 Python 堆栈，但
# 0x40000015（STATUS_FATAL_APP_EXIT，qFatal/abort 这类）不在其列，进程静默
# 消失时日志里只有"没有正常退出"一行，连死在哪个模块都无从判断。
# 这里注册一个向量化异常处理函数，把这类异常的异常码、故障地址与所属模块
# 补写进 crash.log——只记录、不拦截，处理完照常交回系统。
# 实测：继续处理函数（AddVectoredContinueHandler）在无人接管的访问违例里
# 不会被调用，首次机会处理函数（AddVectoredExceptionHandler）才会，故用后者。
# 代价是极少数被 SEH 接走的同类异常也会记一笔，条目按次数封顶，不致命。

_FATAL_EXCEPTIONS = {
    0xC0000005: "ACCESS_VIOLATION",
    0x40000015: "FATAL_APP_EXIT(abort/qFatal)",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000409: "FAIL_FAST",
    0xC000001D: "ILLEGAL_INSTRUCTION",
}
_PROBE_MAX_ENTRIES = 8
_probe_fd = None      # 预打开的 crash.log 句柄（异常上下文里不能再 open）
_probe_handler = None  # 模块级持有，防止回调对象被 GC


def _install_exception_probe():
    """注册 Windows 首次机会异常探针，把致命异常码/地址/所属模块写进 crash.log。"""
    global _probe_fd, _probe_handler
    if _probe_handler is not None or not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _EXCEPTION_RECORD(ctypes.Structure):
            _fields_ = [
                ("ExceptionCode", wintypes.DWORD),
                ("ExceptionFlags", wintypes.DWORD),
                ("ExceptionRecord", ctypes.c_void_p),
                ("ExceptionAddress", ctypes.c_void_p),
                ("NumberParameters", wintypes.DWORD),
                ("ExceptionInformation", ctypes.c_size_t * 15),
            ]

        class _EXCEPTION_POINTERS(ctypes.Structure):
            _fields_ = [
                ("ExceptionRecord", ctypes.POINTER(_EXCEPTION_RECORD)),
                ("ContextRecord", ctypes.c_void_p),
            ]

        def _module_of(addr):
            try:
                k32 = ctypes.windll.kernel32
                hmod = wintypes.HMODULE()
                # GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | ..._UNCHANGED_REFCOUNT
                if not k32.GetModuleHandleExA(0x6, ctypes.c_void_p(addr), ctypes.byref(hmod)):
                    return "<unknown>"
                buf = ctypes.create_unicode_buffer(520)
                return buf.value if k32.GetModuleFileNameW(hmod, buf, 520) else "<unknown>"
            except Exception:
                return "<unknown>"

        _logged = 0

        def _handler(pointers):
            nonlocal _logged
            try:
                if _logged >= _PROBE_MAX_ENTRIES or not pointers:
                    return 0
                rec = ctypes.cast(pointers, ctypes.POINTER(_EXCEPTION_POINTERS)).contents.ExceptionRecord
                if not rec:
                    return 0
                code = int(rec.contents.ExceptionCode) & 0xFFFFFFFF
                name = _FATAL_EXCEPTIONS.get(code)
                if name is None:
                    return 0
                _logged += 1
                addr = int(rec.contents.ExceptionAddress or 0)
                line = (
                    f"\n[C层异常探针 {datetime.now():%Y-%m-%d %H:%M:%S}] "
                    f"code=0x{code:08X} ({name}) addr=0x{addr:X} module={_module_of(addr)}\n"
                )
                if _probe_fd is not None:
                    os.write(_probe_fd, line.encode("utf-8", "replace"))
                try:
                    sys.__stderr__.write(line)
                    sys.__stderr__.flush()
                except Exception:
                    pass
            except Exception:
                pass
            return 0  # EXCEPTION_CONTINUE_SEARCH：只记录，不拦截

        _ensure_log_dir()
        _probe_fd = os.open(os.path.join(_LOG_DIR, _CRASH_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        _probe_handler = handler_type(_handler)
        # 第一个参数 1 = 排到链首；注册失败（返回 NULL）就放弃探针，不留半套状态
        if not ctypes.windll.kernel32.AddVectoredExceptionHandler(1, _probe_handler):
            _probe_handler = None
    except Exception:
        _probe_handler = None


# ============================================================================
# 公开 API
# ============================================================================

def install_crash_hooks():
    """
    一次性安装所有崩溃捕获钩子。
    
    应在程序最早期调用（main_app.py 顶部，任何 Qt 导入之前）。
    重复调用是安全的（幂等）。
    """
    sys.excepthook = _excepthook

    # threading.excepthook 需要 Python 3.8+
    if hasattr(threading, 'excepthook'):
        threading.excepthook = _threading_excepthook

    sys.unraisablehook = _unraisablehook

    _install_faulthandler()
    _install_exception_probe()


# ============================================================================
# Qt 事件安全装饰器
# ============================================================================

def safe_event(func):
    """
    装饰 QWidget 的事件处理函数（xxxEvent / eventFilter），两层保护：
    
    1. 根源防护：如果对象有 _is_closed 标记且为 True，直接短路返回，
       彻底阻止事件打进正在/已经 cleanup 的对象。
    2. 兜底捕获：即使没有 _is_closed 或其他原因异常，也不会被 Qt C++ 静默吞掉。
    
    注意：eventFilter 必须返回 bool，装饰器在短路/异常时返回 False。
    
    用法：
        class PinWindow(QWidget):
            @safe_event
            def wheelEvent(self, event):
                ...
            @safe_event
            def eventFilter(self, obj, event):
                ...
                return super().eventFilter(obj, event)
    """
    # eventFilter 要求返回 bool，短路/异常时必须返回 False 而不是 None
    _is_filter = (func.__name__ == 'eventFilter')

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        # 根源防护：对象正在关闭，不再处理任何事件
        if getattr(self, '_is_closed', False):
            return False if _is_filter else None
        try:
            return func(self, *args, **kwargs)
        except Exception:
            cls_name = type(self).__name__
            error_msg = traceback.format_exc()
            _write_crash(
                f"Qt事件异常 {cls_name}.{func.__name__}",
                error_msg,
            )
            return False if _is_filter else None
    return wrapper
 