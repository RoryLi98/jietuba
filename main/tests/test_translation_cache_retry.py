# -*- coding: utf-8 -*-
"""翻译结果缓存与瞬时故障重试的行为测试。

- 同 (provider, 语言对, 文本, 选项) 的重复翻译直接命中缓存，不再起线程；
- 只缓存成功结果，错误不进缓存（瞬时故障下次仍会真实重试）；
- worker 对超时/网络抖动/限流做短退避重试，认证/配额类错误不重试；
- 已被新请求替代（requestInterruption）的 worker 不再发起网络调用。
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QThread

from translation.models import (
    TranslationErrorCode,
    TranslationRequest,
    TranslationResult,
)
from translation.worker import TranslationWorker


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _ok_result(text="translated"):
    return TranslationResult(success=True, translated_text=text)


def _error_result(code=TranslationErrorCode.NETWORK_ERROR):
    return TranslationResult(success=False, error_code=code, error_message=str(code))


class TestResultCache:
    def test_second_identical_request_hits_cache(self, qapp, monkeypatch):
        from translation.translation_manager import TranslationManager

        manager = TranslationManager()
        worker_cls = MagicMock()
        fake_thread = MagicMock()
        worker_cls.return_value = fake_thread
        monkeypatch.setattr("translation.worker.TranslationWorker", worker_cls)

        delivered = []
        monkeypatch.setattr(manager, "_on_translation_finished",
                            lambda ok, text, err, lang, target: delivered.append((ok, text)))

        manager._start_translation("同一段文字", "en", "auto", "dialog")
        assert worker_cls.call_count == 1, "首次请求应创建 worker"
        # 模拟 worker 成功回包
        manager._on_thread_result(fake_thread, manager._request_token,
                                  True, "translated", "", "auto")

        manager._start_translation("同一段文字", "en", "auto", "dialog")
        assert worker_cls.call_count == 1, "命中缓存不应再创建 worker"
        assert delivered[-1] == (True, "translated")

        # 改文本 → 缓存未命中，走网络
        manager._start_translation("另一段", "en", "auto", "dialog")
        assert worker_cls.call_count == 2

    def test_failures_are_not_cached(self, qapp, monkeypatch):
        from translation.translation_manager import TranslationManager

        manager = TranslationManager()
        worker_cls = MagicMock()
        fake_thread = MagicMock()
        worker_cls.return_value = fake_thread
        monkeypatch.setattr("translation.worker.TranslationWorker", worker_cls)
        monkeypatch.setattr(manager, "_on_translation_finished", lambda *a: None)

        manager._start_translation("会失败的文本", "en", "auto", "dialog")
        manager._on_thread_result(fake_thread, manager._request_token,
                                  False, "", "network down", "")
        assert manager._result_cache == {}, "失败结果不得进缓存"

        manager._start_translation("会失败的文本", "en", "auto", "dialog")
        assert worker_cls.call_count == 2, "失败后再次请求应真实重试"


class TestTransientRetry:
    def _worker(self, provider):
        worker = TranslationWorker.__new__(TranslationWorker)
        QThread.__init__(worker)
        worker._provider = provider
        worker._request = TranslationRequest(
            text="x", target_lang="en", source_lang="auto",
            preserve_formatting=False, options={},
        )
        worker._configuration_error = ""
        return worker

    def test_network_error_is_retried_then_succeeds(self, qapp):
        results = [_error_result(TranslationErrorCode.NETWORK_ERROR), _ok_result()]
        calls = []

        class _Provider:
            def is_configured(self):
                return True

            def translate(self, request):
                calls.append(1)
                return results[len(calls) - 1]

        worker = self._worker(_Provider())
        worker.run()

        assert len(calls) == 2, "网络错误应重试一次"
        assert worker._last_result.success is True

    def test_auth_error_is_not_retried(self, qapp):
        calls = []

        class _Provider:
            def is_configured(self):
                return True

            def translate(self, request):
                calls.append(1)
                return _error_result(TranslationErrorCode.AUTH_FAILED)

        worker = self._worker(_Provider())
        worker.run()

        assert len(calls) == 1, "认证失败重试无益，不应重试"

    def test_interrupted_worker_does_not_retry(self, qapp):
        """在途请求被中断后，重试循环不得再次发起请求（正在跑的 urlopen 无法
        从外部中止，能守住的是"不再发下一次"）。"""
        import threading

        calls = []
        entered = threading.Event()
        release = threading.Event()

        class _Provider:
            def is_configured(self):
                return True

            def translate(self, request):
                calls.append(1)
                if len(calls) == 1:
                    entered.set()
                    release.wait(2.0)  # 模拟在途网络请求
                    return _error_result(TranslationErrorCode.NETWORK_ERROR)
                return _ok_result()

        worker = self._worker(_Provider())
        worker.start()
        try:
            assert entered.wait(2.0)
            worker.requestInterruption()
            release.set()
            worker.wait(5000)
            assert calls == [1], "中断后重试循环必须停手"
            assert worker._last_result.success is False
        finally:
            release.set()
            worker.wait(2000)
