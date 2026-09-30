from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hh_applicant_tool.utils import hhcaptcha

CAPTCHA_URL = "https://hh.ru/account/captcha?state=abc"


def make_tool(tmp_path, *, submit_status=302):
    session = MagicMock()
    posts = []

    def post(url, params=None, headers=None, allow_redirects=True):
        posts.append((url, params))
        if url == "https://hh.ru/captcha":
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"key": f"key-{len(posts)}"},
            )
        return SimpleNamespace(status_code=submit_status)

    session.post.side_effect = post
    session.get.side_effect = lambda url, params=None, headers=None: SimpleNamespace(
        raise_for_status=lambda: None, content=b"PNG", text=""
    )
    tool = SimpleNamespace(
        session=session,
        config_path=tmp_path,
        xsrf_token="x",
        parse_redirect_config=lambda page, check_auth=True: {
            "hhcaptcha": {"captchaState": "STATE"},
            "captchaAccountState": {"backurl": "/", "failurl": CAPTCHA_URL},
        },
    )
    return tool, posts


def test_prepare_saves_image_and_state(tmp_path):
    tool, posts = make_tool(tmp_path)
    path = hhcaptcha.prepare(tool, CAPTCHA_URL)
    assert path.read_bytes() == b"PNG"
    info = hhcaptcha.load_pending(tmp_path)
    assert info["captcha_state"] == "STATE" and info["key"] == "key-1"
    assert posts[0] == ("https://hh.ru/captcha", {"lang": "RU"})


def test_correct_answer_is_sent_and_clears_state(tmp_path):
    tool, posts = make_tool(tmp_path)
    hhcaptcha.prepare(tool, CAPTCHA_URL)
    assert hhcaptcha.submit(tool, " хатку кропил ")
    url, params = posts[-1]
    assert url == "https://hh.ru/account/captcha"
    assert params == {
        "captchaText": "хатку кропил",
        "captchaKey": "key-1",
        "captchaState": "STATE",
        "backurl": "/",
        "failurl": CAPTCHA_URL,
    }
    assert hhcaptcha.load_pending(tmp_path) is None
    assert not hhcaptcha.image_path(tmp_path).exists()


def test_wrong_answer_prepares_new_image(tmp_path):
    tool, posts = make_tool(tmp_path, submit_status=403)
    hhcaptcha.prepare(tool, CAPTCHA_URL)
    assert not hhcaptcha.submit(tool, "неверно")
    assert hhcaptcha.load_pending(tmp_path)["key"] == "key-3"


def test_submit_without_pending_captcha(tmp_path):
    tool, _ = make_tool(tmp_path)
    with pytest.raises(hhcaptcha.CaptchaError):
        hhcaptcha.submit(tool, "текст")
