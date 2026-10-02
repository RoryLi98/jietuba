"""main/core/ 目录下 log_* 调用的中→英翻译表。"""

TRANSLATIONS: dict[str, str] = {
    "[WARN] [Logger] 日志功能已禁用": "[WARN] [Logger] Logging is disabled",
    "[WARN] [Logger] 日志系统已经初始化": "[WARN] [Logger] Logging system already initialized",
    "[OK] [Logger] 日志系统启动成功，日志文件：{log_path}": "[OK] [Logger] Logging system started, log file: {log_path}",
    "[ERROR] [Logger] 无法创建日志文件: {e}": "[ERROR] [Logger] Failed to create log file: {e}",
    "日志级别已设置为: {level_name}": "Log level set to: {level_name}",
    "📝 [Logger] 日志已启用": "📝 [Logger] Logging enabled",
    "🔇 [Logger] 日志已禁用": "🔇 [Logger] Logging disabled",
    "[Logger] 日志系统已初始化，无法更改日志目录": "[Logger] Logging system already initialized, cannot change log directory",
    "[OK] [Logger] 日志目录已设置为: {log_dir}": "[OK] [Logger] Log directory set to: {log_dir}",
    "🛑 [Logger] 日志系统关闭": "🛑 [Logger] Logging system shut down",
    "✂️ [日志截断] {name} 超过 {max_kb} KB，已截断": "✂️ [Log truncated] {name} exceeded {max_kb} KB, truncated",
    "[WARN] [日志截断] 处理 {name} 失败: {e}": "[WARN] [Log truncation] Failed to process {name}: {e}",
    "🗑️ [日志清理] 已删除 {deleted_count} 个过期日志文件（保留 {retention_days} 天）":
        "🗑️ [Log cleanup] Deleted {deleted_count} expired log file(s) (retention: {retention_days} days)",
    "[WARN] [日志清理] 清理失败: {e}": "[WARN] [Log cleanup] Cleanup failed: {e}",

    # core/constants.py
    "系统默认文字字体: {family}": "System default text font: {family}",

    # core/clipboard_utils.py
    "剪切板: 图像为空": "Clipboard: image is empty",
    "剪切板: Win32 写入失败 ({e})": "Clipboard: Win32 write failed ({e})",
    "图像投递: 图像为空，跳过": "Image delivery: image is empty, skipping",
    "图像投递: 未请求复制或保存，跳过": "Image delivery: neither copy nor save was requested, skipping",
    "图像投递: 后台任务失败 ({exc})": "Image delivery: background task failed ({exc})",
    "图像投递: 预留保存路径失败，剪贴板不写入文件路径 ({exc})":
        "Image delivery: failed to reserve a save path, the clipboard carries no file path ({exc})",
    "剪贴板: {path_name} 写入时剪贴板被占用，准备重试 {attempt_next}/{total_attempts}":
        "Clipboard: {path_name} write found the clipboard busy, retrying {attempt_next}/{total_attempts}",
    "已复制到剪切板 (Win32 CF_DIBV5 + PNG)": "Copied to clipboard (Win32 CF_DIBV5 + PNG)",
    "已复制到剪切板 (Qt)": "Copied to clipboard (Qt)",

    # core/export.py
    "选区为空": "Selection is empty",
    "导出选区: {selection_rect}, 目标大小: {w}x{h}": "Exporting selection: {selection_rect}, target size: {w}x{h}",
    "导出完成: {out_width}x{out_height}": "Export complete: {out_width}x{out_height}",
    "导出底图: {selection_rect}, 目标大小: {w}x{h}": "Exporting base image: {selection_rect}, target size: {w}x{h}",
    "导出底图完成: {out_width}x{out_height}": "Base image export complete: {out_width}x{out_height}",

    # core/i18n.py
    "加载 XML 翻译文件失败: {e}": "Failed to load XML translation file: {e}",
    "不支持的语言: {lang_code}": "Unsupported language: {lang_code}",
    "QApplication 实例不存在": "QApplication instance does not exist",
    "已加载语言 (QM): {lang_name} ({lang_code})": "Loaded language (QM): {lang_name} ({lang_code})",
    "QM 文件加载失败，尝试 XML: {qm_file}": "Failed to load QM file, falling back to XML: {qm_file}",
    "已加载语言 (XML): {lang_name} ({lang_code})": "Loaded language (XML): {lang_name} ({lang_code})",
    "加载翻译文件失败: {xml_file}": "Failed to load translation file: {xml_file}",
    "翻译文件不存在: {xml_file}，使用默认文本": "Translation file not found: {xml_file}, using default text",

    # core/bootstrap.py
    "已终止旧实例 (PID {old_pid})": "Terminated previous instance (PID {old_pid})",
    "旧实例 (PID {old_pid}) 终止失败": "Failed to terminate previous instance (PID {old_pid})",
    "实例记录 (PID {old_pid}) 未通过身份校验，不终止": "Instance record (PID {old_pid}) failed identity verification, not terminating",
    "终止旧实例": "Terminating previous instance",
    "写入实例记录": "Writing instance record",
    "清理实例记录": "Cleaning up instance record",
    "资源锁定：成功 {pinned} 个，失败 {failed} 个": "Resource pinning: {pinned} succeeded, {failed} failed",
    "资源锁定：已保护 {pinned} 个打包资源文件": "Resource pinning: protected {pinned} bundled resource file(s)",
    "预加载步骤执行失败": "Preload step execution failed",
    "工具栏预加载完成": "Toolbar preload complete",
    "工具栏预加载失败: {e}": "Toolbar preload failed: {e}",
    "开始预加载截图相关模块...": "Starting preload of screenshot-related modules...",
    "mss 模块已加载并预热": "mss module loaded and warmed up",
    "win32gui 模块已加载": "win32gui module loaded",
    "win32gui 未安装，跳过": "win32gui not installed, skipping",
    "win32clipboard 模块已加载": "win32clipboard module loaded",
    "win32clipboard 未安装，跳过": "win32clipboard not installed, skipping",
    "PNG 编码器已预热": "PNG encoder warmed up",
    "预热PNG编码器": "Warming up PNG encoder",
    "UI 组件已加载": "UI components loaded",
    "CaptureService 已加载": "CaptureService loaded",
    "截图模块预加载完成": "Screenshot module preload complete",
    "截图模块预加载失败: {e}": "Screenshot module preload failed: {e}",
    "开始在后台线程预加载 OCR 模块和引擎...": "Starting background-thread preload of OCR module and engine...",
    "OCR 模块不可用（无OCR版本）": "OCR module unavailable (non-OCR build)",
    "OCR 预加载成功": "OCR preload succeeded",
    "OCR 引擎预加载失败": "OCR engine preload failed",
    "OCR 模块不存在（无OCR版本）": "OCR module not found (non-OCR build)",
    "OCR 预加载失败: {e}": "OCR preload failed: {e}",
    "OCR 引擎预加载失败: {e}": "OCR engine preload failed: {e}",
    "预加载设置窗口...": "Preloading settings window...",
    "设置窗口预加载完成": "Settings window preload complete",
    "启动时显示主界面": "Showing main window on startup",
    "剪贴板功能已禁用，跳过剪贴板窗口预创建": "Clipboard feature disabled, skipping clipboard window precreation",
    "剪贴板窗口预创建完成（未显示）": "Clipboard window precreated (not shown)",
    "剪贴板窗口预创建失败: {e}": "Clipboard window precreation failed: {e}",

    # core/platform_utils.py
    "释放工作集": "Releasing working set",
    "加载 kernel32": "Loading kernel32",
    "查询进程标识": "Querying process identity",
    "终止进程": "Terminating process",

    # core/resource_manager.py
    "SVG 渲染图标": "Rendering SVG icon",
    "图标渲染失败，本次返回空图标: {svg_path}": "Icon rendering failed, returning an empty icon for now: {svg_path}",

    # core/save.py
    "保存失败 {target_path}: {exc}": "Save failed {target_path}: {exc}",
    "已保存文件: {target_path}": "File saved: {target_path}",
    "保存失败: {target_path}": "Save failed: {target_path}",
    "已保存PDF: {target_path}": "PDF saved: {target_path}",
    "保存PDF失败 {target_path}: {exc}": "PDF save failed {target_path}: {exc}",
    "保存路径不是绝对路径，回退到默认: {target_dir}": "Save path is not absolute, falling back to default: {target_dir}",
    "清理失败的占位文件": "Cleaning up failed placeholder file",

    # core/shortcut_manager.py
    "系统热键已临时禁用，忽略回调 (id={hotkey_id})": "System hotkey temporarily disabled, ignoring callback (id={hotkey_id})",
    "热键回调 id={hotkey_id}": "Hotkey callback id={hotkey_id}",
    "系统组合键回调 name={name}": "Taken-over system hotkey callback name={name}",
    "ShortcutManager 已安装（KeyPress + WM_HOTKEY）": "ShortcutManager installed (KeyPress + WM_HOTKEY)",
    "注销 handler: {handler_name}": "Unregistered handler: {handler_name}",
    "按键被 {handler_name} 消费 (key=0x{key_hex:X})": "Key consumed by {handler_name} (key=0x{key_hex:X})",
    "系统热键被 {handler_name} 拦截 (id={hotkey_id})": "System hotkey intercepted by {handler_name} (id={hotkey_id})",
    "检查快捷键可用性": "Checking hotkey availability",
}
