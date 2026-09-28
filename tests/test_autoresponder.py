"""Автоответчик на структуре ответа chatik.hh.ru (снята с реального ответа)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from hh_applicant_tool.operations.autoresponder import Operation

ME = "50078502"
EMPLOYER = "158454321"
RESUMES = [
    {"id": "hash-lead", "real_id": "111", "title": "Lead AI Engineer"},
    {"id": "hash-senior", "real_id": "283740897", "title": "Senior AI Engineer"},
]
VACANCIES = {
    "137838539": {
        "vacancyId": 137838539,
        "name": "AI / ML-инженер",
        "company": {"name": "Смарт СТиМ Сити", "visibleName": "Смарт СТиМ Сити"},
        "compensation": {"from": 4500, "currencyCode": "BYR"},
        "links": {"desktop": "https://hh.ru/vacancy/137838539"},
    }
}


def iso(delta_hours: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=delta_hours)).isoformat()


def chat_item(chat_id, *, author=EMPLOYER, text="Когда удобно созвониться?",
              hours_ago=1, state="INVITATION", is_bot=False):
    return {
        "id": chat_id,
        "type": "NEGOTIATION",
        "resources": {"VACANCY": ["137838539"], "RESUME": ["283740897"]},
        "currentParticipantId": ME,
        "lastMessage": {
            "creationTime": iso(hours_ago),
            "text": text,
            "participantId": author,
            "participantDisplay": {"name": "Анна HR", "isBot": is_bot},
            "workflowTransition": {"applicantState": state},
        },
    }


def make_operation(**args) -> Operation:
    op = Operation()
    op.tool = MagicMock()
    op.tool.get_resumes.return_value = RESUMES
    op.tool.xsrf_token = "x"
    defaults = dict(
        delete=False, max_pages=5, contact="https://t.me/krou4",
        dry_run=False, once=True, interval=60,
    )
    op.args = SimpleNamespace(**{**defaults, **args})
    op._full_resumes = {}
    op.__dict__["chatik_url"] = "https://chatik.hh.ru"
    return op


def test_picks_employer_messages_and_resume_by_real_id():
    op = make_operation()
    items = [
        chat_item(1),
        chat_item(2, author=ME, text="Здравствуйте!"),  # последнее — моё
        chat_item(3, text=""),  # служебное событие без текста
        chat_item(4, text="", state="DISCARD"),  # отказ
    ]
    op._get = MagicMock(return_value={
        "chats": {"items": items, "nextFrom": None},
        "resources": {"vacancies": VACANCIES},
    })

    chats = op.get_chats_awaiting_reply()

    assert [c.chat_id for c in chats] == [1, 4]
    assert chats[0].resume["id"] == "hash-senior"
    assert chats[0].company_name == "Смарт СТиМ Сити"
    assert chats[0].vacancy_compensation == "от 4500 BYR"
    assert chats[1].is_discard


def test_stops_paging_on_old_chats():
    op = make_operation()
    op._get = MagicMock(return_value={
        "chats": {"items": [chat_item(1), chat_item(2, hours_ago=100)], "nextFrom": "c1"},
        "resources": {"vacancies": VACANCIES},
    })

    chats = op.get_chats_awaiting_reply()

    assert [c.chat_id for c in chats] == [1]
    assert op._get.call_count == 1


def test_reply_is_sent_with_contact_rules_and_bot_style():
    op = make_operation()
    chat = op.parse_chat_item(
        chat_item(7, is_bot=True), VACANCIES, {"283740897": RESUMES[1]}, RESUMES[0]
    )
    op._get = MagicMock(return_value={"chat": {
        "currentParticipantId": ME,
        "writePossibility": {"name": "ENABLED"},
        "messages": {"items": [
            {"participantId": ME, "text": "Добрый день!"},
            {"participantId": EMPLOYER, "text": "Когда удобно созвониться?",
             "participantDisplay": {"name": "Анна HR"}},
        ]},
    }})
    op._post = MagicMock(return_value={})
    ai = MagicMock()
    ai.complete.return_value = "Завтра после 14:00 удобно."
    op.tool.get_cover_letter_ai.return_value = ai
    op.tool.api_client.get.return_value = {
        "id": "hash-senior", "title": "Senior AI Engineer", "first_name": "Дмитрий",
        "skill_set": ["LLM", "RAG"], "experience": [],
    }

    op.reply_to_chat(chat)

    system_prompt = op.tool.get_cover_letter_ai.call_args.args[0]
    assert "https://t.me/krou4" in system_prompt
    assert "Никогда не выдумывай" in system_prompt
    user_prompt = ai.complete.call_args.args[0]
    assert "бота-рекрутера" in user_prompt
    assert "Я: Добрый день!" in user_prompt
    path, body, _ = op._post.call_args.args
    assert path == "/chatik/api/send"
    assert body["chatId"] == 7 and body["text"] == "Завтра после 14:00 удобно."


def test_dry_run_and_disabled_chats_send_nothing(capsys):
    op = make_operation(dry_run=True)
    chat = op.parse_chat_item(chat_item(8), VACANCIES, {}, RESUMES[0])
    op._post = MagicMock()

    op._get = MagicMock(return_value={"chat": {
        "writePossibility": {"name": "DISABLED_FOR_APPLICANT_BY_VACANCY_SETTINGS"},
    }})
    op.reply_to_chat(chat)
    op.tool.get_cover_letter_ai.assert_not_called()

    op._get = MagicMock(return_value={"chat": {
        "currentParticipantId": ME,
        "messages": {"items": [{"participantId": EMPLOYER, "text": "Привет"}]},
    }})
    op.tool.get_cover_letter_ai.return_value.complete.return_value = "Добрый день!"
    op.reply_to_chat(chat)

    op._post.assert_not_called()
    assert "не отправлен" in capsys.readouterr().out
