# -*- mode: python ; coding: utf-8 -*-
# 此 spec 文件由 build_with_ocr_onefile.py 自动生成，请勿手动修改后直接运行
import os as _os

a = Analysis(
    ['main/main_app.py'],
    pathex=['main'],
    binaries=[],
    datas=[('svg', 'svg'), ('main/translations', 'translations')],
    hiddenimports=['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtSvg', 'PySide6.QtSvgWidgets', 'PySide6.QtXml', 'pyclipboard', 'longstitch', 'gifrecorder', 'ppocr_rust', 'hdrcapture', 'zxingcpp', 'PIL', 'PIL.Image', 'mss', 'mss.windows', 'mss.base', 'mss.exception', 'mss.factory', 'mss.models', 'mss.screenshot', 'mss.tools', 'win32api', 'win32con', 'win32gui', 'comtypes.client', 'comtypes.gen.UIAutomationClient', 'inputhub', 'darkdetect', 'pin.pin_window', 'pin.pin_toolbar', 'pin.pin_canvas', 'pin.pin_canvas_view', 'pin.pin_ocr_manager', 'pin.pin_thumbnail', 'pin.pin_shortcut', 'pin.pin_controls', 'pin.pin_context_menu', 'pin.pin_translation', 'pin.ocr_text_layer', 'pin.pin_from_clipboard', 'clipboard.ui.dialogs.manage_dialog', 'clipboard.ui.windows.clipboard_window', 'clipboard.ui.windows.pin_window', 'translation.deepl_service', 'translation.models', 'translation.provider', 'translation.registry', 'translation.service', 'translation.translation_dialog', 'translation.translation_manager', 'translation.worker', 'translation.smart_translation_controller'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'numpy', 'pandas', 'torch', 'tensorflow', 'PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'pytest', 'IPython', 'jupyter', 'rapidocr', 'rapidocr_onnxruntime', 'onnxruntime', 'keyboard', 'av', 'windows_media_ocr', 'pythoncom', 'win32com', 'win32com.client', 'win32com.server', 'win32com.gen_py', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickControls2', 'PySide6.QtQuickWidgets', 'PySide6.QtQuickTest', 'PySide6.QtNetwork', 'PySide6.QtSql', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtPrintSupport', 'PySide6.QtHelp', 'PySide6.QtUiTools', 'PySide6.QtDesigner', 'PySide6.QtTest', 'PySide6.QtConcurrent', 'PySide6.QtDBus', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DCore', 'PySide6.Qt3DExtras', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DRender', 'PySide6.QtBluetooth', 'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtGraphs', 'PySide6.QtHttpServer', 'PySide6.QtLocation', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtNetworkAuth', 'PySide6.QtNfc', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtPositioning', 'PySide6.QtQuick3D', 'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtSensors', 'PySide6.QtSerialBus', 'PySide6.QtSerialPort', 'PySide6.QtSpatialAudio', 'PySide6.QtStateMachine', 'PySide6.QtTextToSpeech', 'PySide6.QtVirtualKeyboard', 'PySide6.QtWebChannel', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineQuick', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebSockets'],
    noarchive=False,
    optimize=0,
)

# ── binary 层过滤：使用白名单模式，只保留实际需要的 PySide6 DLL/pyd ──

# 白名单：只保留这些 PySide6 相关的 DLL 和 pyd
_PYSIDE6_WHITELIST = {
    # 核心 DLL
    'qt6core.dll', 'qt6gui.dll', 'qt6widgets.dll',
    'qt6svg.dll', 'qt6svgwidgets.dll', 'qt6xml.dll',
    # 核心 pyd
    'qtcore.pyd', 'qtgui.pyd', 'qtwidgets.pyd',
    'qtsvg.pyd', 'qtsvgwidgets.pyd', 'qtxml.pyd',
    # shiboken / PySide6 绑定
    'pyside6.abi3.dll', 'shiboken6.abi3.dll', 'shiboken.pyd',
    # VC++ 运行时（从 shiboken6 目录带入）
    'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'msvcp140_codecvt_ids.dll',
    'vcruntime140.dll', 'vcruntime140_1.dll',
    'concrt140.dll', 'vcamp140.dll', 'vccorlib140.dll', 'vcomp140.dll',
}

# 额外要删除的文件
_EXTRA_STRIP = {
    'opengl32sw.dll',
    # Qt 6.11 在 Windows 上使用系统 ICU。Codex/文档工具可能把 Poppler 的
    # ABI 不兼容副本注入 PATH；若 PyInstaller 收集它，QtWidgets 会在启动时
    # 报 DLL load failed。不要把这些外来 ICU DLL 冻结进应用。
    'icuuc.dll',
    # FFmpeg（Qt Multimedia 拉入，截图不需要）
    'avcodec-61.dll', 'avformat-61.dll', 'avutil-59.dll',
    'swscale-8.dll', 'swresample-5.dll',
}
_EXTRA_STRIP_PATTERNS = ('libscipy_openblas', 'libopenblas', 'icudt')
_STRIP_BINARY_PATTERNS = ('_avif.',)  # Pillow AVIF

def _keep_binary(entry):
    name = _os.path.basename(entry[1]).lower()
    # 1) 模式排除
    if any(name.startswith(p) for p in _STRIP_BINARY_PATTERNS):
        return False
    if any(name.startswith(p) for p in _EXTRA_STRIP_PATTERNS):
        return False
    # 2) 额外指定排除
    if name in _EXTRA_STRIP:
        return False
    # 3) 排除 windows_media_ocr 的 pyd/dll
    if name.startswith('windows_media_ocr'):
        return False
    # 4) PySide6 相关文件：白名单模式
    src_lower = entry[1].lower()
    if 'pyside6' in src_lower or 'shiboken6' in src_lower:
        if 'plugins' in src_lower or 'translations' in src_lower:
            return True
        return name in _PYSIDE6_WHITELIST
    # 5) 非 PySide6 文件：保留
    return True

a.binaries = TOC([entry for entry in a.binaries if _keep_binary(entry)])

# ── QM 翻译文件过滤：只保留 zh/ja，去掉其他 Qt 语言包 ──
_KEEP_QM_LOCALES = {'zh', 'zh_cn', 'zh_tw', 'ja'}
def _keep_qm(entry):
    src = entry[1]
    name = _os.path.basename(src).lower()
    if not name.endswith('.qm'):
        return True
    if 'pyside6' not in src.lower():
        return True
    stem = name[:-3]
    locale = stem.split('_', 1)[1] if '_' in stem else stem
    return locale in _KEEP_QM_LOCALES or locale.startswith('zh')
a.datas = TOC([e for e in a.datas if _keep_qm(e)])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='jietuba_pp',
    icon='托盘.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
