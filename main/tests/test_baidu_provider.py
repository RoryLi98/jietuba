"""Baidu Translate provider unit tests (mocked HTTP layer)."""

import hashlib
import io
import json
import urllib.error
import urllib.parse

import pytest

from translation.models import TranslationErrorCode, TranslationRequest
from translation.providers.baidu import BaiduTranslateProvider
from translation.service import create_default_translation_service

APP_ID = "20250101000123456"
SECRET = "test-secret-key"


def _provider():
    return BaiduTranslateProvider({"app_id": APP_ID, "secret_key": SECRET})


def _respond(payload: bytes):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return payload

    return _Response()


def _parse_body(request) -> dict:
    """百度 API 为表单编码，解析出各字段。"""
    return dict(urllib.parse.parse_qsl(request.data.decode("utf-8")))


def test_baidu_success_and_sign(monkeypatch):
    captured = {}

    def _urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _respond(
            json.dumps(
                {
                    "from": "en",
                    "to": "zh",
                    "trans_result": [{"src": "Hello", "dst": "你好"}],
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(
        "translation.providers.baidu.urllib.request.urlopen", _urlopen
    )
    result = _provider().translate(
        TranslationRequest("Hello", "zh-Hans", source_lang="en", timeout=8)
    )

    assert result.success
    assert result.translated_text == "你好"
    assert result.detected_source_lang == "en"
    assert captured["timeout"] == 8

    body = _parse_body(captured["request"])
    assert body["appid"] == APP_ID
    assert body["q"] == "Hello"
    assert body["from"] == "en"
    assert body["to"] == "zh"
    expected_sign = hashlib.md5(
        f"{APP_ID}{body['q']}{body['salt']}{SECRET}".encode("utf-8")
    ).hexdigest()
    assert body["sign"] == expected_sign


def test_baidu_joins_multi_line_result(monkeypatch):
    def _urlopen(request, timeout):
        return _respond(
            json.dumps(
                {
                    "from": "en",
                    "to": "zh",
                    "trans_result": [
                        {"src": "Hello", "dst": "你好"},
                        {"src": "World", "dst": "世界"},
                    ],
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(
        "translation.providers.baidu.urllib.request.urlopen", _urlopen
    )
    result = _provider().translate(
        TranslationRequest("Hello\nWorld", "zh-Hans")
    )

    assert result.success
    assert result.translated_text == "你好\n世界"


def test_baidu_maps_language_codes_and_auto_source(monkeypatch):
    captured = {}

    def _urlopen(request, timeout):
        captured["body"] = _parse_body(request)
        return _respond(
            json.dumps(
                {
                    "from": "jp",
                    "to": "cht",
                    "trans_result": [{"src": "こんにちは", "dst": "你好"}],
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(
        "translation.providers.baidu.urllib.request.urlopen", _urlopen
    )
    result = _provider().translate(
        TranslationRequest("こんにちは", "zh-Hant", source_lang="ja")
    )

    assert result.success
    assert captured["body"]["to"] == "cht"
    assert captured["body"]["from"] == "jp"
    # 检测语言从百度码映射回应用内部码
    assert result.detected_source_lang == "ja"


def test_baidu_auto_source_uses_auto_keyword(monkeypatch):
    captured = {}

    def _urlopen(request, timeout):
        captured["body"] = _parse_body(request)
        return _respond(
            json.dumps(
                {
                    "from": "auto",
                    "to": "zh",
                    "trans_result": [{"src": "hi", "dst": "你好"}],
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(
        "translation.providers.baidu.urllib.request.urlopen", _urlopen
    )
    result = _provider().translate(TranslationRequest("hi", "ZH"))

    assert result.success
    assert captured["body"]["from"] == "auto"
    assert captured["body"]["to"] == "zh"
    # auto 检测时不返回语言
    assert result.detected_source_lang == ""


@pytest.mark.parametrize(
    "error_code,expected",
    [
        ("54003", TranslationErrorCode.RATE_LIMITED),
        ("54005", TranslationErrorCode.RATE_LIMITED),
        ("52003", TranslationErrorCode.AUTH_FAILED),
        ("54001", TranslationErrorCode.AUTH_FAILED),
        ("58001", TranslationErrorCode.UNSUPPORTED_LANGUAGE),
        ("54004", TranslationErrorCode.QUOTA_EXCEEDED),
        ("54000", TranslationErrorCode.INVALID_REQUEST),
        ("99999", TranslationErrorCode.UNKNOWN),
    ],
)
def test_baidu_error_code_mapping(monkeypatch, error_code, expected):
    def _urlopen(request, timeout):
        return _respond(
            json.dumps(
                {"error_code": error_code, "error_msg": "msg"}
            ).encode("utf-8")
        )

    monkeypatch.setattr(
        "translation.providers.baidu.urllib.request.urlopen", _urlopen
    )
    result = _provider().translate(TranslationRequest("Hello", "zh-Hans"))

    assert not result.success
    assert result.error_code == expected
    assert error_code in result.error_message


def test_baidu_http_error_is_mapped(monkeypatch):
    error = urllib.error.HTTPError(
        BaiduTranslateProvider.API_URL,
        403,
        "Forbidden",
        {},
        io.BytesIO(json.dumps({"error_code": "54001"}).encode("utf-8")),
    )

    def _urlopen(request, timeout):
        raise error

    monkeypatch.setattr(
        "translation.providers.baidu.urllib.request.urlopen", _urlopen
    )
    result = _provider().translate(TranslationRequest("Hello", "zh-Hans"))

    assert not result.success
    assert result.error_code == TranslationErrorCode.AUTH_FAILED


def test_baidu_network_error(monkeypatch):
    def _urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(
        "translation.providers.baidu.urllib.request.urlopen", _urlopen
    )
    result = _provider().translate(TranslationRequest("Hello", "zh-Hans"))

    assert not result.success
    assert result.error_code == TranslationErrorCode.NETWORK_ERROR


def test_baidu_not_configured():
    empty = BaiduTranslateProvider({"app_id": "", "secret_key": SECRET})
    half = BaiduTranslateProvider({"app_id": APP_ID, "secret_key": ""})
    assert not empty.is_configured()
    assert not half.is_configured()
    assert _provider().is_configured()

    for provider in (empty, half):
        result = provider.translate(TranslationRequest("Hello", "zh-Hans"))
        assert result.error_code == TranslationErrorCode.NOT_CONFIGURED


def test_baidu_rejects_invalid_input():
    result = _provider().translate(TranslationRequest("   ", "zh-Hans"))
    assert result.error_code == TranslationErrorCode.INVALID_REQUEST

    long_text = "字" * 3001  # 3001 * 3 bytes > 6000
    result = _provider().translate(TranslationRequest(long_text, "zh-Hans"))
    assert result.error_code == TranslationErrorCode.INVALID_REQUEST


def test_baidu_registered_in_default_service():
    service = create_default_translation_service()
    providers = {
        meta.provider_id: meta.display_name
        for meta in service.registry.available_providers()
    }
    assert providers["baidu"] == "Baidu Translate"
    # 微软翻译在引擎下拉框中的显示名
    assert providers["azure"] == "Microsoft Translator"
