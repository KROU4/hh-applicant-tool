from __future__ import annotations

from typing import Any

import pytest

from hh_applicant_tool.ai.openrouter import (
    STATE,
    ChatOpenRouter,
    OpenRouterError,
)


class FakeResponse:
    def __init__(self, status: int, data: dict[str, Any], headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._data

    def raise_for_status(self) -> None:
        pass


def ok(text: str) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def err(code: int, message: str, raw: str = "") -> FakeResponse:
    return FakeResponse(
        code,
        {"error": {"code": code, "message": message, "metadata": {"raw": raw}}},
    )


class FakeSession:
    def __init__(self, replies: dict[str, list[FakeResponse]], catalog=None):
        self.replies = replies
        self.calls: list[str] = []
        self.catalog = catalog

    def get(self, url, timeout=None):
        if self.catalog is None:
            raise ValueError("no catalog")
        return FakeResponse(200, {"data": [{"id": m} for m in self.catalog]})

    def post(self, url, json=None, headers=None, timeout=None):
        model = json["model"]
        self.calls.append(model)
        return self.replies[model].pop(0)


@pytest.fixture(autouse=True)
def reset_state():
    STATE.reset()
    yield
    STATE.reset()


def make(session: FakeSession, models: list[str]) -> ChatOpenRouter:
    return ChatOpenRouter(
        "key",
        models=models,
        session=session,
        rate_limit=0,
        max_wait=0,
    )


def test_falls_back_on_rate_limit_and_remembers_cooldown():
    session = FakeSession(
        {
            "a:free": [err(429, "Provider returned error", "rate-limited upstream")],
            "b:free": [ok("письмо 1"), ok("письмо 2")],
        },
        catalog=["a:free", "b:free"],
    )
    client = make(session, ["a:free", "b:free"])

    assert client.complete("привет") == "письмо 1"
    # Модель a на остывании — второй запрос сразу идёт в b
    assert client.complete("привет") == "письмо 2"
    assert session.calls == ["a:free", "b:free", "b:free"]


def test_disables_forbidden_model_and_skips_empty_answer():
    session = FakeSession(
        {
            "a:free": [err(403, "only available on agentic harnesses")],
            "b:free": [ok("<think>размышления</think>   ")],
            "c:free": [ok("<think>x</think>Готово")],
        }
    )
    client = make(session, ["a:free", "b:free", "c:free"])

    assert client.complete("привет") == "Готово"
    assert "a:free" in STATE.disabled
    assert STATE.cooldown_until.get("b:free")


def test_skips_models_missing_from_catalog():
    session = FakeSession(
        {"b:free": [ok("ok")]},
        catalog=["b:free"],
    )
    client = make(session, ["gone:free", "b:free"])

    assert client.complete("привет") == "ok"
    assert session.calls == ["b:free"]


def test_daily_limit_stops_immediately():
    session = FakeSession(
        {
            "a:free": [err(429, "Rate limit exceeded: free-models-per-day")],
            "b:free": [ok("не должно дойти")],
        }
    )
    client = make(session, ["a:free", "b:free"])

    with pytest.raises(OpenRouterError, match="Дневной лимит"):
        client.complete("привет")
    assert session.calls == ["a:free"]


def test_raises_when_all_models_busy():
    session = FakeSession(
        {
            "a:free": [err(429, "busy")],
            "b:free": [err(503, "down")],
        }
    )
    client = make(session, ["a:free", "b:free"])

    with pytest.raises(OpenRouterError, match="заняты"):
        client.complete("привет")


def test_invalid_key_is_not_retried():
    session = FakeSession({"a:free": [err(401, "No auth credentials found")]})
    client = make(session, ["a:free", "b:free"])

    with pytest.raises(OpenRouterError, match="ключ"):
        client.complete("привет")
    assert session.calls == ["a:free"]


def test_tool_uses_openrouter_section(tmp_path):
    from hh_applicant_tool.main import HHApplicantTool
    from hh_applicant_tool.utils import Config

    tool = HHApplicantTool()
    tool.openai_timeout = None
    tool.openai_connect_timeout = None
    tool.openai_proxy_url = None
    tool.proxy_url = None
    tool.__dict__["config"] = Config(tmp_path / "config.json")
    tool.config.save(
        openrouter={"api_key": "sk-or-test", "models": ["x:free"]}
    )

    client = tool.get_cover_letter_ai("system")

    assert isinstance(client, ChatOpenRouter)
    assert client.models == ["x:free"]
    assert client.system_prompt == "system"


def test_slow_failures_stop_after_deadline():
    # Модели падают дольше, чем остывают: без проверки срока цикл вечный
    session = FakeSession(
        {
            "a:free": [err(503, "down")] * 10,
            "b:free": [err(503, "down")] * 10,
        }
    )
    client = make(session, ["a:free", "b:free"])
    STATE.cooldown_until.clear()

    import hh_applicant_tool.ai.openrouter as orouter

    original = orouter.COOLDOWN_ERROR
    orouter.COOLDOWN_ERROR = 0.0
    try:
        with pytest.raises(OpenRouterError):
            client.complete("привет")
    finally:
        orouter.COOLDOWN_ERROR = original
    # Один полный круг по моделям, дальше — ошибка, а не бесконечный цикл
    assert session.calls == ["a:free", "b:free"]


def test_bad_request_cools_down_but_404_disables():
    session = FakeSession(
        {
            "a:free": [err(400, "context length exceeded")],
            "b:free": [err(404, "No endpoints found")],
            "c:free": [ok("ok")],
        }
    )
    client = make(session, ["a:free", "b:free", "c:free"])

    assert client.complete("привет") == "ok"
    assert "a:free" not in STATE.disabled
    assert STATE.cooldown_until["a:free"] > 0
    assert "b:free" in STATE.disabled


def test_daily_limit_is_distinct_error():
    from hh_applicant_tool.ai import OpenRouterDailyLimit

    session = FakeSession(
        {"a:free": [err(429, "Rate limit exceeded: free-models-per-day")]}
    )
    with pytest.raises(OpenRouterDailyLimit):
        make(session, ["a:free"]).complete("привет")
    # Повторный вызов даже не ходит в сеть
    with pytest.raises(OpenRouterDailyLimit):
        make(session, ["a:free"]).complete("привет")
    assert session.calls == ["a:free"]


def test_unclosed_think_is_stripped():
    session = FakeSession(
        {
            "a:free": [ok("<think>обрезано на полуслове")],
            "b:free": [ok("Письмо")],
        }
    )
    assert make(session, ["a:free", "b:free"]).complete("x") == "Письмо"


def test_empty_openai_section_key_does_not_hide_openrouter(tmp_path):
    from hh_applicant_tool.main import HHApplicantTool
    from hh_applicant_tool.utils import Config

    tool = HHApplicantTool()
    tool.openai_timeout = None
    tool.openai_connect_timeout = None
    tool.openai_proxy_url = None
    tool.proxy_url = None
    tool.__dict__["config"] = Config(tmp_path / "config.json")
    tool.config.save(
        openrouter={"api_key": "sk-or-test"},
        openai_cover_letter={"api_key": "", "rate_limit": 40, "model": "gpt-4o"},
    )

    client = tool.get_cover_letter_ai("system")

    assert isinstance(client, ChatOpenRouter)
    assert client.api_key == "sk-or-test"
    assert client.rate_limit == 20
