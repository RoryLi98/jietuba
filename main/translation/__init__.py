# -*- coding: utf-8 -*-
"""
翻译模块 - 提供文字翻译功能

主要组件:
- DeepLService: DeepL API 调用服务
- TranslationDialog: 翻译结果显示窗口
- TranslationManager: 翻译窗口单例管理器（推荐使用）

全部导出惰性加载（PEP 562）：translation_dialog 依赖 qframelesswindow
等第三方库，deepl_service 也在导入期做不少初始化。设置界面、智能翻译
控制器、钉图翻译等都只需要本包的一两个符号，惰性化之后这些路径不必再
为翻译窗口的完整导入链买单。
"""

_LAZY_EXPORTS = {
    "DeepLService": ".deepl_service",
    "TranslationThread": ".deepl_service",
    "TranslationErrorCode": ".models",
    "TranslationRequest": ".models",
    "TranslationResult": ".models",
    "ProviderMetadata": ".provider",
    "TranslationProvider": ".provider",
    "ProviderRegistry": ".registry",
    "TranslationService": ".service",
    "create_default_translation_service": ".service",
    "TranslationDialog": ".translation_dialog",
    "TranslationLoadingDialog": ".translation_dialog",
    "TranslationManager": ".translation_manager",
    "TranslationWorker": ".worker",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name):
    path = _LAZY_EXPORTS.get(name)
    if path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module(path, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)
