# -*- coding: utf-8 -*-
"""快捷键设置页 — Fluent Design"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QStackedWidget, QSizePolicy,
)
from PySide6.QtCore import Qt

from ui.dialogs import show_confirm_dialog
from ui.fluent_lite import (
    ComboBox, CaptionLabel, SegmentedWidget,
)
from ui.fluent_lite.theme import ACCENT
from .components import SettingCardGroup, WhiteCard, apply_theme_text_style
from ..hotkey_edit import HotkeyEdit, validate_hotkey_group
from ..inapp_key_edit import InAppKeyEdit
from settings import ANNOTATION_TOOL_SHORTCUTS
from core.shortcut_manager import is_reserved_inapp_shortcut


# ── 应用内快捷键定义表（分组）──────────────────────────────
SCREENSHOT_KEYS = [
    ("inapp_confirm",   "Confirm Screenshot",     "ctrl+c"),
    ("inapp_pin",       "Pin Image",              "ctrl+d"),
    ("inapp_undo",      "Undo",                   "ctrl+z"),
    ("inapp_redo",      "Redo",                   "ctrl+y"),
    ("inapp_delete",    "Delete Selected",        "delete"),
    ("inapp_zoom_in",   "Magnifier Zoom In",      "pageup"),
    ("inapp_zoom_out",  "Magnifier Zoom Out",     "pagedown"),
    ("inapp_translate", "Screenshot Translate",    "shift+c"),
]

PIN_KEYS = [
    ("inapp_copy_pin",        "Copy Pinned Image",      "ctrl+c"),
    ("inapp_thumbnail",       "Toggle Thumbnail",       "r"),
    ("inapp_toggle_toolbar",  "Toggle Toolbar",         "space"),
]

TOOL_KEYS = [
    (cfg_key, label, default)
    for cfg_key, _tool_id, label, default in ANNOTATION_TOOL_SHORTCUTS
]

INAPP_KEYS = SCREENSHOT_KEYS + TOOL_KEYS + PIN_KEYS

_EDIT_W = 140
_EDIT_H = 28
_SEGMENT_HINT_STYLE = "font-size: 12px; background: transparent;"

# 全局快捷键属于同一个冲突域；任意两个业务不能占用同一个实际按键。
GLOBAL_HOTKEY_EDIT_ATTRS = (
    "hotkey_input",
    "hotkey_input_2",
    "clipboard_hotkey_edit",
    "clipboard_hotkey_edit_2",
    "translation_hotkey_edit",
    "translation_hotkey_edit_2",
    "pin_hotkey_edit",
    "pin_hotkey_edit_2",
)


def _iter_global_hotkey_edits(dialog):
    for attr in GLOBAL_HOTKEY_EDIT_ATTRS:
        edit = getattr(dialog, attr, None)
        if edit is not None:
            yield edit


def validate_global_hotkey_edits(dialog, *, check_system: bool = False) -> bool:
    """校验设置窗口上的全局快捷键。

    这里只回答「哪几个控件属于同一个冲突域」，判重规则本身在
    ui.hotkey_edit.validate_hotkey_group——欢迎向导复用的是同一份。
    """
    return validate_hotkey_group(
        _iter_global_hotkey_edits(dialog), check_system=check_system
    )


def _build_shortcut_row(dialog, parent, title: str, editor: QWidget) -> QWidget:
    row_card = WhiteCard(parent)
    row_card.setFixedHeight(46)

    row_layout = QHBoxLayout(row_card)
    row_layout.setContentsMargins(16, 0, 14, 0)
    row_layout.setSpacing(12)
    row_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

    title_label = QLabel(title, row_card)
    apply_theme_text_style(title_label, 13)
    row_layout.addWidget(title_label, 1)
    row_layout.addWidget(editor, 0, Qt.AlignmentFlag.AlignRight)
    return row_card


def _stack_page_height(row_count: int) -> int:
    return row_count * 46 + max(0, row_count - 1) * 8


def _add_hotkey_pair_row(dialog, group, title: str, attr_main: str, attr_backup: str,
                         main_text: str, backup_text: str, input_style: str) -> None:
    """全局热键单行卡：标题在左，主/备两个录入框并排在右。

    主备本就是一对，上下堆叠又高又空；单行后卡片高度减半，
    四组全局热键在默认窗口高度下几乎不用滚动。
    """
    card = WhiteCard(group)
    row = QHBoxLayout(card)
    row.setContentsMargins(16, 6, 16, 6)
    row.setSpacing(8)

    lbl = QLabel(title, card)
    apply_theme_text_style(lbl, 14)
    row.addWidget(lbl)
    row.addStretch()

    for attr, value in ((attr_main, main_text), (attr_backup, backup_text)):
        edit = HotkeyEdit()
        edit.setText(value)
        edit.setPlaceholderText(dialog.tr("e.g.: ctrl+shift+a"))
        # 不写死 200px：默认宽度下两个框会被挤叠；给最小值 + 横向拉伸，
        # 窄窗口只省略占位符不断裂，宽窗口自动填满卡片。
        edit.setMinimumWidth(160)
        edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        edit.setStyleSheet(input_style)
        setattr(dialog, attr, edit)
        row.addWidget(edit)

    card.setFixedHeight(52)
    group.addSettingCard(card)


def create_hotkey_page(dialog) -> QWidget:
    """创建快捷键设置页面 — Fluent Design"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

    view = QWidget()
    view.setStyleSheet("background: transparent;")
    layout = QVBoxLayout(view)
    layout.setContentsMargins(0, 0, 10, 16)
    layout.setSpacing(20)

    input_style = dialog._get_input_style()

    # ════ 全局热键 ════
    grp_global = SettingCardGroup(dialog.tr("Global Hotkeys"), view)

    # 截图热键（主 + 备用）
    _add_hotkey_pair_row(
        dialog, grp_global, dialog.tr("Screenshot Hotkey"),
        "hotkey_input", "hotkey_input_2",
        dialog.current_hotkey, dialog.config_manager.get_hotkey_2(),
        input_style,
    )

    # 剪贴板热键（主 + 备用）
    _add_hotkey_pair_row(
        dialog, grp_global, dialog.tr("Clipboard Hotkey"),
        "clipboard_hotkey_edit", "clipboard_hotkey_edit_2",
        dialog.config_manager.get_clipboard_hotkey(),
        dialog.config_manager.get_clipboard_hotkey_2(),
        input_style,
    )

    # 智能翻译热键（主 + 备用）
    _add_hotkey_pair_row(
        dialog, grp_global, dialog.tr("Translation Hotkey"),
        "translation_hotkey_edit", "translation_hotkey_edit_2",
        dialog.config_manager.get_translation_hotkey(),
        dialog.config_manager.get_translation_hotkey_2(),
        input_style,
    )

    # 全局钉图热键（主 + 备用）：截图内钉选区，截图外钉剪贴板最新
    _add_hotkey_pair_row(
        dialog, grp_global, dialog.tr("Pin Hotkey"),
        "pin_hotkey_edit", "pin_hotkey_edit_2",
        dialog.config_manager.get_pin_hotkey()
        if hasattr(dialog.config_manager, "get_pin_hotkey") else "",
        dialog.config_manager.get_pin_hotkey_2()
        if hasattr(dialog.config_manager, "get_pin_hotkey_2") else "",
        input_style,
    )

    # 全局热键统一判重。连接放在全部输入框创建之后，避免初始化过程中
    # 只看到半组控件；最后主动跑一次，以识别配置文件里遗留的旧冲突。
    for edit in _iter_global_hotkey_edits(dialog):
        edit.textChanged.connect(
            lambda _text, d=dialog: validate_global_hotkey_edits(d)
        )
    validate_global_hotkey_edits(dialog)

    layout.addWidget(grp_global)

    # ════ 应用内快捷键 ════
    grp_inapp = SettingCardGroup(dialog.tr("In-App Shortcuts"), view)

    dialog._inapp_edits = {}
    dialog._inapp_groups = {}

    tab_card = WhiteCard(grp_inapp)
    tab_layout = QVBoxLayout(tab_card)
    tab_layout.setContentsMargins(16, 16, 16, 16)
    tab_layout.setSpacing(12)

    tab_switch = SegmentedWidget(tab_card)
    tab_switch.setFixedHeight(34)
    tab_switch.setIndicatorColor(ACCENT, ACCENT)

    stack = QStackedWidget(tab_card)
    stack.setObjectName("InAppShortcutStack")
    stack.setStyleSheet("#InAppShortcutStack { background: transparent; border: none; }")

    def _build_tab(keys_list: list, group_name: str, extra_widgets=None) -> QWidget:
        page = QWidget()
        vbox = QVBoxLayout(page)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(8)

        for cfg_key, tr_src, default in keys_list:
            edit = InAppKeyEdit()
            edit.setFixedSize(_EDIT_W, _EDIT_H)
            edit.setStyleSheet(input_style)
            value = dialog.config_manager.get_inapp_shortcut(cfg_key)
            edit.setText("" if is_reserved_inapp_shortcut(value) else value)
            dialog._inapp_edits[cfg_key] = edit
            dialog._inapp_groups[cfg_key] = group_name

            vbox.addWidget(_build_shortcut_row(dialog, page, dialog.tr(tr_src), edit))

        if extra_widgets:
            for w in extra_widgets:
                vbox.addWidget(w)

        vbox.addStretch(1)
        return page

    # 鼠标微移模式
    dialog.cursor_move_combo = ComboBox()
    dialog.cursor_move_combo.setFixedSize(_EDIT_W, _EDIT_H)
    dialog.cursor_move_combo.addItem("WASD + ↑↓←→", userData="both")
    dialog.cursor_move_combo.addItem("↑↓←→", userData="arrows")
    dialog.cursor_move_combo.addItem("WASD", userData="wasd")

    cur_mode = dialog.config_manager.get_inapp_cursor_move_mode()
    idx = dialog.cursor_move_combo.findData(cur_mode)
    if idx >= 0:
        dialog.cursor_move_combo.setCurrentIndex(idx)

    move_row = _build_shortcut_row(
        dialog, tab_card, dialog.tr("Cursor Move Keys"), dialog.cursor_move_combo
    )

    screenshot_tab = _build_tab(
        SCREENSHOT_KEYS, "screenshot", extra_widgets=[move_row]
    )
    tools_tab = _build_tab(TOOL_KEYS, "screenshot")
    pin_tab = _build_tab(PIN_KEYS, "pin")

    stack.addWidget(screenshot_tab)
    stack.addWidget(tools_tab)
    stack.addWidget(pin_tab)

    tab_switch.addItem("screenshot", dialog.tr("Screenshot Shortcuts"), lambda: stack.setCurrentIndex(0))
    tab_switch.addItem("tools", dialog.tr("Annotation Tools"), lambda: stack.setCurrentIndex(1))
    tab_switch.addItem("pin", dialog.tr("Pin Shortcuts"), lambda: stack.setCurrentIndex(2))
    tab_switch.setCurrentItem("screenshot")

    tab_layout.addWidget(tab_switch, 0, Qt.AlignmentFlag.AlignLeft)
    tab_layout.addWidget(stack)

    screenshot_h = _stack_page_height(len(SCREENSHOT_KEYS) + 1)
    tools_h = _stack_page_height(len(TOOL_KEYS))
    pin_h = _stack_page_height(len(PIN_KEYS))
    stack.setMinimumHeight(max(screenshot_h, tools_h, pin_h))
    tab_card.setFixedHeight(max(screenshot_h, tools_h, pin_h) + 80)
    grp_inapp.addSettingCard(tab_card)

    # 冲突检测
    for cfg_key, edit in dialog._inapp_edits.items():
        edit.textChanged.connect(
            lambda text, k=cfg_key: _on_shortcut_changed(
                dialog, k, text, input_style
            )
        )

    layout.addWidget(grp_inapp)

    # 提示
    hint = CaptionLabel(
        dialog.tr("💡 Configured shortcuts take priority over WASD and C. Arrow keys remain available; Esc is reserved."),
        view,
    )
    hint.setStyleSheet("padding: 5px;")
    layout.addWidget(hint)

    layout.addStretch()
    scroll.setWidget(view)
    return scroll


