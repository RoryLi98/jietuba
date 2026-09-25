# -*- coding: utf-8 -*-
"""DeepL 配置键迁移测试：app/ 旧前缀 → translation/providers/deepl/ 新前缀。"""
import pytest
from PySide6.QtCore import QSettings

from settings.tool_settings import ToolSettingsManager


@pytest.fixture
def manager():
    qs = QSettings("TestOrg", "DeeplMigration")
    qs.clear()
    qs.sync()
    mgr = ToolSettingsManager(qsettings=qs)
    yield mgr, qs
    qs.clear()
    qs.sync()


def test_reads_from_new_path(manager):
    mgr, qs = manager
    qs.setValue("translation/providers/deepl/api_key", "new-key")
    qs.sync()
    assert mgr.get_deepl_api_key() == "new-key"


def test_legacy_key_is_migrated_on_read(manager):
    mgr, qs = manager
    qs.setValue("app/deepl_api_key", "legacy-key")
    qs.sync()

    assert mgr.get_deepl_api_key() == "legacy-key"
    # 迁移后新键就位、旧键清除
    assert qs.value("translation/providers/deepl/api_key", "", type=str) == "legacy-key"
    assert qs.contains("app/deepl_api_key") is False


def test_legacy_use_pro_is_migrated_on_read(manager):
    mgr, qs = manager
    qs.setValue("app/deepl_use_pro", "true")
    qs.sync()

    assert mgr.get_deepl_use_pro() is True
    assert qs.contains("app/deepl_use_pro") is False


def test_set_writes_new_path_and_cleans_legacy(manager):
    mgr, qs = manager
    qs.setValue("app/deepl_api_key", "legacy-key")
    qs.sync()

    mgr.set_deepl_api_key("fresh-key")
    assert qs.value("translation/providers/deepl/api_key", "", type=str) == "fresh-key"
    assert qs.contains("app/deepl_api_key") is False

    mgr.set_deepl_use_pro(True)
    assert qs.contains("translation/providers/deepl/use_pro")
    assert mgr.get_deepl_use_pro() is True


def test_empty_new_and_empty_legacy_returns_default(manager):
    mgr, _qs = manager
    assert mgr.get_deepl_api_key() == ""
    assert mgr.get_deepl_use_pro() is False
