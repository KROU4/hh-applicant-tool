"""Автоответчик на структуре ответа chatik.hh.ru (снята с реального ответа)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    op.tool.config_path = Path(tempfile.mkdtemp())
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
    op.tool.api_client.get.return_value = RESUMES[0]
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


def test_human_decision_redirects_to_telegram_and_is_reported(capsys):
    op = make_operation()
    chat = op.parse_chat_item(chat_item(9), VACANCIES, {}, RESUMES[1])
    op._get = MagicMock(return_value={"chat": {
        "currentParticipantId": ME,
        "messages": {"items": [{"participantId": EMPLOYER, "text": "Готовы к офису 5/2 и какая зарплата?"}]},
    }})
    op._post = MagicMock(return_value={})
    op.tool.api_client.get.return_value = RESUMES[1]
    ai = MagicMock()
    ai.complete.return_value = "[НУЖЕН_ЧЕЛОВЕК] Добрый день! Эти детали лучше обсудить лично."
    op.tool.get_cover_letter_ai.return_value = ai

    op.reply_to_chat(chat)

    system_prompt = op.tool.get_cover_letter_ai.call_args.args[0]
    assert "никогда не признавай отсутствие опыта" in system_prompt
    assert "не давай обещаний от себя" in system_prompt
    sent = op._post.call_args.args[1]["text"]
    assert "[НУЖЕН_ЧЕЛОВЕК]" not in sent
    assert sent.endswith("Это удобнее обсудить в Telegram: https://t.me/krou4")
    out = capsys.readouterr().out
    assert out.startswith("🙋 Нужно ваше решение: «AI / ML-инженер» — Смарт СТиМ Сити")
    assert out.rstrip().endswith("=== конец ===")


def _robot_chat(op, items):
    op._get = MagicMock(return_value={"chat": {
        "currentParticipantId": ME,
        "writePossibility": {"name": "ENABLED_FOR_ALL_BY_EMPLOYER"},
        "messages": {"items": items},
    }})
    op._post = MagicMock(return_value={})
    op.tool.api_client.get.return_value = RESUMES[1]


QUESTION = {
    "participantId": "90236278",
    "text": "Есть ли у вас опыт работы с код-агентами?",
    "participantDisplay": {"isBot": True, "name": "Робот-рекрутер"},
    "actions": {"text_buttons": [{"size": "full", "text": "да"}, {"size": "full", "text": "нет"}]},
}


def test_robot_buttons_are_answered_with_button_text():
    op = make_operation()
    chat = op.parse_chat_item(chat_item(10, is_bot=True), VACANCIES, {}, RESUMES[1])
    _robot_chat(op, [QUESTION])
    op.tool.get_cover_letter_ai.return_value.complete.return_value = (
        "Да, есть. Проектировал агентные LLM-системы."
    )

    op.reply_to_chat(chat)

    assert op._post.call_args.args[1]["text"] == "да"
    prompt = op.tool.get_cover_letter_ai.return_value.complete.call_args.args[0]
    assert "- да" in prompt and "- нет" in prompt


def test_no_reply_marker_repeated_question_and_left_robot_skip_sending():
    op = make_operation()
    chat = op.parse_chat_item(chat_item(11), VACANCIES, {}, RESUMES[1])
    ai = op.tool.get_cover_letter_ai.return_value

    _robot_chat(op, [{"participantId": EMPLOYER, "text": "Спасибо! Ответы переданы работодателю."}])
    ai.complete.return_value = "[БЕЗ_ОТВЕТА]"
    op.reply_to_chat(chat)
    op._post.assert_not_called()

    _robot_chat(op, [QUESTION, {"participantId": ME, "text": "да"}] * 2 + [QUESTION])
    op.reply_to_chat(chat)
    op._post.assert_not_called()

    _robot_chat(op, [
        {"participantId": EMPLOYER, "text": "Спасибо!"},
        {"participantId": "90236278", "text": "", "type": "PARTICIPANT_LEFT"},
    ])
    op.reply_to_chat(chat)
    op._post.assert_not_called()


def test_pick_option():
    from hh_applicant_tool.operations.autoresponder import pick_option

    assert pick_option("Да, есть.", ["да", "нет"]) == "да"
    assert pick_option("нет", ["да", "нет"]) == "нет"
    assert pick_option("Скорее всего", ["Готов", "Не готов"]) == "Готов"


def test_human_button_question_waits_for_owner_once(capsys):
    op = make_operation()
    chat = op.parse_chat_item(chat_item(12, is_bot=True), VACANCIES, {}, RESUMES[1])
    office = {
        "id": 777,
        "participantId": "90236278",
        "text": "Готовы ли к офисному формату м. Курская?",
        "participantDisplay": {"isBot": True, "name": "Робот-рекрутер"},
        "actions": {"text_buttons": [{"text": "да"}, {"text": "нет"}]},
    }
    _robot_chat(op, [office])
    ai = op.tool.get_cover_letter_ai.return_value
    ai.complete.return_value = "[НУЖЕН_ЧЕЛОВЕК] Формат обсудим в Telegram"

    op.reply_to_chat(chat)
    op.reply_to_chat(chat)  # следующая проверка — сообщение уже разобрано

    op._post.assert_not_called()
    assert ai.complete.call_count == 1
    out = capsys.readouterr().out
    assert out.count("🙋 Нужно ваше решение") == 1
    assert "Чат: 12" in out and "Кнопки: да | нет" in out


def test_no_reply_is_remembered():
    op = make_operation()
    chat = op.parse_chat_item(chat_item(13), VACANCIES, {}, RESUMES[1])
    _robot_chat(op, [{"id": 5, "participantId": EMPLOYER, "text": "Спасибо, передам ответы"}])
    ai = op.tool.get_cover_letter_ai.return_value
    ai.complete.return_value = "[БЕЗ_ОТВЕТА]"
    op.reply_to_chat(chat)
    op.reply_to_chat(chat)
    assert ai.complete.call_count == 1


def test_markdown_is_stripped_from_chat_reply():
    op = make_operation()
    chat = op.parse_chat_item(chat_item(14), VACANCIES, {}, RESUMES[1])
    _robot_chat(op, [{"participantId": EMPLOYER, "text": "Расскажите про PyTorch"}])
    op.tool.get_cover_letter_ai.return_value.complete.return_value = "**PyTorch:** 3+ года"
    op.reply_to_chat(chat)
    assert op._post.call_args.args[1]["text"] == "PyTorch: 3+ года"


def test_send_chat_mode_sends_one_message():
    op = make_operation()
    op._post = MagicMock(return_value={})
    op.tool.config_path = Path(tempfile.mkdtemp())
    args = SimpleNamespace(**{**vars(op.args), "send_chat": 42, "text": "да"})
    op.run(op.tool, args)
    path, body, _ = op._post.call_args.args
    assert path == "/chatik/api/send" and body["chatId"] == 42 and body["text"] == "да"


def test_candidate_answers_let_bot_answer_office_question_itself():
    op = make_operation()
    (op.tool.config_path / "candidate_answers.txt").write_text(
        "Формат: только удалёнка, на офис и гибрид — «нет».\nГрафик: любой — соглашайся.\n",
        encoding="utf-8",
    )
    chat = op.parse_chat_item(chat_item(15, is_bot=True), VACANCIES, {}, RESUMES[1])
    office = {
        "id": 900,
        "participantId": "90236278",
        "text": "Готовы ли к офисному формату м. Курская?",
        "participantDisplay": {"isBot": True, "name": "Робот-рекрутер"},
        "actions": {"text_buttons": [{"text": "да"}, {"text": "нет"}]},
    }
    _robot_chat(op, [office])
    ai = op.tool.get_cover_letter_ai.return_value
    ai.complete.return_value = "нет"

    op.reply_to_chat(chat)

    system_prompt = op.tool.get_cover_letter_ai.call_args.args[0]
    assert "только удалёнка" in system_prompt and "Решения соискателя" in system_prompt
    assert op._post.call_args.args[1]["text"] == "нет"


def test_vacancy_area_is_in_prompt():
    op = make_operation()
    vacancies = {"137838539": {**VACANCIES["137838539"], "area": {"name": "Минск"}}}
    chat = op.parse_chat_item(chat_item(16), vacancies, {}, RESUMES[1])
    assert chat.vacancy_area == "Минск"
    assert "Город вакансии: Минск" in op.build_user_prompt(chat, "история")
