"""
钉图控制按钮管理器

负责管理钉图窗口右上角的窗口控制区（工具栏切换、关闭）。

设计（对标 Windows 资源管理器标题栏右上角）：
- 无底衬、无胶囊：两个字形按钮直接贴边放在右上角，
  平时透明，悬停才出现浅灰底，关闭悬停为红色白字。
- 用字形（× / ⚙）而不用 SVG 图标：字体渲染、跟随界面缩放，
  深浅主题下不会出现半残图标；hover 时换色用样式表 color 一句话搞定。
- 按钮保持为 PinWindow 的直接子控件、窗口坐标系：
  OCR 文字层的点击穿透判断依赖 button.geometry() 的窗口坐标。
- 所有显隐都走本管理器的方法，外部不要直接 show/hide 单个按钮。
"""

from PySide6.QtWidgets import QPushButton, QWidget, QGraphicsDropShadowEffect
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor


class PinControlButtons:
    """
    钉图控制按钮管理器

    管理工具栏切换按钮、关闭按钮的创建、样式、位置与显隐。
    """

    # 设计尺寸（逻辑像素，字体与尺寸会随界面缩放自动放大）
    BUTTON_WIDTH = 40
    BUTTON_HEIGHT = 32
    GLYPH_SIZE = 20
    # 字形：关闭 ×，工具栏切换 ⚙（均为系统字体必然覆盖的字形）
    CLOSE_GLYPH = "\u00d7"
    TOOLBAR_GLYPH = "\u2699"

    def __init__(self, parent: QWidget):
        """
        初始化控制按钮管理器

        Args:
            parent: 父窗口（PinWindow）
        """
        self.parent = parent
        self.close_button = None
        self.toolbar_toggle_button = None

        self._create_buttons()

    def _create_buttons(self):
        """创建所有控制按钮"""
        # 1. 工具栏切换按钮（在左）
        self.toolbar_toggle_button = self._create_button(
            glyph=self.TOOLBAR_GLYPH,
            tooltip="Show toolbar",
            style=self._get_toolbar_button_style()
        )

        # 2. 关闭按钮（在右，贴边）
        self.close_button = self._create_button(
            glyph=self.CLOSE_GLYPH,
            tooltip="Close (ESC)",
            style=self._get_close_button_style()
        )

    def _create_button(self, glyph: str, tooltip: str, style: str) -> QPushButton:
        """
        创建单个字形按钮

        Args:
            glyph: 显示的字形
            tooltip: 按钮提示文字
            style: 按钮样式表

        Returns:
            创建的按钮
        """
        button = QPushButton(glyph, self.parent)
        button.setFixedSize(self.BUTTON_WIDTH, self.BUTTON_HEIGHT)
        font = button.font()
        font.setPixelSize(self.GLYPH_SIZE)
        font.setFamilies([
            "Segoe UI Symbol", "Segoe UI",
            "Microsoft YaHei UI", "sans-serif",
        ])
        button.setFont(font)
        button.setStyleSheet(style)
        button.setToolTip(self.parent.tr(tooltip))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 白色外发光：在深色图像上也能看清深色字形
        halo = QGraphicsDropShadowEffect(button)
        halo.setBlurRadius(8)
        halo.setOffset(0, 0)
        halo.setColor(QColor(255, 255, 255, 200))
        button.setGraphicsEffect(halo)
        button.hide()  # 初始隐藏

        return button

    def _get_close_button_style(self) -> str:
        """关闭按钮样式（对标资源管理器：透明底，悬停红底白字）"""
        return """
            QPushButton {
                background-color: transparent;
                border: none;
                border-radius: 0px;
                padding: 0px;
                color: #1A1A1A;
            }
            QPushButton:hover {
                background-color: #E81123;
                color: #FFFFFF;
            }
            QPushButton:pressed {
                background-color: #C50F1F;
                color: #FFFFFF;
            }
        """

    def _get_toolbar_button_style(self) -> str:
        """工具栏切换按钮样式（透明底，悬停浅灰）"""
        return """
            QPushButton {
                background-color: transparent;
                border: none;
                border-radius: 0px;
                padding: 0px;
                color: #1A1A1A;
            }
            QPushButton:hover {
                background-color: rgba(0, 0, 0, 0.08);
            }
            QPushButton:pressed {
                background-color: rgba(0, 0, 0, 0.14);
            }
        """

    def update_positions(self, window_width: int):
        """
        更新按钮位置（右上贴边，窗口坐标系）

        Args:
            window_width: 窗口宽度
        """
        bw = self.close_button.width()
        bh = self.close_button.height()

        # 关闭按钮贴右上边
        close_x = max(0, window_width - bw)
        self.close_button.move(close_x, 0)

        # 工具栏切换按钮在关闭按钮左边
        self.toolbar_toggle_button.resize(bw, bh)
        self.toolbar_toggle_button.move(max(0, close_x - bw), 0)

        self._restack()

    def _restack(self):
        """按钮置于内容视图之上。"""
        self.close_button.raise_()
        self.toolbar_toggle_button.raise_()


    def hide_all(self):
        """隐藏所有控制按钮"""
        self.close_button.hide()
        self.toolbar_toggle_button.hide()


    def raise_all(self):
        """两个按钮回到最上层"""
        self.close_button.raise_()
        self.toolbar_toggle_button.raise_()

    def set_visible(self, close: bool, toolbar: bool):
        """显隐由 PinHoverControls 决定，这里只照做"""
        for button, visible in ((self.close_button, close), (self.toolbar_toggle_button, toolbar)):
            if visible != button.isHidden():
                continue
            button.setVisible(visible)
            if visible:
                button.raise_()
    
    def connect_signals(self, close_handler, toggle_toolbar_handler):
        """
        连接按钮信号

        Args:
            close_handler: 关闭按钮点击处理函数
            toggle_toolbar_handler: 工具栏切换按钮点击处理函数
        """
        self.close_button.clicked.connect(close_handler)
        self.toolbar_toggle_button.clicked.connect(toggle_toolbar_handler)

