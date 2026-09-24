# -*- coding: utf-8 -*-
"""Baidu Translate (百度翻译开放平台) general text API provider."""

from __future__ import annotations

import hashlib
import json
import random
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from core import log_error

from ..models import (
    TranslationErrorCode,
    TranslationRequest,
    TranslationResult,
    normalize_language_code,
)
from ..provider import TranslationProvider


class BaiduTranslateProvider(TranslationProvider):
    """百度翻译通用文本翻译 API（https://fanyi-api.baidu.com）。

    鉴权：sign = MD5(appid + q + salt + secret_key)，q 为原文 UTF-8 明文。
    免费版限制 1 QPS、单次请求原文不超过 6000 字节。
    """

    provider_id = "baidu"
    display_name = "Baidu Translate"
    API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"
    MAX_TEXT_BYTES = 6000

    # 应用内 BCP-47 风格代码 → 百度语言代码
    _LANGUAGE_CODES: dict[str, str] = {
        "zh-Hans": "zh",
        "zh-Hant": "cht",
        "ja": "jp",
        "ko": "kor",
        "vi": "vie",
        "fr": "fra",
        "es": "spa",
        "ar": "ara",
        "uk": "ukr",
    }

    # 百度错误码 → 统一错误码
    #   52001 请求超时 / 52002 系统错误
    #   52003 未授权用户 / 54001 签名错误 / 58000 客户端IP非法 / 58002 服务已关闭
    #   54000 必填参数为空 / 58001 译文语言方向不支持
    #   54003 请求频率受限 / 54005 长query请求频繁
    #   54004 账户余额不足
    _ERROR_CODE_MAP: dict[str, TranslationErrorCode] = {
        "52001": TranslationErrorCode.NETWORK_ERROR,
        "52002": TranslationErrorCode.NETWORK_ERROR,
        "52003": TranslationErrorCode.AUTH_FAILED,
        "54000": TranslationErrorCode.INVALID_REQUEST,
        "54001": TranslationErrorCode.AUTH_FAILED,
        "54003": TranslationErrorCode.RATE_LIMITED,
        "54004": TranslationErrorCode.QUOTA_EXCEEDED,
        "54005": TranslationErrorCode.RATE_LIMITED,
        "58000": TranslationErrorCode.AUTH_FAILED,
        "58001": TranslationErrorCode.UNSUPPORTED_LANGUAGE,
        "58002": TranslationErrorCode.AUTH_FAILED,
    }

    def __init__(self, config: Mapping[str, Any]):
        self._app_id = str(config.get("app_id", "") or "").strip()
        self._secret_key = str(config.get("secret_key", "") or "").strip()

    def is_configured(self) -> bool:
        return bool(self._app_id and self._secret_key)

    def translate(self, request: TranslationRequest) -> TranslationResult:
        if not request.text or not request.text.strip():
            return self._error(
                TranslationErrorCode.INVALID_REQUEST, "Text is empty"
            )
        if len(request.text.encode("utf-8")) > self.MAX_TEXT_BYTES:
            return self._error(
                TranslationErrorCode.INVALID_REQUEST,
                "Text exceeds Baidu Translate's 6000-byte limit",
            )
        if not self.is_configured():
            return self._error(
                TranslationErrorCode.NOT_CONFIGURED,
                "Baidu Translate APP ID / secret key is not configured",
            )

        salt = str(random.randint(32768, 65536 * 1000))
        sign = hashlib.md5(
            f"{self._app_id}{request.text}{salt}{self._secret_key}".encode(
                "utf-8"
            )
        ).hexdigest()

        params = {
            "q": request.text,
            "from": self._to_baidu_code(request.source_lang or "auto"),
            "to": self._to_baidu_code(request.target_lang),
            "appid": self._app_id,
            "salt": salt,
            "sign": sign,
        }
        http_request = urllib.request.Request(
            url=self.API_URL,
            data=urllib.parse.urlencode(params).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        try:
            with urllib.request.urlopen(
                http_request, timeout=request.timeout
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
            return self._parse_response(raw)
        except urllib.error.HTTPError as exc:
            return self._http_error(exc)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            log_error(
                f"Baidu Translate network error: {reason}",
                "BaiduTranslate",
            )
            return self._error(
                TranslationErrorCode.NETWORK_ERROR,
                f"Network error: {reason}",
            )
        except (ValueError, UnicodeDecodeError) as exc:
            log_error(
                f"Baidu Translate response error: {exc}",
                "BaiduTranslate",
            )
            return self._error(
                TranslationErrorCode.UNKNOWN,
                "Failed to parse Baidu Translate response",
            )
        except Exception as exc:
            log_error(
                f"Baidu Translate request failed: {exc}",
                "BaiduTranslate",
            )
            return self._error(
                TranslationErrorCode.UNKNOWN,
                f"Translation failed: {exc}",
            )

    def _parse_response(self, raw: Any) -> TranslationResult:
        if not isinstance(raw, dict):
            return self._error(
                TranslationErrorCode.UNKNOWN,
                "Invalid Baidu Translate response",
            )
        error_code = str(raw.get("error_code", "") or "")
        if error_code:
            message = str(raw.get("error_msg", "") or f"Error {error_code}")
            code = self._ERROR_CODE_MAP.get(
                error_code, TranslationErrorCode.UNKNOWN
            )
            log_error(
                f"Baidu Translate error {error_code}: {message}",
                "BaiduTranslate",
            )
            return self._error(code, f"[{error_code}] {message}")

        entries = raw.get("trans_result")
        if not isinstance(entries, list) or not entries:
            return self._error(
                TranslationErrorCode.UNKNOWN,
                "Invalid Baidu Translate response",
            )
        # 多行原文按 \n 逐段返回，拼接还原
        translated = "\n".join(
            str(entry.get("dst", "") or "") for entry in entries
        )
        if not translated:
            return self._error(
                TranslationErrorCode.UNKNOWN,
                "Invalid Baidu Translate response",
            )
        detected = self._from_baidu_code(str(raw.get("from", "") or ""))
        return TranslationResult(
            success=True,
            translated_text=translated,
            detected_source_lang=detected or "",
        )

    @classmethod
    def _to_baidu_code(cls, language_code: str) -> str:
        if not language_code:
            return "auto"
        return cls._LANGUAGE_CODES.get(language_code, language_code)

    @classmethod
    def _from_baidu_code(cls, baidu_code: str) -> str:
        """百度返回的 from 代码 → 应用内部代码（用于展示检测语言）。"""
        if not baidu_code or baidu_code == "auto":
            return ""
        for internal, baidu in cls._LANGUAGE_CODES.items():
            if baidu == baidu_code:
                return internal
        return normalize_language_code(baidu_code) or ""

    def _http_error(
        self, error: urllib.error.HTTPError
    ) -> TranslationResult:
        try:
            body = error.read()
            data = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        err_code = str(data.get("error_code", "") or "") if data else ""
        message = str(
            data.get("error_msg") or error.reason or f"HTTP {error.code}"
        )
        code = self._ERROR_CODE_MAP.get(
            err_code,
            TranslationErrorCode.NETWORK_ERROR
            if error.code >= 500
            else TranslationErrorCode.UNKNOWN,
        )
        log_error(
            f"Baidu Translate HTTP {error.code}: {err_code or message}",
            "BaiduTranslate",
        )
        return self._error(code, f"[{err_code or error.code}] {message}")

    @staticmethod
    def _error(
        code: TranslationErrorCode, message: str
    ) -> TranslationResult:
        return TranslationResult(
            success=False,
            error_code=code,
            error_message=message,
        )
