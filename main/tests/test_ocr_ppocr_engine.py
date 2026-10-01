# -*- coding: utf-8 -*-
"""
ppocr_rust 引擎在 OCRManager 里的生命周期

扩展从「进程级全局单例 + Python 侧一个 ready 布尔标志」改成「持有 Engine 对象」
之后，初始化 / 识别 / 释放三条路径的状态判断全部改由「对象是否存在」决定，
原先那个与扩展内部重复的标志被删掉了。这里把这套状态机钉住：

- 初始化幂等，且失败时不留下半初始化的对象
- TextLine 的 .points/.text/.score 被正确摊平成下游要的 [[[x,y]x4], text, score]
- 空文本行被丢弃
- 释放会真的调用 close()（模型有几十 MB，不能只重置标记等进程退出）
- 识别取到局部变量后引擎才被别的线程释放时，走错误分支而不是抛 AttributeError
"""
import sys

import pytest
from PySide6.QtGui import QImage

from ocr import ocr_manager as om


class _FakeTextLine:
    """冒充扩展返回的 TextLine：具名字段，不是裸元组。"""

    def __init__(self, points, text, score):
        self.points = points
        self.text = text
        self.score = score


def _disable_upscale(monkeypatch):
    """关掉"OCR图像放大"设置（默认开，本文件多数用例不关心它）。"""

    class _Cfg:
        def get_ocr_upscale_enabled(self):
            return False

        def get_ocr_upscale_factor(self):
            return 2.0

    import settings.tool_settings as _ts

    monkeypatch.setattr(_ts, "get_tool_settings_manager", lambda: _Cfg())


class _FakeEngine:
    def __init__(self, det_path, rec_path):
        self.det_path = det_path
        self.rec_path = rec_path
        self.closed = False
        self.recognize_calls = []
        self.lines = [
            _FakeTextLine([(0.0, 0.0), (9.0, 0.0), (9.0, 5.0), (0.0, 5.0)], "hello", 0.9)
        ]

    def recognize(self, data, w, h, stride):
        self.recognize_calls.append((len(data), w, h, stride))
        return self.lines

    def close(self):
        self.closed = True


class _FakeModule:
    """冒充 ppocr_rust 扩展模块。"""

    def __init__(self):
        self.instances = []
        self.raise_on_new = None

    def Engine(self, det_path, rec_path):  # noqa: N802 —— 冒充扩展里的类名
        if self.raise_on_new is not None:
            raise self.raise_on_new
        eng = _FakeEngine(det_path, rec_path)
        self.instances.append(eng)
        return eng


