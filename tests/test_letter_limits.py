from hh_applicant_tool.operations.apply_vacancies import shorten_letter

from tests.test_apply_limit_shutdown import _make_operation, _make_vacancy


def test_shorten_letter_keeps_contact_and_limit():
    text = (
        "Добрый день!\n\n"
        + "Опыт работы с LLM и RAG в проде. " * 80
        + "\n\nЖду обратной связи в Telegram: https://t.me/krou4"
    )
    short = shorten_letter(text, 1500)
    assert len(short) <= 1500
    assert short.endswith("https://t.me/krou4")
    assert short.startswith("Добрый день!")
    assert shorten_letter("коротко", 1500) == "коротко"


def test_title_search_stop_words_ignore_description():
    op = _make_operation(max_responses=0)
    op.excluded_filter = r"junior|java"
    op.search_field = ["name"]
    op.tool.api_client.get.return_value = {"description": "будете менторить junior, Java плюс"}
    ml = {**_make_vacancy(1), "name": "ML-инженер", "employer": {"name": "IBS"}}
    java = {**_make_vacancy(2), "name": "Java Developer", "employer": {"name": "X"}}
    assert not op._is_excluded(ml)
    assert op._is_excluded(java)
    assert not op.tool.api_client.get.called
