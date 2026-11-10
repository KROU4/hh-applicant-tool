from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hh_applicant_tool.utils import hhcaptcha

CAPTCHA_URL = "https://hh.ru/account/captcha?state=abc"


def make_tool(tmp_path, *, submit_status=302, location="/"):
    session = MagicMock()
    posts = []

    def post(url, params=None, headers=None, allow_redirects=True):
        posts.append((url, params))
        if url == "https://hh.ru/captcha":
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"key": f"key-{len(posts)}"},
            )
        return SimpleNamespace(status_code=submit_status, headers={"Location": location})

    session.post.side_effect = post
    session.get.side_effect = lambda url, params=None, headers=None: SimpleNamespace(
        raise_for_status=lambda: None, content=b"PNG", text=""
    )
    tool = SimpleNamespace(
        session=session,
        config_path=tmp_path,
        xsrf_token="x",
        config={
            "openrouter": {"api_key": "sk-or-test"},
            "captcha": {"solve_delay": [0, 0]},
        },
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


def test_redirect_back_to_captcha_means_wrong_answer(tmp_path):
    tool, _ = make_tool(tmp_path, location="/account/captcha?state=abc")
    hhcaptcha.prepare(tool, CAPTCHA_URL)
    assert not hhcaptcha.submit(tool, "неверно")
    assert hhcaptcha.load_pending(tmp_path) is not None


def test_solve_with_ai_retries_after_wrong_answer(tmp_path):
    tool, posts = make_tool(tmp_path)
    # Первый ответ — редирект обратно на капчу, второй принят
    answers = iter([("/captcha", 302), ("/", 302)])

    def post(url, params=None, headers=None, allow_redirects=True):
        posts.append((url, params))
        if url == "https://hh.ru/captcha":
            return SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: {"key": f"key-{len(posts)}"}
            )
        location, status = next(answers)
        return SimpleNamespace(status_code=status, headers={"Location": location})

    tool.session.post.side_effect = post
    ai = MagicMock()
    ai.solve_captcha.side_effect = ["abc12", "x y9"]
    tool.get_captcha_ai = lambda: ai

    assert hhcaptcha.solve_with_ai(tool, CAPTCHA_URL, pause=(0, 0)) is True
    assert ai.solve_captcha.call_count == 2
    submitted = [p["captchaText"] for u, p in posts if u.endswith("/account/captcha")]
    assert submitted == ["abc12", "x y9"]
    assert hhcaptcha.load_pending(tmp_path) is None


def test_solve_with_ai_gives_up_without_ai_or_after_attempts(tmp_path):
    tool, _ = make_tool(tmp_path, location="/captcha")

    def no_ai():
        raise ValueError("нет ключа")

    tool.get_captcha_ai = no_ai
    assert hhcaptcha.solve_with_ai(tool, CAPTCHA_URL, pause=(0, 0)) is False

    ai = MagicMock()
    ai.solve_captcha.return_value = "zzz"
    tool.get_captcha_ai = lambda: ai
    assert hhcaptcha.solve_with_ai(tool, CAPTCHA_URL, attempts=2, pause=(0, 0)) is False
    assert ai.solve_captcha.call_count == 2


def test_solve_uses_capsolver_after_openrouter_failure(tmp_path):
    tool, _ = make_tool(tmp_path, location="/")
    tool.config["capsolver"] = {"api_key": "CAP-test"}
    ai = MagicMock()
    ai.solve_captcha.side_effect = ValueError("модель недоступна")
    tool.get_captcha_ai = lambda: ai

    capsolver = MagicMock()
    capsolver.solve_image.return_value = "верный ответ"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(hhcaptcha, "capsolver", capsolver)
    try:
        assert hhcaptcha.solve_with_ai(tool, CAPTCHA_URL, pause=(0, 0)) is True
    finally:
        monkeypatch.undo()
    capsolver.solve_image.assert_called_once()


def test_solve_skips_unconfigured_providers(tmp_path):
    tool, _ = make_tool(tmp_path)
    tool.config = {}
    tool.get_captcha_ai = MagicMock()
    assert hhcaptcha.solve_with_ai(tool, CAPTCHA_URL, pause=(0, 0)) is False
    tool.get_captcha_ai.assert_not_called()