@pytest.fixture
def fake_ppocr(monkeypatch):
    """把假扩展塞进 sys.modules，并让模块级的可用性探测结果为「可用」。"""
    module = _FakeModule()
    monkeypatch.setitem(sys.modules, "ppocr_rust", module)
    monkeypatch.setattr(om, "PP_RUST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(om, "_pp_det", "det.onnx", raising=False)
    monkeypatch.setattr(om, "_pp_rec", "rec.onnx", raising=False)
    return module


@pytest.fixture
def manager(monkeypatch):
    """OCRManager 是单例，每条用例给一个干净的实例。"""
    monkeypatch.setattr(om.OCRManager, "_instance", None, raising=False)
    monkeypatch.setattr(om.OCRManager, "_initialized", False, raising=False)
    mgr = om.OCRManager()
    yield mgr
    om.OCRManager._instance = None
    om.OCRManager._initialized = False


def _image(w=12, h=6):
    img = QImage(w, h, QImage.Format.Format_RGB888)
    img.fill(0)
    return img


class TestInitialize:

    def test_creates_engine_with_configured_model_paths(self, manager, fake_ppocr):
        assert manager._initialize_ppocr_rust() is True
        assert len(fake_ppocr.instances) == 1
        assert (fake_ppocr.instances[0].det_path, fake_ppocr.instances[0].rec_path) == (
            "det.onnx", "rec.onnx")

    def test_is_idempotent(self, manager, fake_ppocr):
        assert manager._initialize_ppocr_rust() is True
        assert manager._initialize_ppocr_rust() is True
        # 已有引擎就直接返回，不再构造第二个
        assert len(fake_ppocr.instances) == 1

    def test_failure_leaves_no_half_built_engine(self, manager, fake_ppocr):
        fake_ppocr.raise_on_new = RuntimeError("模型加载失败")
        assert manager._initialize_ppocr_rust() is False
        assert manager._pp_engine is None
        assert "模型加载失败" in manager._last_error

    def test_unavailable_extension_short_circuits(self, manager, monkeypatch):
        monkeypatch.setattr(om, "PP_RUST_AVAILABLE", False, raising=False)
        assert manager._initialize_ppocr_rust() is False


class TestRecognize:

    def test_textline_fields_are_flattened_for_downstream(self, manager, fake_ppocr, qapp, monkeypatch):
        # 本测试只管字段摊平，预放大另测（见 TestPpocrUpscale）——这里显式关掉
        _disable_upscale(monkeypatch)
        result = manager._recognize_with_ppocr_rust(_image(), "dict")
        assert result["code"] == 100
        box, text, score = result["data"][0]["box"], result["data"][0]["text"], result["data"][0]["score"]
        assert text == "hello"
        assert score == pytest.approx(0.9)
        # points 的元组被摊成 [[x, y], ...]，四个角点
        assert box == [[0.0, 0.0], [9.0, 0.0], [9.0, 5.0], [0.0, 5.0]]

    def test_initializes_on_first_use(self, manager, fake_ppocr, qapp):
        manager._recognize_with_ppocr_rust(_image(), "text")
        assert len(fake_ppocr.instances) == 1
        assert fake_ppocr.instances[0].recognize_calls

    def test_blank_lines_are_dropped(self, manager, fake_ppocr, qapp):
        manager._initialize_ppocr_rust()
        fake_ppocr.instances[0].lines = [_FakeTextLine([(0.0, 0.0)] * 4, "", 0.5)]
        assert manager._recognize_with_ppocr_rust(_image(), "text") == "[未识别到文字]"

    def test_no_lines_at_all(self, manager, fake_ppocr, qapp):
        manager._initialize_ppocr_rust()
        fake_ppocr.instances[0].lines = []
        assert manager._recognize_with_ppocr_rust(_image(), "text") == "[未识别到文字]"

    def test_engine_released_concurrently_reports_error(self, manager, fake_ppocr, qapp):
        """引擎在另一线程被释放时走错误分支，而不是对 None 取属性崩掉。"""
        manager._initialize_ppocr_rust()
        manager._pp_engine = None
        monkey_done = {"called": False}

        def _no_reinit():
            monkey_done["called"] = True
            return True   # 谎称初始化成功但不真的建引擎，模拟竞态窗口

        manager._initialize_ppocr_rust = _no_reinit
        out = manager._recognize_with_ppocr_rust(_image(), "text")
        assert monkey_done["called"]
        assert "已释放" in out

    def test_recognize_failure_is_reported(self, manager, fake_ppocr, qapp):
        manager._initialize_ppocr_rust()

        def _boom(*_a, **_k):
            raise RuntimeError("推理失败")

        fake_ppocr.instances[0].recognize = _boom
        assert "推理失败" in manager._recognize_with_ppocr_rust(_image(), "text")

    def test_unavailable_extension_reports_error(self, manager, monkeypatch, qapp):
        monkeypatch.setattr(om, "PP_RUST_AVAILABLE", False, raising=False)
        assert "不可用" in manager._recognize_with_ppocr_rust(_image(), "text")


class TestReleaseAndStatus:

    def test_release_closes_the_engine(self, manager, fake_ppocr):
        manager._initialize_ppocr_rust()
        engine = fake_ppocr.instances[0]
        manager.release_engine()
        # 模型有几十 MB，必须当场释放而不是等进程退出
        assert engine.closed is True
        assert manager._pp_engine is None

    def test_release_without_engine_is_harmless(self, manager, fake_ppocr):
        manager.release_engine()
        assert manager._pp_engine is None

    def test_is_engine_loaded_tracks_the_object(self, manager, fake_ppocr):
        manager._current_engine = om.OCRManager.ENGINE_PP_RUST
        assert manager.is_engine_loaded() is False
        manager._initialize_ppocr_rust()
        assert manager.is_engine_loaded() is True
        manager.release_engine()
        assert manager.is_engine_loaded() is False

    def test_memory_status_tracks_the_object(self, manager, fake_ppocr):
        manager._current_engine = om.OCRManager.ENGINE_PP_RUST
        assert manager.get_memory_status() == "未初始化"
        manager._initialize_ppocr_rust()
        assert "已初始化" in manager.get_memory_status()


class TestPpocrUpscale:
    """"OCR图像放大"设置的实际实现：小图预放大 + 坐标还原。"""

    def _manager_with_lines(self, manager, fake_ppocr):
        manager._initialize_ppocr_rust()
        fake_ppocr.instances[0].lines = [_FakeTextLine([(0.0, 0.0), (9.0, 0.0), (9.0, 5.0), (0.0, 5.0)], "hello", 0.9)]

    def _enable_upscale(self, monkeypatch, factor=2.0):
        import settings.tool_settings as _ts

        class _Cfg:
            def get_ocr_upscale_enabled(self):
                return True

            def get_ocr_upscale_factor(self):
                return factor

        monkeypatch.setattr(_ts, "get_tool_settings_manager", lambda: _Cfg())

    def test_small_image_points_scale_back(self, manager, fake_ppocr, qapp, monkeypatch):
        """小图放大 2 倍识别后，文本框坐标除回原图坐标系。

        假引擎给出的是放大坐标系里的点（0..18, 0..10），除回 2 倍后
        应与原图坐标系下的期望框（0..9, 0..5）一致。"""
        self._enable_upscale(monkeypatch, 2.0)
        manager._initialize_ppocr_rust()
        fake_ppocr.instances[0].lines = [
            _FakeTextLine([(0.0, 0.0), (18.0, 0.0), (18.0, 10.0), (0.0, 10.0)], "hello", 0.9)]
        result = manager._recognize_with_ppocr_rust(_image(12, 6), "dict")
        box = result["data"][0]["box"]
        assert box == [[0.0, 0.0], [9.0, 0.0], [9.0, 5.0], [0.0, 5.0]]
        # 识别确实发生在放大后的图上（宽 24）
        assert fake_ppocr.instances[0].recognize_calls[0][1] == 24

    def test_large_image_not_upscaled(self, manager, fake_ppocr, qapp, monkeypatch):
        """放大后超过检测边限（736）的大图不放大——检测端会缩回去，白费。"""
        self._enable_upscale(monkeypatch, 2.0)
        self._manager_with_lines(manager, fake_ppocr)
        manager._recognize_with_ppocr_rust(_image(4000, 200), "text")
        assert fake_ppocr.instances[0].recognize_calls[0][1] == 4000

    def test_disabled_keeps_original_size(self, manager, fake_ppocr, qapp, monkeypatch):
        _disable_upscale(monkeypatch)
        self._manager_with_lines(manager, fake_ppocr)
        manager._recognize_with_ppocr_rust(_image(12, 6), "text")
        assert fake_ppocr.instances[0].recognize_calls[0][1] == 12