# ── 输入后冲突检测（交互式弹窗）──────────────────────────

def _on_shortcut_changed(dialog, changed_key: str, new_text: str, base_style: str):
    """某个输入框值变化时，检查同组内是否冲突，弹窗询问是否替换"""
    new_text = new_text.strip().lower()
    # 忽略空值、未完成的中间态（如 "ctrl+"）
    if not new_text or new_text.endswith("+"):
        return

    my_group = dialog._inapp_groups.get(changed_key, "")

    # 找同组内与新值相同的其他 edit
    conflict_key = None
    for cfg_key, edit in dialog._inapp_edits.items():
        if cfg_key == changed_key:
            continue
        if dialog._inapp_groups.get(cfg_key, "") != my_group:
            continue
        if edit.text().strip().lower() == new_text:
            conflict_key = cfg_key
            break

    if conflict_key is None:
        return  # 无冲突

    # 找到冲突项的显示名
    conflict_label = conflict_key
    for keys_list in (SCREENSHOT_KEYS, TOOL_KEYS, PIN_KEYS):
        for cfg, tr_src, _default in keys_list:
            if cfg == conflict_key:
                conflict_label = dialog.tr(tr_src)
                break

    # 弹窗询问
    current_edit = dialog._inapp_edits[changed_key]
    conflict_edit = dialog._inapp_edits[conflict_key]

    # 阻塞信号防止递归
    current_edit.blockSignals(True)
    conflict_edit.blockSignals(True)

    ret = show_confirm_dialog(
        dialog,
        dialog.tr("Shortcut Conflict"),
        dialog.tr('"%1" is already used by "%2".\nReplace it?')
            .replace('%1', new_text.upper())
            .replace('%2', conflict_label),
    )

    if ret is True:
        # 清空旧的，保留新的
        conflict_edit.setText("")
    else:
        # 撤销本次输入，恢复旧值
        old_val = dialog.config_manager.get_inapp_shortcut(changed_key)
        current_edit.setText("" if is_reserved_inapp_shortcut(old_val) else old_val)

    current_edit.blockSignals(False)
    conflict_edit.blockSignals(False)
 
