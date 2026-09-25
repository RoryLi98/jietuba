# -*- coding: utf-8 -*-
"""杂项设置页 — Fluent Design"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea
from core.ui_scale import dialog_scaled
from ui.fluent_lite import (
    SwitchSettingCard, SettingCard as FSettingCard,
    FluentIcon, ComboBox, CaptionLabel,
)
from .components import SettingCardGroup
from core.ui_theme import set_own_style


def create_misc_page(dialog) -> QWidget:
    """创建杂项设置页面 — Fluent Design"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

    view = QWidget()
    set_own_style(view, "background: transparent;")
    layout = QVBoxLayout(view)
    layout.setContentsMargins(0, 0, dialog_scaled(10), 0)
    layout.setSpacing(dialog_scaled(20))

    # ════ 启动行为 ════
    grp_startup = SettingCardGroup(dialog.tr("Startup"), view)

    # 开机自启
    autostart_card = SwitchSettingCard(
        FluentIcon.POWER_BUTTON,
        dialog.tr("Launch on Startup"),
        dialog.tr("Register in Windows startup via registry."),
        parent=grp_startup,
    )
    autostart_card.setChecked(dialog.config_manager.get_app_setting("autostart_enabled"))
    dialog.autostart_toggle = autostart_card
    grp_startup.addSettingCard(autostart_card)

    # 主界面显示
    show_card = SwitchSettingCard(
        FluentIcon.APPLICATION,
        dialog.tr("Show Main Window on Startup"),
        dialog.tr("If off, starts in background."),
        parent=grp_startup,
    )
    show_card.setChecked(dialog.config_manager.get_show_main_window())
    dialog.show_main_window_toggle = show_card
    grp_startup.addSettingCard(show_card)

    # 启动速度预设：一键切换五个 preload_* 开关
    preload_card = FSettingCard(
        FluentIcon.STOP_WATCH,
        dialog.tr("Startup Speed"),
        dialog.tr("Fast skips all preloading for quickest launch; Custom keeps the developer-page toggles."),
        parent=grp_startup,
    )
    dialog.preload_preset_combo = ComboBox(preload_card)
    _PRELOAD_KEYS = (
        "preload_screenshot", "preload_toolbar", "preload_ocr",
        "preload_settings", "preload_clipboard",
    )
    _PRESETS = (
        ("standard", dialog.tr("Standard"), {key: True for key in _PRELOAD_KEYS}),
        ("fast", dialog.tr("Fast launch"), {key: False for key in _PRELOAD_KEYS}),
        ("custom", dialog.tr("Custom"), None),
    )
    for preset_id, label, _values in _PRESETS:
        dialog.preload_preset_combo.addItem(label, userData=preset_id)
    dialog._preload_preset_keys = _PRELOAD_KEYS
    dialog._preload_presets = {pid: values for pid, _l, values in _PRESETS}

    def _derive_preset_from_toggles():
        """根据开发者页五个开关的实际状态回推预设档位。"""
        values = {
            key: bool(dialog.config_manager.get_app_setting(key, True))
            for key in _PRELOAD_KEYS
        }
        for preset_id, _label, preset_values in _PRESETS:
            if preset_values is not None and preset_values == values:
                dialog.preload_preset_combo.setCurrentIndex(
                    dialog.preload_preset_combo.findData(preset_id)
                )
                return
        dialog.preload_preset_combo.setCurrentIndex(
            dialog.preload_preset_combo.findData("custom")
        )

    _derive_preset_from_toggles()
    preload_card.addControl(dialog.preload_preset_combo)
    grp_startup.addSettingCard(preload_card)

    def _sync_toggles_from_preset(index):
        """选预设时把开发者页五个开关的勾选态一并同步，保持两处显示一致。"""
        preset_values = dialog._preload_presets.get(
            dialog.preload_preset_combo.itemData(index)
        )
        if preset_values is None:
            return
        for key, value in preset_values.items():
            toggle = getattr(dialog, f"{key}_toggle", None)
            if toggle is not None:
                toggle.setChecked(value)

    dialog.preload_preset_combo.currentIndexChanged.connect(
        _sync_toggles_from_preset
    )

    layout.addWidget(grp_startup)

    # ════ 操作 ════
    grp_ops = SettingCardGroup(dialog.tr("Operation"), view)

    # 界面语言
    lang_card = FSettingCard(
        FluentIcon.LANGUAGE,
        dialog.tr("Language"),
        dialog.tr("Select display language. Restart required after change."),
        parent=grp_ops,
    )
    dialog.language_combo = ComboBox(lang_card)

    from core.i18n import I18nManager
    for code, name in I18nManager.get_available_languages().items():
        dialog.language_combo.addItem(name, userData=code)

    current_lang = dialog.config_manager.get_app_setting("language", "ja")
    index = dialog.language_combo.findData(current_lang)
    if index >= 0:
        dialog.language_combo.setCurrentIndex(index)
    lang_card.addControl(dialog.language_combo)
    grp_ops.addSettingCard(lang_card)

    layout.addWidget(grp_ops)

    # 提示
    hint = CaptionLabel(
        dialog.tr("💡 Hint: Even with background startup, you can operate from system tray."),
        view,
    )
    hint.setStyleSheet(f"padding: {dialog_scaled(5)}px;")
    layout.addWidget(hint)

    layout.addStretch()
    scroll.setWidget(view)
    return scroll
 
