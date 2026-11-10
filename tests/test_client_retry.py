from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from hh_applicant_tool.api import client


def make_client(monkeypatch, responses):
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    session = MagicMock()
    session.request.side_effect = responses
    return client.BaseClient(
        "https://api.hh.ru/",
        session=session,
        delay=0,
        jitter=0,
        max_retries=3,
    )


def response(status: int, data=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.text = "{}"
    r.json.return_value = data or {}
    r.headers = headers or {}
    return r


def test_retries_transient_status(monkeypatch):
    c = make_client(monkeypatch, [response(429), response(200, {"ok": 1})])
    assert c.get("/me") == {"ok": 1}
    assert c.session.request.call_count == 2


def test_retries_connection_error(monkeypatch):
    c = make_client(
        monkeypatch, [requests.ConnectionError("boom"), response(200, {"ok": 1})]
    )
    assert c.get("/me") == {"ok": 1}
    assert c.session.request.call_count == 2


def test_gives_up_after_retries(monkeypatch):
    c = make_client(monkeypatch, [requests.ConnectionError("boom")] * 3)
    with pytest.raises(requests.ConnectionError):
        c.get("/me")
    assert c.session.request.call_count == 3


def test_uses_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    session = MagicMock()
    session.request.side_effect = [response(503, headers={"Retry-After": "7"}), response(200)]
    c = client.BaseClient("https://api.hh.ru/", session=session, delay=0, jitter=0)
    c.get("/me")
    assert sleeps and sleeps[0] >= 7


def test_honours_non_retriable_error(monkeypatch):
    c = make_client(monkeypatch, [response(404, {"errors": []})])
    with pytest.raises(client.errors.ResourceNotFound):
        c.get("/me")
    assert c.session.request.call_count == 1


CAPTCHA_DATA = {"errors": [{"value": "captcha_required", "captcha_url": "u"}]}


def make_api_client(monkeypatch, responses, handler):
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    session = MagicMock()
    session.request.side_effect = responses
    return client.ApiClient(
        base_url="https://api.hh.ru/",
        session=session,
        delay=0,
        jitter=0,
        access_token="USER1",
        captcha_handler=handler,
    )


def test_captcha_handler_retries_request(monkeypatch):
    seen = []

    def handler(url):
        seen.append(url)
        return True

    c = make_api_client(
        monkeypatch, [response(403, CAPTCHA_DATA), response(200, {"ok": 1})], handler
    )
    assert c.get("/vacancies/1") == {"ok": 1}
    assert seen == ["u"]


def test_captcha_handler_gives_up(monkeypatch):
    c = make_api_client(
        monkeypatch, [response(403, CAPTCHA_DATA)], lambda url: False
    )
    with pytest.raises(client.errors.CaptchaRequired):
        c.get("/vacancies/1")


def test_captcha_handler_stops_after_limit(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    session = MagicMock()
    session.request.side_effect = [response(403, CAPTCHA_DATA)] * 5
    c = client.ApiClient(
        base_url="https://api.hh.ru/",
        session=session,
        delay=0,
        jitter=0,
        access_token="USER1",
        captcha_handler=lambda url: True,
        max_captcha_attempts=2,
    )
    with pytest.raises(client.errors.CaptchaRequired):
        c.get("/vacancies/1")
    assert session.request.call_count == 3

