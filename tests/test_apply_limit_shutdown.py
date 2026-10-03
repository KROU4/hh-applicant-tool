"""Deterministic tests for two apply fixes:

1. --max-responses is actually enforced in the apply loop.
2. Ctrl+C (SIGINT) triggers graceful shutdown, not a raw traceback.

These tests avoid live hh.ru credentials — they mock the network/API layer,
so they run in any environment (the real /me and /resumes/mine calls return
403 here because the HH token is server-rejected, unrelated to the code).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from hh_applicant_tool.operations.apply_vacancies import Operation


def _make_vacancy(i: int) -> dict:
    return {
        "id": str(i),
        "name": f"Vacancy {i}",
        "alternate_url": f"https://hh.ru/vacancy/{i}",
        "employer": {},  # no employer id -> no profile fetch
        "snippet": {},
    }


def _make_operation(max_responses: int = 5) -> Operation:
    op = Operation()
    op.vacancy_fetch_delay = (0.0, 0.0)
    # Namespace-like args used by _apply_resume.
    # NOTE: `args` is a read-only property backed by `_args` (set in run()).
    op._args = SimpleNamespace(
        skip_tests=False,
        send_email=False,
        letter_file=None,
        use_ai=False,
        system_prompt="",
        ai_rate_limit=0,
    )
    op.max_responses = max_responses
    op.dry_run = False
    op.ai_filter = None
    op.vacancy_filter_ai = None
    op.excluded_filter = None
    op.cover_letter = "hello"
    op.force_message = False
    op.send_email = False
    op.json_decoder = __import__(
        "hh_applicant_tool.utils.json",
        fromlist=["JSONDecoder"],
    ).JSONDecoder()

    # Mocked tool with a storage that no-ops saves
    tool = MagicMock()
    tool.storage.vacancies.save.return_value = None
    tool.storage.vacancy_contacts.save.return_value = None
    tool.storage.employers.save.return_value = None
    tool.storage.skipped_vacancies.find.return_value = []
    tool.storage.negotiations.save.return_value = None
    op.tool = tool

    # Each post returns empty dict -> assert res == {} passes, applied_count++.
    # `api_client` is a read-only property returning `tool.api_client`.
    op.tool.api_client = MagicMock()
    op.tool.api_client.post.return_value = {}
    return op


class TestMaxResponses:
    def test_loop_stops_after_max_responses(self):
        """With --max-responses 5 and 20 vacancies, only 5 are applied."""
        op = _make_operation(max_responses=5)
        total = 20
        op._get_vacancies = lambda resume_id=None: iter(
            _make_vacancy(i) for i in range(total)
        )

        resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
        op._apply_resume(resume=resume, user=user, seen_employers=set())

        assert op.tool.api_client.post.call_count == 5

    def test_no_limit_means_no_early_stop(self):
        """Without max-responses, all vacancies are attempted."""
        op = _make_operation(max_responses=0)
        total = 8
        op._get_vacancies = lambda resume_id=None: iter(
            _make_vacancy(i) for i in range(total)
        )

        resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
        op._apply_resume(resume=resume, user=user, seen_employers=set())

        assert op.tool.api_client.post.call_count == total


class TestGracefulShutdown:
    def test_cancel_event_stops_loop_between_vacancies(self):
        """Setting _cancel_event gracefully halts the loop (UI + new CLI path)."""
        op = _make_operation(max_responses=0)
        cancel_event = threading.Event()
        op._cancel_event = cancel_event

        applied = []

        def fake_get(resume_id=None):
            for i in range(20):
                applied.append(i)
                if i == 2:
                    cancel_event.set()
                yield _make_vacancy(i)

        op._get_vacancies = fake_get
        resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
        op._apply_resume(resume=resume, user=user, seen_employers=set())

        # The cancel event yields the first 3, then the loop must break
        assert len(applied) == 3
        assert op.tool.api_client.post.call_count <= 3


class TestMaxResponsesAcrossResumes:
    def test_limit_is_shared_by_all_resumes(self):
        """--max-responses 5 при двух резюме — всего 5 откликов, а не 10."""
        op = _make_operation(max_responses=5)
        op._get_vacancies = lambda resume_id=None: iter(
            _make_vacancy(f"{resume_id}-{i}") for i in range(20)
        )
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}

        for rid in ("r1", "r2"):
            resume = {"id": rid, "title": "Dev", "alternate_url": "u"}
            op._apply_resume(resume=resume, user=user, seen_employers=set())

        assert op.tool.api_client.post.call_count == 5
        assert op.total_applied == 5


class TestCoverLetterPrompt:
    def test_prompt_has_vacancy_and_resume_but_no_contacts(self):
        op = _make_operation()
        op.message_prompt = "Напиши письмо."
        op.letter_contact = "https://t.me/krou4"
        op._resume_analysis_cache = {}
        op.api_client.get.side_effect = lambda url, *a, **k: (
            {
                "description": "<p>Строить <b>RAG</b> и AI-агентов</p>",
                "key_skills": [{"name": "LLM"}, {"name": "Python"}],
            }
            if url.startswith("/vacancies/")
            else {
                "title": "Senior AI Engineer",
                "skill_set": ["RAG", "FastAPI"],
                "experience": [
                    {"company": "Хэппи ИИ", "position": "AI Engineer",
                     "start": "2024-01-01", "description": "LLM-оркестрация"}
                ],
            }
        )
        placeholders = {
            "vacancy_name": "AI Engineer", "employer_name": "Лайфтех",
            "resume_title": "Senior AI Engineer", "resume_url": "https://hh.ru/resume/x",
            "first_name": "Дмитрий", "last_name": "В", "phone": "375000", "email": "a@b.c",
        }
        prompt = op._build_cover_letter_prompt(
            {"id": "1", "name": "AI Engineer"}, {"id": "r1"}, placeholders
        )
        assert "Строить RAG и AI-агентов" in prompt
        assert "Ключевые навыки: LLM, Python" in prompt
        assert "Хэппи ИИ" in prompt and "LLM-оркестрация" in prompt
        assert "https://t.me/krou4" in prompt
        for secret in ("375000", "a@b.c", "https://hh.ru/resume/x"):
            assert secret not in prompt

    def test_contact_is_appended_when_model_forgets_it(self):
        op = _make_operation()
        op.letter_contact = "https://t.me/krou4"
        assert op._finalize_letter("Добрый день! Текст.").endswith(
            "Жду обратной связи в Telegram: https://t.me/krou4"
        )
        kept = "Добрый день! Пишите: https://t.me/krou4"
        assert op._finalize_letter(kept) == kept


class TestIncludedFilter:
    def test_vacancies_without_keywords_are_skipped_not_blacklisted(self):
        op = _make_operation(max_responses=0)
        op.included_filter = r"llm|rag"
        vacancies = [
            {**_make_vacancy(1), "name": "LLM Engineer"},
            {**_make_vacancy(2), "name": "Java QA"},
            {**_make_vacancy(3), "name": "Python dev"},
        ]
        descriptions = {
            "/vacancies/2": {"description": "<p>Тестирование на Java</p>"},
            "/vacancies/3": {"description": "<p>Строим <b>RAG</b></p>"},
        }
        op.tool.api_client.get.side_effect = lambda url, *a, **k: descriptions.get(url, {})
        op._get_vacancies = lambda resume_id=None: iter(vacancies)

        resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
        op._apply_resume(resume=resume, user=user, seen_employers=set())

        applied = [c.args[0] for c in op.tool.api_client.post.call_args_list]
        assert len(applied) == 2
        # Без совпадения — просто пропуск, не чёрный список hh
        assert not op.tool.api_client.put.called


class TestDryRun:
    def test_dry_run_prints_letters_and_respects_limit(self, capsys):
        op = _make_operation(max_responses=2)
        op.dry_run = True
        op.force_message = True
        op.cover_letter_ai = None
        op._get_vacancies = lambda resume_id=None: iter(
            _make_vacancy(i) for i in range(10)
        )
        resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
        op._apply_resume(resume=resume, user=user, seen_employers=set())

        out = capsys.readouterr().out
        assert out.count("🧪 Тест: откликнулся бы на") == 2
        assert "--- письмо ---" in out and "=== конец ===" in out
        assert not op.tool.api_client.post.called


def test_finalize_letter_strips_markdown():
    op = _make_operation()
    op.letter_contact = ""
    text = "## Опыт\n**LLM в проде.** Делал __RAG__.\n* пункт"
    assert op._finalize_letter(text) == "Опыт\nLLM в проде. Делал RAG.\n* пункт"


class TestExcludedAndNetworkErrors:
    def test_excluded_uses_api_and_network_error_skips_only_one_vacancy(self):
        import requests

        op = _make_operation(max_responses=0)
        op.excluded_filter = r"php"

        def api_get(url, *a, **k):
            if url == "/vacancies/2":
                return {"description": "<p>Нужен PHP</p>"}
            if url == "/vacancies/3":
                raise requests.ConnectionError("сеть упала")
            return {"description": "Python"}

        op.tool.api_client.get.side_effect = api_get
        op._get_vacancies = lambda resume_id=None: iter(_make_vacancy(i) for i in range(1, 5))

        resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
        op._apply_resume(resume=resume, user=user, seen_employers=set())

        # 1 и 4 — отклик; 2 — стоп-слово из API; 3 — сетевая ошибка, но прогон жив
        assert op.tool.api_client.post.call_count == 2
        assert not op.tool.session.get.called


class TestCaptchaPause:
    def test_captcha_pauses_run_without_marking_vacancies_skipped(self, capsys):
        from hh_applicant_tool.api.errors import CaptchaRequired

        op = _make_operation(max_responses=0)
        op.included_filter = r"llm"

        def api_get(url, *a, **k):
            if url == "/vacancies/2":
                raise CaptchaRequired(MagicMock(), {"errors": [{"value": "captcha_required", "captcha_url": "https://hh.ru/account/captcha"}]})
            return {"description": "LLM"}

        op.tool.api_client.get.side_effect = api_get
        op._get_vacancies = lambda resume_id=None: iter(_make_vacancy(i) for i in range(1, 6))
        resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
        user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
        op._apply_resume(resume=resume, user=user, seen_employers=set())

        assert op.captcha_paused
        assert op.tool.api_client.post.call_count == 1  # только вакансия 1
        assert not op.tool.storage.skipped_vacancies.save.called
        assert "hh запросил капчу" in capsys.readouterr().out

    def test_vacancy_is_fetched_once_per_run(self):
        op = _make_operation(max_responses=0)
        op.included_filter = r"llm"
        op.excluded_filter = r"php"
        op.tool.api_client.get.return_value = {"description": "LLM"}
        assert op._is_included(_make_vacancy(7))
        assert not op._is_excluded(_make_vacancy(7))
        urls = [c.args[0] for c in op.tool.api_client.get.call_args_list]
        assert urls.count("/vacancies/7") == 1


def test_vacancy_is_dropped_from_cache_after_processing():
    op = _make_operation(max_responses=0)
    op.included_filter = r"llm"
    op.tool.api_client.get.return_value = {"description": "LLM"}
    op._get_vacancies = lambda resume_id=None: iter(_make_vacancy(i) for i in range(1, 4))
    resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
    user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
    op._apply_resume(resume=resume, user=user, seen_employers=set())
    assert op.tool.api_client.post.call_count == 3
    assert op.__dict__.get("_vacancy_cache", {}) == {}


def test_title_search_checks_keywords_without_opening_vacancy():
    op = _make_operation(max_responses=0)
    op.included_filter = r"llm"
    op.search_field = ["name"]
    assert op._is_included({**_make_vacancy(1), "name": "LLM Engineer"})
    assert not op._is_included({**_make_vacancy(2), "name": "Менеджер по продажам"})
    assert not op.tool.api_client.get.called



def test_sent_responses_are_timestamped(tmp_path):
    op = _make_operation(max_responses=3)
    op.tool.config_path = tmp_path
    op._get_vacancies = lambda resume_id=None: iter(_make_vacancy(i) for i in range(10))
    resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
    user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
    op._apply_resume(resume=resume, user=user, seen_employers=set())
    assert len((tmp_path / "sent_times.txt").read_text().split()) == 3


def test_vacancy_sources_priority_and_dedup():
    op = _make_operation(max_responses=0)
    op.search = "llm"
    op.search_field = ["name"]
    op.area = None
    op.recommended_first = True
    op.priority_area = ["16"]
    op.total_pages = 1
    calls = []

    def api_get(url, params, *a, **k):
        calls.append((url, params))
        if "similar_vacancies" in url:
            items = [{"id": "1"}, {"id": "2"}]
        elif params.get("area") == ["16"]:
            items = [{"id": "2"}, {"id": "3"}]
        else:
            items = [{"id": "3"}, {"id": "4"}]
        return {"items": items, "found": len(items), "pages": 1}

    op.tool.api_client.get.side_effect = api_get
    op._get_search_params = lambda page: {"page": page, "text": "llm", "search_field": ["name"]}
    ids = [v["id"] for v in op._get_vacancies(resume_id="r1")]

    assert ids == ["1", "2", "3", "4"]
    assert calls[0][0] == "/resumes/r1/similar_vacancies"
    assert "text" not in calls[0][1] and "search_field" not in calls[0][1]
    assert calls[1][1]["area"] == ["16"] and calls[1][1]["text"] == "llm"
    assert "area" not in calls[2][1]
