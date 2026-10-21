"""Вход по коду без браузера: шаги web-login на поддельных ответах hh."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hh_applicant_tool.operations import web_login
from hh_applicant_tool.operations.web_login import Operation

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBKEY = (
    PRIVATE_KEY.public_key()
    .public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    .decode()
)
LOGIN_PAGE = "https://hh.ru/account/login?backurl=%2Foauth%2Fauthorize%3Fx%3D1"


def response(status=200, data=None, url="", headers=None, content=b""):
    r = MagicMock()
    r.status_code = status
    r.url = url
    r.headers = headers or {}
    r.content = content
    r.text = json.dumps(data) if data is not None else ""
    if data is None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = data
    r.request = SimpleNamespace(method="POST")
    return r


@pytest.fixture
def op(tmp_path, monkeypatch):
    monkeypatch.setattr(web_login.time, "sleep", lambda s: None)
    tool = MagicMock()
    tool.config_path = tmp_path
    tool._cookie_value.return_value = "xsrf123"
    tool.parse_redirect_config.return_value = {"loginTrustFlagsSecret": PUBKEY}
    tool.api_client.oauth_client.client_id = "CID"
    tool.api_client.oauth_client.resolve_url = lambda u: "https://hh.ru/oauth" + u
    tool.api_client.oauth_client.authenticate.return_value = {"access_token": "USERnew"}
    operation = Operation()
    operation.tool = tool
    return operation


def test_trust_flags_are_encrypted_with_page_key():
    flags = web_login.trust_flags(PUBKEY, {"emailOrPhone": {"paste": False}})
    plain = PRIVATE_KEY.decrypt(
        base64.b64decode(flags),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    data = json.loads(plain)
    assert data["emailOrPhone"] == {"paste": False} and data["ts"] > 0


def test_phone_is_normalized():
    assert web_login.normalize_phone("8 (999) 123-45-67") == "79991234567"


def test_full_login_with_captcha(op, tmp_path):
    session = op.tool.session
    session.get.return_value = response(url=LOGIN_PAGE)
    # Сначала hh просит капчу, после ответа — высылает код
    session.post.side_effect = [
        response(data={"hhcaptcha": {"captchaState": "ST"}}),
        response(data={"key": "K1"}),
        response(data={"success": True}),
    ]
    session.get.side_effect = [
        response(url=LOGIN_PAGE),
        response(content=b"png"),
    ]
    assert op.request_code("+7 999 123-45-67") == web_login.CAPTCHA_EXIT_CODE
    assert (tmp_path / web_login.IMAGE_FILENAME).read_bytes() == b"png"
    otp = session.post.call_args_list[0]
    assert otp.args[0] == "https://hh.ru/account/otp_generate"
    assert otp.kwargs["data"]["login"] == "79991234567"
    assert otp.kwargs["headers"]["X-XSRFToken"] == "xsrf123"

    assert op.answer_captcha(" abc ") == 0
    sent = session.post.call_args.kwargs["data"]
    assert (sent["captchaState"], sent["captchaText"], sent["captchaKey"]) == ("ST", "abc", "K1")
    assert web_login.load_pending(tmp_path)["stage"] == "code"

    session.post.side_effect = [
        response(data={"success": True, "verification": {"success": True}}),
    ]
    session.get.side_effect = [
        response(status=302, headers={"location": "hhandroid://oauthresponse?code=AUTH"}),
    ]
    assert op.confirm_code("12 34") == 0
    assert session.post.call_args.kwargs["data"]["code"] == "1234"
    op.tool.api_client.oauth_client.authenticate.assert_called_once_with("AUTH")
    op.tool.api_client.handle_access_token.assert_called_once_with({"access_token": "USERnew"})
    assert web_login.load_pending(tmp_path) is None


def test_wrong_code_keeps_step_for_retry(op, tmp_path):
    session = op.tool.session
    session.get.return_value = response(url=LOGIN_PAGE)
    session.post.side_effect = [response(data={"success": True})]
    assert op.request_code("me@example.com") == 0
    assert parse_qs(urlsplit(session.get.call_args_list[0].args[0]).query)["role"] == ["applicant"]

    session.post.side_effect = [response(data={"success": False, "error": {"key": "WRONG_CODE"}})]
    with pytest.raises(web_login.LoginError, match="WRONG_CODE"):
        op.confirm_code("0000")
    assert web_login.load_pending(tmp_path)["stage"] == "code"


def test_code_without_request_is_rejected(op):
    with pytest.raises(web_login.LoginError, match="не начат"):
        op.confirm_code("1234")


def test_already_logged_in_site_gives_token_at_once(op):
    op.tool.session.get.side_effect = [
        response(status=302, headers={"location": "/oauth/authorize?step=2"}),
        response(status=302, headers={"location": "hhandroid://oauthresponse?code=FAST"}),
    ]
    assert op.request_code("me@example.com") == 0
    op.tool.api_client.oauth_client.authenticate.assert_called_once_with("FAST")
    assert not op.tool.session.post.called


def test_token_error_after_code_asks_to_start_over(op, tmp_path):
    session = op.tool.session
    session.get.return_value = response(url=LOGIN_PAGE)
    session.post.side_effect = [response(data={"success": True})]
    op.request_code("me@example.com")
    session.post.side_effect = [response(data={"success": True, "verification": {"success": True}})]
    session.get.side_effect = None
    session.get.return_value = response(status=302, headers={"location": "hhandroid://x?code=C"})
    op.tool.api_client.oauth_client.authenticate.side_effect = RuntimeError("boom")
    with pytest.raises(web_login.LoginError, match="заново"):
        op.confirm_code("1234")
    assert web_login.load_pending(tmp_path) is None
