# -*- coding: utf-8 -*-
"""启动速度预设（其他页）与五个 preload_* 开关的联动测试。"""

import pytest

from ui.settings_ui.dialog import SettingsDialog


@pytest.fixture
def dialog(qapp, monkeypatch):
    """构造设置对话框，拦截系统级副作用（热键探测、自启动注册表、警告框）。"""
    monkeypatch.setattr(
        "ui.settings_ui.dialog.validate_global_hotkey_edits",
        lambda *a, **k: True,
    )
    import ui.dialogs as dialogs_mod
    monkeypatch.setattr(dialogs_mod, "show_warning_dialog", lambda *a, **k: None)
    from ui.welcome.page6_finish import FinishPage
    monkeypatch.setattr(FinishPage, "_set_autostart", classmethod(lambda cls, e: None))
    # accept() 里的主题切换会触发 QApplication 级样式表重刷，本进程与测试无关
    from core.ui_theme import get_ui_theme
    monkeypatch.setattr(get_ui_theme(), "set_mode", lambda mode, persist=True: None)
    # 关窗时的未保存确认是原生模态框，离屏进程里直接崩溃，统一按「不保存」处理
    monkeypatch.setattr(
        SettingsDialog, "_confirm_close_with_unsaved_changes",
        lambda self: "discard",
    )

    dlg = SettingsDialog()  # config_manager=None → 内部使用 MockConfig
    dlg.show()
    yield dlg
    dlg.close()


def test_preset_combo_offers_three_levels(dialog):
    combo = dialog.preload_preset_combo
    assert [combo.itemData(i) for i in range(combo.count())] == [
        "standard", "fast", "custom",
    ]


def test_accept_with_fast_preset_disables_all_preloads(dialog, monkeypatch):
    writes = {}
    monkeypatch.setattr(
        dialog.config_manager, "set_app_setting",
        lambda key, value: writes.__setitem__(key, value),
    )
    dialog.preload_preset_combo.setCurrentIndex(
        dialog.preload_preset_combo.findData("fast")
    )
    dialog.accept()

    for key in dialog._preload_preset_keys:
        assert writes.get(key) is False, key


def test_accept_with_standard_preset_enables_all_preloads(dialog, monkeypatch):
    writes = {}
    monkeypatch.setattr(
        dialog.config_manager, "set_app_setting",
        lambda key, value: writes.__setitem__(key, value),
    )
    dialog.preload_preset_combo.setCurrentIndex(
        dialog.preload_preset_combo.findData("standard")
    )
    dialog.accept()

    for key in dialog._preload_preset_keys:
        assert writes.get(key) is True, key


def test_accept_with_custom_preset_respects_developer_toggles(dialog, monkeypatch):
    """「自定义」档：写入值必须等于开发者页开关的状态，而不是预设模板。"""
    writes = {}
    monkeypatch.setattr(
        dialog.config_manager, "set_app_setting",
        lambda key, value: writes.__setitem__(key, value),
    )
    # 开关保持默认（全 True），把 ocr 单独关掉，模拟用户在开发者页的单独调整
    dialog.preload_ocr_toggle.setChecked(False)
    dialog.preload_preset_combo.setCurrentIndex(
        dialog.preload_preset_combo.findData("custom")
    )
    dialog.accept()

    for key in dialog._preload_preset_keys:
        expected = key != "preload_ocr"
        assert writes.get(key) is expected, (key, writes.get(key))


def test_refresh_derives_preset_from_config_values(dialog, monkeypatch):
    """五个键全 False 时回推为 fast；混合组合回推为 custom。"""
    values = {key: False for key in dialog._preload_preset_keys}
    monkeypatch.setattr(
        dialog.config_manager, "get_app_setting",
        lambda key, default=None: values.get(key, default),
    )
    dialog.refresh_settings()
    assert dialog.preload_preset_combo.currentData() == "fast"

    values["preload_ocr"] = True  # 混合状态
    dialog.refresh_settings()
    assert dialog.preload_preset_combo.currentData() == "custom"


def test_reset_restores_standard(dialog):
    dialog.preload_preset_combo.setCurrentIndex(
        dialog.preload_preset_combo.findData("fast")
    )
    dialog._reset_misc_page()
    assert dialog.preload_preset_combo.currentData() == "standard"
