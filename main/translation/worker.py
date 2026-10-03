"""Qt worker shared by every translation provider."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QThread, Signal

from core.logger import log_error, T

from .models import TranslationErrorCode, TranslationRequest, TranslationResult
from .provider import TranslationProvider
from .service import TranslationService


class TranslationWorker(QThread):
    finished_signal = Signal(object)

    # 瞬时故障的退避间隔；只重试这类错误，认证/配额/语言不支持重试无益
    _RETRY_DELAYS_MS = (600, 1200)

    @staticmethod
    def _is_transient(result) -> bool:
        code = getattr(result, "error_code", None)
        try:
            return code in (
                TranslationErrorCode.NETWORK_ERROR,
                TranslationErrorCode.RATE_LIMITED,
                TranslationErrorCode.UNKNOWN,
            )
        except ValueError:
            return False

    def __init__(
        self,
        service: TranslationService,
        request: TranslationRequest,
        *,
        provider_id: str | None = None,
        provider_overrides: Mapping[str, Any] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._request = request
        self._provider: TranslationProvider | None = None
        self._configuration_error = ""
        # Resolve configuration on the GUI thread. The worker thread only owns
        # the provider's synchronous network call and never touches QSettings.
        try:
            self._provider = service.provider(
                provider_id, provider_overrides
            )
        except ValueError as exc:
            self._configuration_error = str(exc)

    def effective_timeout_ms(self) -> int:
        if self._provider is None:
            return 0
        return int(self._provider.effective_timeout(self._request) * 1000)

    def run(self) -> None:
        try:
            if self._provider is None:
                result = TranslationResult(
                    success=False,
                    error_code=TranslationErrorCode.NOT_CONFIGURED,
                    error_message=self._configuration_error,
                )
            elif not self._provider.is_configured():
                result = TranslationResult(
                    success=False,
                    error_code=TranslationErrorCode.NOT_CONFIGURED,
                    error_message=(
                        f"{self._provider.display_name} is not configured"
                    ),
                )
            else:
                if self.isInterruptionRequested():
                    # 已被新请求替代：不再发起网络调用，结果反正会被丢弃
                    return
                result = self._provider.translate(self._request)
                # 瞬时故障（超时/网络抖动/限流）短退避重试一次：一次性的
                # 网络抖动不再表现为"翻译失败"。每次重试前检查中断标志。
                for delay_ms in self._RETRY_DELAYS_MS:
                    if result.success or not self._is_transient(result):
                        break
                    if self.isInterruptionRequested():
                        break
                    self.msleep(delay_ms)
                    if self.isInterruptionRequested():
                        break
                    result = self._provider.translate(self._request)
        except Exception as exc:
            log_error(T("翻译线程异常: {exc}", exc=exc), "Translation")
            result = TranslationResult(
                success=False,
                error_code=TranslationErrorCode.UNKNOWN,
                error_message=f"Translation failed: {exc}",
            )
        self._last_result = result  # 排障与测试用；emit 与否见下面的中断判定
        if not self.isInterruptionRequested():
            self.finished_signal.emit(result)
