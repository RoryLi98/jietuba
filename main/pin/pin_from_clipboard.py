# -*- coding: utf-8 -*-
"""
从剪贴板最新内容创建钉图

- 截图外按全局钉图热键时调用
- 图片：直接钉
- 文字：渲染成图片再钉
"""

from typing import Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QGuiApplication, QImage, QPainter, QColor
from PySide6.QtWidgets import QApplication

from core.logger import T, log_debug, log_info, log_warning, log_error


def render_text_to_image(
    text: str,
    font_family: str = "",
    font_size: int = 16,
    max_width: int = 640,
    padding: int = 20,
    bg_color: str = "#FFFFFF",
    text_color: str = "#1A1A1A",
) -> QImage:
    """把文字渲染成 QImage，用于钉图。

    自动换行，尺寸按内容自适应。
    """
    text = (text or "").rstrip("\n")
    if not text.strip():
        text = "(empty)"

    font = QFont(font_family) if font_family else QFont()
    font.setPixelSize(font_size)

    # 先用一个临时 QImage 测量高度
    tmp = QImage(8, 8, QImage.Format.Format_ARGB32)
    tmp_p = QPainter(tmp)
    tmp_p.setFont(font)
    metrics = tmp_p.fontMetrics()
    # boundingRect with WordWrap gives real height
    from PySide6.QtCore import QRect
    bounds = metrics.boundingRect(
        QRect(0, 0, max_width, 100000),
        int(Qt.TextFlag.TextWordWrap),
        text,
    )
    tmp_p.end()

    w = max(120, bounds.width() + padding * 2)
    h = max(48, bounds.height() + padding * 2)

    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(QColor(bg_color))

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(font)
        painter.setPen(QColor(text_color))
        painter.drawText(
            padding, padding, max_width, h - padding * 2,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap),
            text,
        )
    finally:
        painter.end()

    return image


def _screen_center_position(image: QImage) -> QPoint:
    """以后续逻辑为准：在光标所在屏幕中心放置钉图。"""
    screen = None
    try:
        pos = QApplication.instance() and __import__(
            "PySide6.QtGui", fromlist=["QCursor"]).QCursor.pos()
        if pos is not None:
            screen = QGuiApplication.screenAt(pos)
    except Exception:
        screen = None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QPoint(100, 100)

    geo = screen.availableGeometry()
    x = geo.x() + max(0, (geo.width() - image.width()) // 2)
    y = geo.y() + max(0, (geo.height() - image.height()) // 2)
    return QPoint(x, y)


def get_latest_clipboard_qimage() -> Optional[QImage]:
    """获取剪贴板最新内容的 QImage。

    优先级：
    1. 剪贴板历史 DB 最新一条（image 直接解码，text 渲染成图）
    2. 系统剪贴板当前 QMimeData（image / text）
    """
    # --- 1. 历史 DB ---
    try:
        from clipboard import ClipboardManager
        manager = ClipboardManager()
        if manager.is_available:
            history = manager.get_history(offset=0, limit=5)
            for item in history:
                if item is None:
                    continue
                ctype = getattr(item, "content_type", "")
                if ctype == "image" and getattr(item, "image_id", None):
                    try:
                        data = manager.get_image_data(item.image_id)
                        if data:
                            img = QImage.fromData(data)
                            del data
                            if img is not None and not img.isNull():
                                log_debug(T("全局钉图：使用历史图片"), "PinFromClipboard")
                                return img
                    except Exception:
                        continue
                elif ctype == "text" and (getattr(item, "content", "") or "").strip():
                    log_debug(T("全局钉图：使用历史文字转图片"), "PinFromClipboard")
                    return render_text_to_image(item.content)
                # file / html 等跳过，继续找下一条可钉的
    except Exception as e:
        log_warning(T("读取剪贴板历史失败，回退到系统剪贴板: {e}", e=e), "PinFromClipboard")

    # --- 2. 系统剪贴板 ---
    try:
        cb = QApplication.clipboard()
        mime = cb.mimeData() if cb else None
        if mime is not None:
            if mime.hasImage():
                img = cb.image()
                if isinstance(img, QImage) and not img.isNull():
                    log_debug(T("全局钉图：使用系统剪贴板图片"), "PinFromClipboard")
                    return img
            if mime.hasText():
                txt = (mime.text() or "").strip()
                if txt:
                    log_debug(T("全局钉图：使用系统剪贴板文字转图片"), "PinFromClipboard")
                    return render_text_to_image(txt)
    except Exception as e:
        log_error(T("读取系统剪贴板失败: {e}", e=e), "PinFromClipboard")

    return None


def create_pin_from_latest_clipboard(config_manager=None) -> bool:
    """从剪贴板最新内容创建钉图，成功返回 True。"""
    image = get_latest_clipboard_qimage()
    if image is None or image.isNull():
        log_warning(T("剪贴板为空或无可钉内容，无法钉图"), "PinFromClipboard")
        return False

    if config_manager is None:
        try:
            from settings import get_tool_settings_manager
            config_manager = get_tool_settings_manager()
        except Exception:
            config_manager = None

    try:
        from pin.pin_manager import PinManager
        pin_manager = PinManager.instance()
        pos = _screen_center_position(image)
        pin = pin_manager.create_pin(
            image=image,
            position=pos,
            config_manager=config_manager,
        )
        try:
            pin.show()
            pin.raise_()
            pin.activateWindow()
        except Exception:
            pass
        log_info(T("已从剪贴板创建钉图: {w}x{h}", w=image.width(), h=image.height()),
                 "PinFromClipboard")
        return True
    except Exception as e:
        log_error(T("从剪贴板创建钉图失败: {e}", e=e), "PinFromClipboard")
        return False
