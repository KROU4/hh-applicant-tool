from __future__ import annotations

import argparse
import html
import logging
import random
import re
import time
from datetime import datetime
from email.message import EmailMessage
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal
from urllib.parse import urlparse

import requests

from ..ai.base import AIError
from ..ai.openrouter import OpenRouterDailyLimit
from ..api import BadResponse, Redirect, datatypes
from ..api.datatypes import PaginatedItems, SearchVacancy
from ..api.errors import ApiError, CaptchaRequired, LimitExceeded
from ..main import BaseNamespace, BaseOperation
from ..storage.repositories.errors import RepositoryError
from ..utils import hhcaptcha
from ..utils.datatypes import VacancyTestsData
from ..utils.find import find_key
from ..utils.json import JSONDecoder
from ..utils.string import (
    bool2str,
    rand_text,
    render_template,
    shorten,
    strip_tags,
    unescape_string,
)

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)

# Конец блока тестового отклика в выводе --dry-run (его разбирает Telegram-бот)
DRY_RUN_END = "=== конец ==="
# Код выхода, если hh попросил капчу: бот повторит прогон позже
CAPTCHA_EXIT_CODE = 3
# Сколько полных вакансий держать в памяти (сервер маленький)
VACANCY_CACHE_SIZE = 50


# Время каждого отправленного отклика (для лимита hh за 24 часа)
SENT_TIMES_FILENAME = "sent_times.txt"
# Лимит письма в форме отклика с тестом (hh отвечает too-long-letter)
TEST_LETTER_LIMIT = 1500


def shorten_letter(letter: str, limit: int) -> str:
    """Обрезает письмо по границе абзаца или предложения, сохраняя контакт в конце."""
    if len(letter) <= limit:
        return letter
    lines = letter.rstrip().split("\n")
    tail = lines[-1] if "t.me/" in lines[-1] or "@" in lines[-1] else ""
    body = "\n".join(lines[:-1]) if tail else letter
    budget = limit - (len(tail) + 2 if tail else 0)
    cut = body[:budget]
    for sep in ("\n\n", ". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos > budget // 2:
            cut = cut[: pos + 1]
            break
    return (cut.rstrip() + ("\n\n" + tail if tail else ""))[:limit]


class VacancyCaptcha(Exception):
    """hh требует капчу — прогон нужно поставить на паузу."""

    def __init__(self, captcha_url: str | None) -> None:
        super().__init__(f"Captcha required: {captcha_url}")
        self.captcha_url = captcha_url

DEFAULT_COVER_LETTER_SYSTEM_PROMPT = (
    "Ты пишешь сопроводительные письма на hh.ru от первого лица за кандидата. "
    "Начинай с «Добрый день!». Тон околопрофессиональный: уверенный и живой, "
    "без канцелярита и штампов вроде «с огромным интересом» или «идеально "
    "подхожу». Покажи, что кандидат закрывает ключевые требования вакансии: "
    "опирайся на опыт из резюме, связывай его с задачами вакансии и смело "
    "усиливай его правдоподобными деталями в пользу того, что требует "
    "вакансия. Кандидат владеет всем, что требует вакансия: никогда не "
    "признавай отсутствие опыта, навыка или знания, не пиши «опыта нет», "
    "«знаком поверхностно», «готов изучить» и подобное. Не обсуждай само "
    "резюме и никогда не принижай опыт кандидата. Не давай обещаний о "
    "формате работы (офис, удалёнка, график), переезде и зарплате — эти "
    "вопросы кандидат решает сам; если вакансия их касается, напиши, что "
    "детали готов обсудить в Telegram. Не указывай телефон, email и ссылку "
    "на резюме. Без плейсхолдеров, markdown и подписи — ответ будет "
    "отправлен без обработки."
)


class Namespace(BaseNamespace):
    resume_id: str | None
    letter_file: Path | None
    ignore_employers: Path | None
    force_message: bool
    use_ai: bool
    ai_filter: Literal["heavy", "light", "custom"] | None
    ai_rate_limit: int
    ai_filter_prompt: str | None
    system_prompt: str
    message_prompt: str
    letter_contact: str | None
    order_by: str
    search: str
    schedule: str
    work_format: list[str] | None
    dry_run: bool
    # Пошли доп фильтры, которых не было
    experience: str
    employment: list[str] | None
    area: list[str] | None
    metro: list[str] | None
    professional_role: list[str] | None
    industry: list[str] | None
    employer_id: list[str] | None
    excluded_employer_id: list[str] | None
    currency: str | None
    salary: int | None
    only_with_salary: bool
    label: list[str] | None
    period: int | None
    date_from: str | None
    date_to: str | None
    top_lat: float | None
    bottom_lat: float | None
    left_lng: float | None
    right_lng: float | None
    sort_point_lat: float | None
    sort_point_lng: float | None
    no_magic: bool
    premium: bool
    per_page: int
    total_pages: int
    excluded_filter: str | None
    included_filter: str | None
    recommended_first: bool
    priority_area: list[str] | None
    other_areas: str
    max_responses: int
    send_email: bool
    skip_tests: bool


class Operation(BaseOperation):
    """Откликнуться на все подходящие вакансии."""

    __aliases__ = ("apply", "apply-similar")

    # Сколько откликов отправлено за запуск по всем резюме
    total_applied: int = 0
    # Пауза между запросами полных вакансий (сек): реже капча hh
    vacancy_fetch_delay: tuple[float, float] = (3.0, 6.0)
    captcha_paused: bool = False
    # Контакт в конце AI-письма (например, ссылка на Telegram)
    letter_contact: str = ""
    # Регулярка ключевых слов: без совпадения вакансия пропускается
    included_filter: str | None = None

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--resume-id", help="Идентефикатор резюме")
        parser.add_argument(
            "--search",
            help="Строка поиска для фильтрации вакансий. Если указана, то поиск будет производиться по вакансиям. В остальных случаях отклики будут производиться по списку рекомендованных вакансий.",  # noqa: E501
            type=str,
        )
        parser.add_argument(
            "-L",
            "--letter-file",
            "--letter",
            help="Путь до файла с текстом сопроводительного письма.",
            type=Path,
        )
        parser.add_argument(
            "-f",
            "--force-message",
            "--force",
            help="Всегда отправлять сообщение при отклике",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--use-ai",
            "--ai",
            help="Использовать AI для генерации сообщений",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--ai-filter",
            help="Использовать AI для фильтрации вакансий. Режимы: heavy - полный анализ вакансии и резюме, light - быстрый анализ по названию и навыкам, custom - свой системный промпт (--ai-filter-prompt)",
            choices=["heavy", "light", "custom"],
            default=None,
        )
        parser.add_argument(
            "--ai-rate-limit",
            help="Лимит запросов к AI в минуту для фильтрации",
            type=int,
            default=40,
        )
        parser.add_argument(
            "--ai-filter-prompt",
            help="Системный промпт для AI-фильтра (используется только в режиме custom)",
            default=None,
        )
        parser.add_argument(
            "--system-prompt",
            "--ai-system",
            help="Системный промпт для AI генерации сопроводительных писем",
            default=DEFAULT_COVER_LETTER_SYSTEM_PROMPT,
        )
        parser.add_argument(
            "--message-prompt",
            "--prompt",
            help="Промпт для генерации сопроводительного письма",
            default="Сгенерируй сопроводительное письмо от моего имени для этой вакансии.",
        )
        parser.add_argument(
            "--letter-contact",
            help="Контакт в конце AI-письма, например ссылка на Telegram: https://t.me/username",
        )
        parser.add_argument(
            "--total-pages",
            "--pages",
            help="Количество обрабатываемых страниц поиска",  # noqa: E501
            default=20,
            type=int,
        )
        parser.add_argument(
            "--per-page",
            help="Сколько должно быть результатов на странице",  # noqa: E501
            default=100,
            type=int,
        )
        parser.add_argument(
            "--send-email",
            help="Отправлять письмо на email компании или рекрутера с просьбой рассмотреть резюме",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--skip-tests",
            help="Пропускать тесты при откликах вместо",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--excluded-filter",
            type=str,
            help=r"Исключить вакансии, если название или описание не соответствует шаблону. Например, `--excluded-filter 'junior|стажир|bitrix|дружн\w+ коллектив|полиграф|open\s*space|опенспейс|хакатон|конкурс|тестов\w+ задан'`",
        )
        parser.add_argument(
            "--recommended-first",
            action="store_true",
            help="Сначала откликаться на рекомендованные hh вакансии для резюме, потом на найденные поиском",
        )
        parser.add_argument(
            "--priority-area",
            nargs="+",
            help="Регион (area id), вакансии из которого обрабатываются раньше остальных, например 16 — Беларусь",
        )
        parser.add_argument(
            "--other-areas",
            choices=["all", "remote", "none"],
            default="all",
            help="Вакансии вне --priority-area: все, только удалёнка или никакие",
        )
        parser.add_argument(
            "--included-filter",
            type=str,
            help=r"Откликаться только на вакансии, где в названии или описании есть совпадение с шаблоном. Например, `--included-filter 'llm|rag|ai[- ]?engineer|genai'`",
        )
        parser.add_argument(
            "--max-responses",
            type=int,
            help="Отправить не более N откликов (по умолчанию без ограничения)",
        )
        parser.add_argument(
            "--dry-run",
            help="Не отправлять отклики, а только выводить информацию",
            action=argparse.BooleanOptionalAction,
        )

        # Дальше идут параметры в точности соответствующие параметрам запроса
        # при поиске подходящих вакансий
        api_search_filters = parser.add_argument_group(
            "Фильтры для поиска вакансий",
            "Эти параметры напрямую соответствуют фильтрам поиска HeadHunter API",
        )

        api_search_filters.add_argument(
            "--order-by",
            help="Сортировка вакансий",
            choices=[
                "publication_time",
                "salary_desc",
                "salary_asc",
                "relevance",
                "distance",
            ],
            # default="relevance",
        )
        api_search_filters.add_argument(
            "--experience",
            help="Уровень опыта работы (noExperience, between1And3, between3And6, moreThan6)",
            type=str,
            default=None,
        )
        api_search_filters.add_argument(
            "--schedule",
            help="Тип графика (fullDay, shift, flexible, remote, flyInFlyOut)",
            type=str,
        )
        api_search_filters.add_argument(
            "--work-format",
            nargs="+",
            help="Формат работы (REMOTE, HYBRID, ON_SITE, FIELD_WORK)",
        )
        api_search_filters.add_argument(
            "--employment", nargs="+", help="Тип занятости"
        )
        api_search_filters.add_argument(
            "--area", nargs="+", help="Регион (area id)"
        )
        api_search_filters.add_argument(
            "--metro", nargs="+", help="Станции метро (metro id)"
        )
        api_search_filters.add_argument(
            "--professional-role", nargs="+", help="Проф. роль (id)"
        )
        api_search_filters.add_argument(
            "--industry", nargs="+", help="Индустрия (industry id)"
        )
        api_search_filters.add_argument(
            "--employer-id", nargs="+", help="ID работодателей"
        )
        api_search_filters.add_argument(
            "--excluded-employer-id", nargs="+", help="Исключить работодателей"
        )
        api_search_filters.add_argument(
            "--currency", help="Код валюты (RUR, USD, EUR)"
        )
        api_search_filters.add_argument(
            "--salary", type=int, help="Минимальная зарплата"
        )
        api_search_filters.add_argument(
            "--only-with-salary",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        api_search_filters.add_argument(
            "--label", nargs="+", help="Метки вакансий (label)"
        )
        api_search_filters.add_argument(
            "--period", type=int, help="Искать вакансии за N дней"
        )
        api_search_filters.add_argument(
            "--date-from", help="Дата публикации с (YYYY-MM-DD)"
        )
        api_search_filters.add_argument(
            "--date-to", help="Дата публикации по (YYYY-MM-DD)"
        )
        api_search_filters.add_argument(
            "--top-lat", type=float, help="Гео: верхняя широта"
        )
        api_search_filters.add_argument(
            "--bottom-lat", type=float, help="Гео: нижняя широта"
        )
        api_search_filters.add_argument(
            "--left-lng", type=float, help="Гео: левая долгота"
        )
        api_search_filters.add_argument(
            "--right-lng", type=float, help="Гео: правая долгота"
        )
        api_search_filters.add_argument(
            "--sort-point-lat",
            type=float,
            help="Координата lat для сортировки по расстоянию",
        )
        api_search_filters.add_argument(
            "--sort-point-lng",
            type=float,
            help="Координата lng для сортировки по расстоянию",
        )
        api_search_filters.add_argument(
            "--no-magic",
            action="store_true",
            help="Отключить авторазбор текста запроса",
        )
        api_search_filters.add_argument(
            "--premium",
            default=False,
            action=argparse.BooleanOptionalAction,
            help="Только премиум вакансии",
        )
        api_search_filters.add_argument(
            "--search-field",
            nargs="+",
            help="Поля поиска (name, company_name и т.п.)",
        )

    cover_letter: str = "{Здравствуйте|Добрый день}, меня зовут %(first_name)s. {Прошу|Предлагаю} рассмотреть {мою кандидатуру|мое резюме «%(resume_title)s»} на вакансию «%(vacancy_name)s». С уважением, %(first_name)s."

    @property
    def api_client(self):
        return self.tool.api_client

    @property
    def args(self) -> Namespace:
        return self._args

    def run(
        self,
        tool: HHApplicantTool,
        args: Namespace,
    ) -> None:
        self.tool = tool
        self._args = args
        self.cover_letter = (
            args.letter_file.read_text(encoding="utf-8", errors="ignore")
            if args.letter_file
            else self.cover_letter
        )
        self.area = args.area
        self.bottom_lat = args.bottom_lat
        self.currency = args.currency
        self.date_from = args.date_from
        self.date_to = args.date_to
        self.dry_run = args.dry_run
        self.employer_id = args.employer_id
        self.employment = args.employment
        self.excluded_employer_id = args.excluded_employer_id
        self.excluded_filter = args.excluded_filter
        self.included_filter = args.included_filter
        self.recommended_first = bool(args.recommended_first)
        self.priority_area = args.priority_area
        self.other_areas = args.other_areas
        self.experience = args.experience
        self.force_message = args.force_message
        self.industry = args.industry
        self.label = args.label
        self.left_lng = args.left_lng
        self.max_responses = args.max_responses
        self.total_applied = 0
        self.metro = args.metro
        self.no_magic = args.no_magic
        self.only_with_salary = args.only_with_salary
        self.order_by = args.order_by
        self.per_page = args.per_page
        self.period = args.period
        self.message_prompt = args.message_prompt
        self.letter_contact = (args.letter_contact or "").strip()
        self.premium = args.premium
        self.professional_role = args.professional_role
        self.resume_id = args.resume_id
        self.right_lng = args.right_lng
        self.salary = args.salary
        self.schedule = args.schedule
        self.work_format = args.work_format
        self.search = args.search
        self.search_field = args.search_field
        self.sort_point_lat = args.sort_point_lat
        self.sort_point_lng = args.sort_point_lng
        self.top_lat = args.top_lat
        self.total_pages = args.total_pages
        self.cover_letter_ai = (
            tool.get_cover_letter_ai(args.system_prompt)
            if args.use_ai
            else None
        )
        self.ai_filter = args.ai_filter
        self.ai_filter_prompt = args.ai_filter_prompt
        self.vacancy_filter_ai = None
        self._resume_analysis_cache: dict[tuple[str | None, str], str] = {}

        return self._apply_vacancies()

    def _get_full_resume(self, resume_id: str) -> dict:
        return self.api_client.get(f"/resumes/{resume_id}")

    def _analyze_resume_heavy(self, resume: dict) -> str:
        resume_id = resume.get("id")
        cache_key = (resume_id, "heavy")
        if cache_key in self._resume_analysis_cache:
            return self._resume_analysis_cache[cache_key]

        if resume_id:
            try:
                full_resume = self._get_full_resume(resume_id)

                parts = []

                title = full_resume.get("title", "")
                if title:
                    parts.append(f"Должность: {title}")

                if "skills" in full_resume:
                    parts.append("\n---------- О СЕБЕ ----------")
                    parts.append(full_resume.get("skills", ""))

                if "skill_set" in full_resume and full_resume["skill_set"]:
                    parts.append("\n---------- НАВЫКИ ----------")
                    skills_row = ", ".join(full_resume["skill_set"])
                    parts.append(skills_row)

                if "experience" in full_resume:
                    parts.append("\n---------- ОПЫТ РАБОТЫ ----------")
                    for exp in full_resume.get("experience", []):
                        company = exp.get("company", "Не указано")
                        position = exp.get("position", "Не указано")
                        start = exp.get("start", "")
                        end = exp.get("end") or "по настоящее время"

                        parts.append(f"\n- {company}")
                        parts.append(f" Должность: {position}")
                        parts.append(f" Период: {start} - {end}")

                        description = exp.get("description")
                        if description:
                            parts.append(" Описание:")
                            parts.append(f" {description}")

                result = "\n".join(parts)
                self._resume_analysis_cache[cache_key] = result
                return result

            except Exception as e:
                logger.warning(f"Не удалось получить полное резюме: {e}")

        return ""

    def _analyze_resume_light(self, resume: dict) -> str:
        resume_id = resume.get("id")
        cache_key = (resume_id, "light")
        if cache_key in self._resume_analysis_cache:
            return self._resume_analysis_cache[cache_key]

        parts = []

        full_resume = self._get_full_resume(resume_id)

        title = full_resume.get("title", "")
        if title:
            parts.append(f"Должность: {title}")

        if "skill_set" in full_resume and full_resume["skill_set"]:
            parts.append("Навыки: ")
            skills_row = ", ".join(full_resume["skill_set"])
            parts.append(skills_row)

        result = "\n".join(parts)
        self._resume_analysis_cache[cache_key] = result
        return result

    def _build_cover_letter_prompt(
        self, vacancy: dict, resume: dict, placeholders: dict[str, str]
    ) -> str:
        """Запрос к AI с полным текстом вакансии и резюме.

        Раньше модель видела только названия и потому выдумывала опыт мимо
        вакансии. Контакты и ссылку на резюме не передаём: работодатель их
        видит, а в письме они лишние.
        """
        full_vacancy: dict = {}
        if vacancy.get("id"):
            try:
                full_vacancy = self._full_vacancy(vacancy["id"])
            except ApiError as ex:
                logger.warning("Не удалось получить вакансию: %s", ex)
        description = strip_tags(
            full_vacancy.get("description")
            or " ".join(
                filter(None, (vacancy.get("snippet") or {}).values())
            )
        )
        key_skills = ", ".join(
            s["name"] for s in full_vacancy.get("key_skills") or [] if s.get("name")
        )

        parts = [
            self.message_prompt,
            "",
            "[ВАКАНСИЯ]",
            f"Название: {placeholders['vacancy_name']}",
            f"Работодатель: {placeholders['employer_name']}",
        ]
        if key_skills:
            parts.append(f"Ключевые навыки: {key_skills}")
        if description:
            parts.append(f"Описание:\n{description[:4000]}")
        parts += [
            "",
            "[РЕЗЮМЕ]",
            f"Имя: {placeholders['first_name']} {placeholders['last_name']}".strip(),
            strip_tags(self._analyze_resume_heavy(resume))[:6000]
            or f"Должность: {placeholders['resume_title']}",
        ]
        answers = self._candidate_answers()
        if answers:
            parts += [
                "",
                "[РЕШЕНИЯ КАНДИДАТА] (письмо не должно им противоречить)",
                answers,
            ]
        if self.letter_contact:
            parts += [
                "",
                "В конце напиши, что ждёшь обратной связи, и укажи Telegram "
                f"для связи: {self.letter_contact}",
            ]
        return "\n".join(parts)

    def _candidate_answers(self) -> str:
        """Решения соискателя из «📋 Ответы рекрутерам» Telegram-бота."""
        try:
            path = self.tool.config_path / "candidate_answers.txt"
            text = path.read_text(encoding="utf-8")
        except (OSError, TypeError, AttributeError):
            return ""
        return text.strip()[:3000] if isinstance(text, str) else ""

    def _finalize_letter(self, letter: str) -> str:
        # hh показывает письмо как обычный текст: markdown остался бы звёздочками
        letter = re.sub(r"(\*\*|__)(.+?)\1", r"\2", letter)
        letter = re.sub(r"^#{1,6}\s*", "", letter, flags=re.M).strip()
        # Модель может забыть контакт — он нужен в каждом письме
        if self.letter_contact and self.letter_contact not in letter:
            letter += f"\n\nЖду обратной связи в Telegram: {self.letter_contact}"
        return letter

    def _get_vacancy_key_skills(self, vacancy_id: str | int) -> str:
        try:
            full_vacancy = self._full_vacancy(vacancy_id)
            key_skills_data = full_vacancy.get("key_skills") or []
            return ", ".join(
                s["name"] for s in key_skills_data if s.get("name")
            )
        except Exception as e:
            logger.warning(
                "Не удалось получить key_skills вакансии %s: %s", vacancy_id, e
            )
            return ""

    def _build_vacancy_context(
        self,
        vacancy: dict,
        full_vacancy: dict | None = None,
        include_full: bool = False,
    ) -> str:
        parts: list[str] = []

        name = vacancy.get("name")
        if name:
            parts.append(f"Вакансия: {name}")

        if full_vacancy:
            description = full_vacancy.get("description")
            if description:
                parts.append(f"Описание: {strip_tags(description)}")
        else:
            if vacancy.get("id"):
                key_skills = self._get_vacancy_key_skills(vacancy["id"])
                if key_skills:
                    parts.append(f"Ключевые навыки: {key_skills}")

        return "\n".join(parts)

    def _ask_ai_suitability(
        self, prompt: str, vacancy_name: str, log_suffix: str = ""
    ) -> bool:

        MAX_RETRIES = 3

        if not self.vacancy_filter_ai:
            return True

        for attempt in range(MAX_RETRIES):
            try:
                response = self.vacancy_filter_ai.complete(prompt).strip()

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "AI %s ответ (попытка %d): %s",
                        log_suffix,
                        attempt + 1,
                        response,
                    )

                result = self._parse_ai_json_response(response)
                if result is not None:
                    if result:
                        return True
                    logger.info(
                        "Вакансия %s отклонена AI %s", vacancy_name, log_suffix
                    )
                    return False

                # Если не удалось распарсить JSON, повторяем запрос
                logger.warning(
                    "AI %s не дал валидный JSON для вакансии %s (попытка %d/%d)",
                    log_suffix,
                    vacancy_name,
                    attempt + 1,
                    MAX_RETRIES,
                )
                continue

            except OpenRouterDailyLimit as e:
                # Без фильтра откликнулись бы на всё подряд
                logger.error("AI %s недоступен, вакансия пропущена: %s", log_suffix, e)
                return False
            except AIError as e:
                # ChatOpenAI уже делает retry для 429, поэтому здесь только логируем
                logger.error("Ошибка AI %s: %s", log_suffix, e)
                return True

        logger.warning(
            "AI %s не дал валидный JSON после %d попыток для вакансии %s",
            log_suffix,
            MAX_RETRIES,
            vacancy_name,
        )
        return True

    def _parse_ai_json_response(self, response: str) -> bool | None:
        response = response.strip().lower()

        if response in ("да", "yes", "true"):
            return True
        if response in ("нет", "no", "false"):
            return False

        import re

        from ..utils import json as utils_json

        if response.startswith("```"):
            response = re.sub(r"^```(?:json)?\s*", "", response)
            response = re.sub(r"\s*```$", "", response)

        json_match = re.search(
            r'\{[^{}]*"suitable"\s*:\s*(true|false)[^{}]*\}',
            response,
            re.IGNORECASE,
        )
        if json_match:
            try:
                data = utils_json.loads(json_match.group(0))
                return data.get("suitable")
            except Exception:
                pass

        return None

    # КТО ЭТО ПРОЧИТАЛ ТОТ ПИД@РАС
    def _is_vacancy_suitable_heavy(
        self, vacancy: dict, log_suffix: str = "(heavy)"
    ) -> bool:
        full_vacancy = None
        if vacancy.get("id"):
            full_vacancy = self._full_vacancy(vacancy["id"])

        vacancy_info = self._build_vacancy_context(
            vacancy,
            full_vacancy=full_vacancy,
            include_full=True,
        )
        prompt = f"Вакансия: {vacancy_info}"
        return self._ask_ai_suitability(
            prompt, vacancy.get("name", ""), log_suffix
        )

    def _is_vacancy_suitable_light(self, vacancy: dict) -> bool:
        vacancy_info = self._build_vacancy_context(vacancy, include_full=False)
        prompt = f"Вакансия: {vacancy_info}"
        return self._ask_ai_suitability(
            prompt, vacancy.get("name", ""), "(light)"
        )

    def _build_filter_system_prompt_heavy(self, resume_analysis: str) -> str:
        return f"""
Определи, подходит ли вакансия кандидату.

Смотри в первую очередь на тип работы (роль), а не на технологии.

Правила:

1. Если работа по сути другая -> suitable = false

2. Если роль совпадает или очень близкая:
   - есть пересечения по задачам или навыкам -> suitable = true
   - даже частичное совпадение допустимо

3. Общие технологии сами по себе ничего не значат.
   Если работа разная, это не делает вакансию подходящей.

4. Если данных мало:
   - ориентируйся на название роли

Не пиши объяснения.
Ответ строго JSON:
{{"suitable": true}} или {{"suitable": false}}

Кандидат:
{resume_analysis}
"""

    def _build_filter_system_prompt_light(self, resume_analysis: str) -> str:
        return f"""
Ты делаешь очень грубую проверку: подходит вакансия или нет.

Используй только:
- название резюме
- список навыков резюме
- название вакансии
- явно указанные ключевые навыки вакансии

Не анализируй описание, обязанности, контекст, домен, уровень, карьерный рост и прочую воду.
Не додумывай ничего, чего нет в тексте.

Правила:
- если название вакансии и резюме в одной профессии или близких ролях, и есть хотя бы частичное совпадение по ключевым навыкам -> suitable = true
- если роли явно разные или совпадений по навыкам почти нет -> suitable = false
- если данных мало -> ориентируйся только на явные совпадения, без фантазий

Ответ только JSON:
{{"suitable": true}} или {{"suitable": false}}

Кандидат:
{resume_analysis}
"""

    def _apply_vacancies(self) -> int | None:
        resumes: list[datatypes.Resume] = self.tool.get_resumes()
        try:
            self.tool.storage.resumes.save_batch(resumes)
        except RepositoryError as ex:
            logger.exception(ex)
        resumes = (
            list(filter(lambda x: x["id"] == self.resume_id, resumes))
            if self.resume_id
            else resumes
        )
        # Выбираем только опубликованные
        resumes = list(
            filter(lambda x: x["status"]["id"] == "published", resumes)
        )
        if not resumes:
            logger.warning("У вас нет опубликованных резюме")
            return

        me: datatypes.User = self.tool.get_me()
        seen_employers = set()

        for resume in resumes:
            limit_reached = self._apply_resume(
                resume=resume,
                user=me,
                seen_employers=seen_employers,
            )
            if limit_reached:
                logger.warning(
                    "Лимит откликов hh.ru исчерпан. Пропускаю оставшиеся резюме."
                )
                print("⛔ Лимит откликов hh.ru исчерпан. Попробуйте позже.")
                break
            if self.max_responses and self.total_applied >= self.max_responses:
                break
            if self.captcha_paused:
                break

        # Синхронизация откликов
        # for neg in self.tool.get_negotiations():
        #     try:
        #         self.tool.storage.negotiations.save(neg)
        #     except RepositoryError as e:
        #         logger.warning(e)

        print("📝 Отклики на вакансии разосланы!")
        if self.captcha_paused:
            return CAPTCHA_EXIT_CODE
        return None

    def _apply_resume(
        self,
        resume: datatypes.Resume,
        user: datatypes.User,
        seen_employers: set[str],
    ) -> bool:
        logger.info(
            "Начинаю рассылку откликов для резюме: %s (%s)",
            resume["alternate_url"],
            resume["title"],
        )
        print("🚀 Начинаю рассылку откликов для резюме:", resume["title"])
        applied_count = 0
        limit_reached = False

        placeholders = {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "email": user.get("email") or "",
            "phone": user.get("phone") or "",
            "resume_hash": resume.get("id") or "",
            "resume_title": resume.get("title") or "",
            "resume_url": resume.get("alternate_url") or "",
        }

        do_apply = True
        storage = self.tool.storage
        site_emails = {}

        if self.ai_filter:
            if self.ai_filter in ("heavy", "custom"):
                resume_analysis = self._analyze_resume_heavy(resume)
            elif self.ai_filter == "light":
                resume_analysis = self._analyze_resume_light(resume)
            else:
                raise ValueError(
                    f"Неизвестный режим AI фильтра: {self.ai_filter}"
                )

            if self.ai_filter == "custom":
                if not self.ai_filter_prompt:
                    raise ValueError(
                        "Режим 'custom' требует --ai-filter-prompt"
                    )
                # Кастомный промпт заменяет только инструкции. Анализ резюме
                # добавляем блоком "Кандидат" в конец (как во встроенных).
                system_prompt = (
                    f"{self.ai_filter_prompt}\n\nКандидат:\n{resume_analysis}\n\n"
                    "Не пиши объяснения.\n"
                    'Ответ строго JSON:\n'
                    '{{"suitable": true}} или {{"suitable": false}}'
                )
            elif self.ai_filter == "heavy":
                system_prompt = self._build_filter_system_prompt_heavy(
                    resume_analysis
                )
            else:
                system_prompt = self._build_filter_system_prompt_light(
                    resume_analysis
                )

            logger.debug(
                "AI системный промпт (%s, custom=%s): %s",
                self.ai_filter,
                bool(self.ai_filter_prompt),
                system_prompt,
            )

            self.vacancy_filter_ai = self.tool.get_vacancy_filter_ai(
                system_prompt
            )

            if self.args.ai_rate_limit:
                self.vacancy_filter_ai.rate_limit = self.args.ai_rate_limit

        for vacancy in self._get_vacancies(resume_id=resume["id"]):
            # С предыдущей вакансией закончили — её данные больше не нужны
            self._forget_vacancies()
            if (
                getattr(self, "_cancel_event", None)
                and self._cancel_event.is_set()
            ):
                logger.info("Операция отменена пользователем")
                break
            # Лимит общий на все резюме за запуск, а не на каждое
            if (
                self.max_responses
                and self.total_applied + applied_count >= self.max_responses
            ):
                logger.info(
                    "Достигнут лимит откликов --max-responses (%d). Останавливаюсь.",
                    self.max_responses,
                )
                break
            try:
                employer = vacancy.get("employer", {})

                message_placeholders = {
                    "vacancy_name": vacancy.get("name", ""),
                    "vacancy_url": vacancy.get("alternate_url") or "",
                    "employer_name": employer.get("name", ""),
                    **placeholders,
                }

                try:
                    storage.vacancies.save(vacancy)
                except RepositoryError as ex:
                    logger.debug(ex)

                # По факту контакты можно получить только здесь?!
                if vacancy.get("contacts"):
                    logger.debug(
                        f"Найдены контакты в вакансии: {vacancy['alternate_url']}"
                    )

                    try:
                        # logger.debug(vacancy)
                        storage.vacancy_contacts.save(vacancy)
                    except RepositoryError as ex:
                        logger.exception(ex)

                if not do_apply:
                    continue

                vacancy_id = vacancy["id"]
                relations = vacancy.get("relations", [])

                if relations:
                    logger.debug(
                        "Пропускаем вакансию с откликом: %s",
                        vacancy["alternate_url"],
                    )
                    if "got_rejection" in relations:
                        logger.debug(
                            "Вы получили отказ от %s",
                            vacancy["alternate_url"],
                        )
                        print("⛔ Пришел отказ от", vacancy["alternate_url"])
                    continue

                if vacancy.get("archived"):
                    logger.debug(
                        "Пропускаем вакансию в архиве: %s",
                        vacancy["alternate_url"],
                    )
                    continue

                if vacancy.get("has_test") and self.args.skip_tests:
                    logger.debug(
                        "Пропускаю вакансию с тестом %s",
                        vacancy["alternate_url"],
                    )
                    continue

                if redirect_url := vacancy.get("response_url"):
                    logger.debug(
                        "Пропускаем вакансию %s с перенаправлением: %s",
                        vacancy["alternate_url"],
                        redirect_url,
                    )
                    continue

                if not self._is_included(vacancy):
                    logger.info(
                        "Вакансия не содержит ключевых слов: %s",
                        vacancy["alternate_url"],
                    )
                    self._save_skipped_vacancy(
                        vacancy, "included_filter", resume["id"]
                    )
                    continue

                if self._is_excluded(vacancy):
                    logger.info(
                        "Вакансия попала под фильтр: %s",
                        vacancy["alternate_url"],
                    )

                    self._save_skipped_vacancy(
                        vacancy, "excluded_filter", resume["id"]
                    )

                    self.api_client.put(
                        f"/vacancies/blacklisted/{vacancy['id']}"
                    )
                    logger.info(
                        "Вакансия добавлена в черный список: %s",
                        vacancy["alternate_url"],
                    )
                    continue

                # AI фильтрация вакансий
                if self.ai_filter and self.vacancy_filter_ai:
                    if self._is_vacancy_already_skipped(vacancy, resume["id"]):
                        logger.debug(
                            "Вакансия уже была отклонена ранее: %s",
                            vacancy["alternate_url"],
                        )
                        print(
                            "⏩ Вакансия уже отклонена ранее",
                            vacancy["alternate_url"],
                        )
                        continue

                    if self.ai_filter in ("heavy", "custom"):
                        is_suitable = self._is_vacancy_suitable_heavy(
                            vacancy, "(custom)" if self.ai_filter == "custom" else "(heavy)"
                        )
                    else:
                        is_suitable = self._is_vacancy_suitable_light(vacancy)

                    if not is_suitable:
                        logger.info(
                            "Вакансия отклонена AI фильтром (%s): %s",
                            self.ai_filter,
                            vacancy["alternate_url"],
                        )
                        print(
                            f"🧠 AI ({self.ai_filter}) посчитал неподходящей",
                            vacancy["alternate_url"],
                        )

                        self._save_skipped_vacancy(
                            vacancy, "ai_rejected", resume["id"]
                        )
                        continue

                # Перед откликом выгружаем профиль компании
                employer_id = employer.get("id")
                if employer_id and employer_id not in seen_employers:
                    employer_profile: datatypes.Employer = self.api_client.get(
                        f"/employers/{employer_id}"
                    )

                    try:
                        storage.employers.save(employer_profile)
                    except RepositoryError as ex:
                        logger.exception(ex)

                    # Если есть сайт, то ищем на нем емейлы для отправки письма
                    if self.args.send_email and (
                        site_url := (
                            employer_profile.get("site_url") or ""
                        ).strip()
                    ):
                        site_url = (
                            site_url
                            if "://" in site_url
                            else "https://" + site_url
                        )
                        logger.debug("visit site: %s", site_url)

                        try:
                            site_info = self._parse_site(site_url)
                            site_emails[employer_id] = site_info["emails"]
                        except requests.RequestException as ex:
                            site_info = None
                            logger.error(ex)

                        if site_info:
                            logger.debug("site info: %r", site_info)

                            # try:
                            #     subdomains = self._get_subdomains(site_url)
                            # except requests.RequestException as ex:
                            #     subdomains = []
                            #     logger.error(ex)

                            try:
                                storage.employer_sites.save(
                                    {
                                        "site_url": site_url,
                                        "employer_id": employer_id,
                                        "subdomains": [],
                                        **site_info,
                                    }
                                )
                            except RepositoryError as ex:
                                logger.exception(ex)

                letter = ""

                if self.force_message or vacancy.get(
                    "response_letter_required"
                ):
                    if self.cover_letter_ai:
                        msg = self._build_cover_letter_prompt(
                            vacancy, resume, message_placeholders
                        )
                        letter = self._finalize_letter(
                            self.cover_letter_ai.complete(msg)
                        )
                    else:
                        letter = render_template(
                            rand_text(self.cover_letter),
                            message_placeholders,
                            "сопроводительном письме",
                        )

                    logger.debug(letter)

                logger.debug(
                    "Пробуем откликнуться на вакансию: %s",
                    vacancy["alternate_url"],
                )

                if self.dry_run:
                    # Считаем, чтобы --max-responses ограничивал и тестовый
                    # прогон, и показываем, что именно ушло бы работодателю
                    applied_count += 1
                    print(
                        f"🧪 Тест: откликнулся бы на «{vacancy.get('name', '')}» — "
                        f"{employer.get('name', '')}\n{vacancy['alternate_url']}\n"
                        f"--- письмо ---\n{letter or '(без письма)'}\n"
                        f"{DRY_RUN_END}"
                    )
                    continue

                test_handled = False

                if vacancy.get("has_test"):
                    logger.debug(
                        "Решаем тест: %s",
                        vacancy["alternate_url"],
                    )

                    try:
                        if not self.dry_run:
                            result = self._solve_vacancy_test(
                                vacancy_id=vacancy["id"],
                                resume_hash=resume["id"],
                                letter=letter,
                            )
                            if (
                                isinstance(result, dict)
                                and result.get("error") == "too-long-letter"
                            ):
                                # В форме с тестом у письма свой, меньший лимит
                                letter = shorten_letter(letter, TEST_LETTER_LIMIT)
                                result = self._solve_vacancy_test(
                                    vacancy_id=vacancy["id"],
                                    resume_hash=resume["id"],
                                    letter=letter,
                                )
                            test_handled = True
                            if result.get("success") == "true":
                                applied_count += 1
                                self._record_sent()
                                print(
                                    "📨 Отправили отклик на вакансию с тестом",
                                    vacancy["alternate_url"],
                                )
                            else:
                                err = (
                                    result.get("error")
                                    if isinstance(result, dict)
                                    else None
                                )

                                if err == "negotiations-limit-exceeded":
                                    do_apply = False
                                    limit_reached = True
                                    logger.warning(
                                        "Достигли лимита на отклики (отправлено в этой сессии: %d)",
                                        applied_count,
                                    )
                                    break
                                else:
                                    status = (
                                        result.get("_http_status")
                                        if isinstance(result, dict)
                                        else None
                                    )
                                    logger.error(
                                        "Произошла ошибка при отклике на вакансию с тестом: %s (HTTP %s), result: %s",
                                        vacancy["alternate_url"],
                                        status if status is not None else "?",
                                        shorten(str(result), 300),
                                    )
                        else:
                            test_handled = True
                    except ValueError as ex:
                        if str(ex) == "tests not found.":
                            logger.warning(
                                "Не удалось получить тест (%s), пробую откликнуться как на обычную вакансию: %s",
                                ex,
                                vacancy["alternate_url"],
                            )
                        else:
                            logger.error(f"Произошла непредвиденная ошибка: {ex}")
                            continue
                    except Exception as ex:
                        logger.error(f"Произошла непредвиденная ошибка: {ex}")
                        continue

                if not test_handled:
                    params = {
                        "resume_id": resume["id"],
                        "vacancy_id": vacancy_id,
                        "message": letter,
                    }
                    try:
                        if not self.dry_run:
                            res = self.api_client.post(
                                "/negotiations",
                                params,
                                delay=random.uniform(1, 3),
                            )
                            assert res == {}
                            applied_count += 1
                            self._record_sent()
                            print(
                                "📨 Отправили отклик на вакансию",
                                vacancy["alternate_url"],
                            )
                    except Redirect:
                        logger.warning(
                            f"Игнорирую перенаправление на форму: {vacancy['alternate_url']}"  # noqa: E501
                        )
                        continue
                    except CaptchaRequired as ex:
                        # Капчу вводит владелец в Telegram-боте; вакансия
                        # вернётся в работу при продолжении прогона
                        raise VacancyCaptcha(ex.captcha_url) from ex

                # Отправка письма на email
                if self.args.send_email:
                    # fix NoneType has no attribute get
                    # contacts может быть null
                    mail_to: str | list[str] | None = (
                        vacancy.get("contacts") or {}
                    ).get("email") or site_emails.get(employer_id)
                    if mail_to:
                        mail_to = (
                            ", ".join(mail_to)
                            if isinstance(mail_to, list)
                            else mail_to
                        )
                        # Шаблоны письма рендерим внутри try: ошибка в них
                        # не должна обрывать рассылку остальных откликов
                        try:
                            mail_subject = render_template(
                                rand_text(
                                    self.tool.config.get("apply_mail_subject")
                                    or "{Отклик|Резюме} на вакансию %(vacancy_name)s"
                                ),
                                message_placeholders,
                                "apply_mail_subject",
                            )
                            mail_body = render_template(
                                unescape_string(
                                    rand_text(
                                        self.tool.config.get("apply_mail_body")
                                        or "{Здравствуйте|Добрый день}, {прошу рассмотреть|пожалуйста рассмотрите} мое резюме %(resume_url)s на вакансию %(vacancy_name)s."
                                    )
                                ),
                                message_placeholders,
                                "apply_mail_body",
                            )
                            self._send_email(mail_to, mail_subject, mail_body)
                            print(
                                "📧 Отправлено письмо на email по поводу вакансии",
                                vacancy["alternate_url"],
                            )
                        except Exception as ex:
                            logger.error(f"Ошибка отправки письма: {ex}")
            except VacancyCaptcha as ex:
                logger.warning("hh запросил капчу: %s", ex)
                self._prepare_captcha(ex.captcha_url)
                print(
                    "⏸ hh запросил капчу — ставлю отклики на паузу, "
                    "продолжу после ввода капчи"
                )
                self.captcha_paused = True
                break
            except LimitExceeded:
                do_apply = False
                limit_reached = True
                logger.warning(
                    "Достигли лимита на отклики (отправлено в этой сессии: %d)",
                    applied_count,
                )
                break
            except ApiError as ex:
                logger.warning(ex)
            except (BadResponse, AIError) as ex:
                logger.error(ex)
            except requests.RequestException as ex:
                # Сбой сети или защита сайта на одной вакансии не должны
                # обрывать весь прогон
                logger.error(
                    "Сетевая ошибка на вакансии %s: %s",
                    vacancy.get("alternate_url"),
                    ex,
                )

        self._forget_vacancies()

        logger.info(
            "Закончили рассылку откликов для резюме: %s (%s). Отправлено: %d",
            resume["alternate_url"],
            resume["title"],
            applied_count,
        )
        print(
            f"✅️ Закончили рассылку для резюме: {resume['title']}. Отправлено: {applied_count}"
        )
        self.total_applied += applied_count
        return limit_reached

    def _send_email(self, to: str, subject: str, body: str) -> None:
        cfg = self.tool.config.get("smtp", {})
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("from") or cfg.get("user")
        msg["To"] = to
        msg.set_content(body)
        self.tool.smtp.send_message(msg)

    json_decoder = JSONDecoder()

    def _get_vacancy_tests(self, response_url: str) -> VacancyTestsData | None:
        """Парсит тесты"""
        res = self.tool.get_redirect_config(response_url)  
        return find_key(res, "vacancyTests")

    def _solve_vacancy_test(
        self,
        vacancy_id: str | int,
        resume_hash: str,
        letter: str = "",
    ) -> dict[str, Any]:
        """Загружает тест, ждет паузу и отправляет отклик."""
        response_url = f"https://hh.ru/applicant/vacancy_response?vacancyId={vacancy_id}&startedWithQuestion=false&hhtmFrom=vacancy"

        if not (tests_data := self._get_vacancy_tests(response_url)):
            raise ValueError(f"Данные тестов не найдены на {response_url}.")

        if not (test_data := tests_data.get(str(vacancy_id))):
            raise ValueError(f"Пустые данные теста вакансии vacancy_id={vacancy_id}")
        
        logger.debug(f"{test_data = }")

        payload: dict[str, Any] = {
            "_xsrf": self.tool.xsrf_token,
            "uidPk": test_data["uidPk"],
            "guid": test_data["guid"],
            "startTime": test_data["startTime"],
            "testRequired": test_data["required"],
            "vacancy_id": vacancy_id,
            "resume_hash": resume_hash,
            "ignore_postponed": "true",
            "incomplete": "false",
            "mark_applicant_visible_in_vacancy_country": "false",
            "country_ids": "[]",
            "lux": "true",
            "withoutTest": "no",
            "letter": letter,
        }

        for task in test_data["tasks"]:
            field_name = f"task_{task['id']}"
            solutions = task.get("candidateSolutions") or []
            question = (task.get("description") or "").strip()

            if solutions:
                if self.cover_letter_ai:
                    options = "\n".join(
                        [
                            f"{s['id']}: {strip_tags(s['text'])}"
                            for s in solutions
                        ]
                    )
                    prompt = (
                        f"Вопрос: {question}\n"
                        f"Варианты:\n{options}\n"
                        f"Выбери ID правильного ответа. Пришли только ID."
                    )
                    ai_answer = self.cover_letter_ai.complete(prompt).strip()
                    # Ищем ID в ответе AI на случай лишнего текста
                    match = re.search(r"\d+", ai_answer)
                    selected_id = (
                        match.group(0) if match else solutions[0]["id"]
                    )
                    payload[field_name] = selected_id
                else:
                    yes_solution = next(
                        filter(lambda x: x["text"].lower() == "да", solutions),
                        None,
                    )

                    payload[field_name] = (
                        yes_solution["id"]
                        if yes_solution
                        # По статистике правильный ответ в большинстве случаев
                        # находится посередине
                        else solutions[len(solutions) // 2]["id"]
                    )
            else:
                # Рандомные эмоджи
                # payload[f"{field_name}_text"] = "".join(
                #     chr(random.randint(0x1F300, 0x1F64F))
                #     for _ in range(random.randint(3, 15))
                # )

                if "://" in question:
                    answer = rand_text(
                        "{{Простите|Извините}, но я не перехожу по {внешним|сторонним} ссылкам, так как {опасаюсь взлома|не хочу {быть взломанным|подхватить вирус|чтобы у меня {со|с банковского} счета украли деньги}}.|У меня нет времени на заполнение анкет и гуглодоков}"
                    )
                elif self.cover_letter_ai:
                    prompt = f"Дай краткий и профессиональный ответ на вопрос: {question}"
                    answer = self.cover_letter_ai.complete(prompt)
                    ## добавляем ответ AI на вопрос теста##
                    logger.debug("AI ответ= %r", answer)
                # Тупоеблые любят вопросы с ответами да/нет, где ответ да является правильным в большинстве случаев.
                else:
                    answer = "Да"

                payload[f"{field_name}_text"] = answer

        logger.debug(f"{payload = }")

        # Ожидание перед отправкой (float)
        time.sleep(random.uniform(2.0, 3.0))

        response = self.tool.session.post(
            "https://hh.ru/applicant/vacancy_response/popup",
            data=payload,
            headers={
                "Referer": response_url,
                # x-gib-fgsscgib-w-hh и x-gib-gsscgib-w-hh вроде в куках
                # передаются и не нужны
                "X-Hhtmfrom": "vacancy",
                "X-Hhtmsource": "vacancy_response",
                "X-Requested-With": "XMLHttpRequest",
                "X-Xsrftoken": self.tool.xsrf_token,
            },
        )

        logger.debug(
            "%s %s %d",
            response.request.method,
            response.url,
            response.status_code,
        )

        try:
            data = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as ex:
            logger.warning(
                "Ответ на отклик с тестом не является JSON (status=%s, %s), result: %s",
                response.status_code,
                response.url,
                shorten(response.text, 300),
            )
            raise ValueError(
                "Эндпоинт отклика на тест вернул не-JSON ответ "
                "(возможно, перенаправление на капчу или форму)."
            ) from ex

        if isinstance(data, dict):
            data["_http_status"] = response.status_code

        return data

    def _parse_site(self, url: str) -> dict[str, Any]:
        with self.tool.session.get(url, timeout=10) as r:
            val = lambda m: html.unescape(m.group(1)) if m else ""

            title = val(re.search(r"<title>(.*?)</title>", r.text, re.I | re.S))
            description = val(
                re.search(
                    r'<meta name="description" content="(.*?)"', r.text, re.I
                )
            )
            generator = val(
                re.search(
                    r'<meta name="generator" content="(.*?)"', r.text, re.I
                )
            )

            # Поиск email
            emails = set(
                m.group(0)
                # Исключение всякого мусора типа energy-software-slider-225x225@2x.png
                for m in re.finditer(
                    r"\b[a-z][a-z0-9_.-]+@([a-z0-9][a-z0-9-]+)(?!\.(?:png|jpe?g|bmp|gif|ico|js|css)\b)(\.[a-z0-9][a-z0-9-]+)+\b",
                    r.text,
                )
            )

            return {
                "title": title,
                "description": description,
                "generator": generator,
                "emails": list(emails),
                "server_name": r.headers.get("Server"),
                "powered_by": r.headers.get("X-Powered-By"),
                # Не работает, если отключена проверка сертификата
                "ip_address": r.raw._connection.sock.getpeername()[0]
                if r.raw._connection
                else None,
            }

    # Слишком тормознутая... Толи российские айпи заблокированы
    def _get_subdomains(self, url: str) -> set[str]:
        domain = urlparse(url).netloc
        r = self.tool.session.get(
            "https://crt.sh",
            params={"q": domain, "output": "json"},
            timeout=30,
        )

        r.raise_for_status()

        return set(
            item
            for item in chain.from_iterable(
                item["name_value"].split() for item in r.json()
            )
            if not item.startswith("*.")
        )

    def _get_search_params(self, page: int) -> dict:
        params = {
            "page": page,
            "per_page": self.per_page,
        }
        if self.order_by:
            params |= {"order_by": self.order_by}
        if self.search:
            params["text"] = self.search
        if self.schedule:
            params["schedule"] = self.schedule
        if self.work_format:
            params["work_format"] = list(self.work_format)
        if self.experience:
            params["experience"] = self.experience
        if self.currency:
            params["currency"] = self.currency
        if self.salary:
            params["salary"] = self.salary
        if self.period:
            params["period"] = self.period
        if self.date_from:
            params["date_from"] = self.date_from
        if self.date_to:
            params["date_to"] = self.date_to
        if self.top_lat:
            params["top_lat"] = self.top_lat
        if self.bottom_lat:
            params["bottom_lat"] = self.bottom_lat
        if self.left_lng:
            params["left_lng"] = self.left_lng
        if self.right_lng:
            params["right_lng"] = self.right_lng
        if self.sort_point_lat:
            params["sort_point_lat"] = self.sort_point_lat
        if self.sort_point_lng:
            params["sort_point_lng"] = self.sort_point_lng
        if self.search_field:
            params["search_field"] = list(self.search_field)
        if self.employment:
            params["employment"] = list(self.employment)
        if self.area:
            params["area"] = list(self.area)
        if self.metro:
            params["metro"] = list(self.metro)
        if self.professional_role:
            params["professional_role"] = list(self.professional_role)
        if self.industry:
            params["industry"] = list(self.industry)
        if self.employer_id:
            params["employer_id"] = list(self.employer_id)
        if self.excluded_employer_id:
            params["excluded_employer_id"] = list(self.excluded_employer_id)
        if self.label:
            params["label"] = list(self.label)
        if self.only_with_salary:
            params["only_with_salary"] = bool2str(self.only_with_salary)
        # if self.clusters:
        #     params["clusters"] = bool2str(self.clusters)
        if self.no_magic:
            params["no_magic"] = bool2str(self.no_magic)
        if self.premium:
            params["premium"] = bool2str(self.premium)
        # if self.responses_count_enabled is not None:
        #     params["responses_count_enabled"] = bool2str(self.responses_count_enabled)

        return params

    def _get_vacancies(
        self, resume_id: str | None = None
    ) -> Iterator[SearchVacancy]:
        similar = f"/resumes/{resume_id}/similar_vacancies"
        if not self.search:
            yield from self._paginate(similar, {})
            return

        # Сначала самое релевантное: рекомендации hh под это резюме, затем
        # поиск в приоритетном регионе, затем везде. Повторы пропускаем
        sources: list[tuple[str, dict[str, Any]]] = []
        priority_area = getattr(self, "priority_area", None)
        if self.area:
            priority_area = None
        if getattr(self, "recommended_first", False) and resume_id:
            # Рекомендации — тоже только из приоритетного региона, иначе hh
            # подмешивает вакансии из других стран раньше своих
            recommended = {"text": None, "search_field": None}
            if priority_area:
                recommended["area"] = list(priority_area)
            sources.append((similar, recommended))
        if priority_area:
            sources.append(("/vacancies", {"area": list(priority_area)}))
        other_areas = getattr(self, "other_areas", None) or "all"
        if not priority_area or other_areas == "all":
            sources.append(("/vacancies", {}))
        elif other_areas == "remote":
            # Из других стран интересна только удалёнка
            sources.append(("/vacancies", {"work_format": ["REMOTE"]}))

        seen: set[str] = set()
        for url, overrides in sources:
            for vacancy in self._paginate(url, overrides):
                if vacancy["id"] in seen:
                    continue
                seen.add(vacancy["id"])
                yield vacancy

    def _paginate(
        self, url: str, overrides: dict[str, Any]
    ) -> Iterator[SearchVacancy]:
        for page in range(self.total_pages):
            logger.debug("Загружаем вакансии: %s, страница %d", url, page + 1)
            params = self._get_search_params(page)
            for key, value in overrides.items():
                if value is None:
                    params.pop(key, None)
                else:
                    params[key] = value

            res: PaginatedItems[SearchVacancy] = self.api_client.get(url, params)
            logger.debug(f"Количество вакансий: {res['found']}")

            if not res["items"]:
                return

            yield from res["items"]

            if page >= res["pages"] - 1:
                return

    def _vacancy_summary(self, vacancy: SearchVacancy) -> str:
        snippet = vacancy.get("snippet") or {}
        return " ".join(
            filter(
                None,
                [
                    vacancy.get("name"),
                    snippet.get("requirement"),
                    snippet.get("responsibility"),
                ],
            )
        )

    def _record_sent(self) -> None:
        """Время отклика: hh считает лимит 200 за скользящие 24 часа."""
        try:
            with (self.tool.config_path / SENT_TIMES_FILENAME).open("a") as fp:
                fp.write(f"{time.time():.0f}\n")
        except (OSError, TypeError, AttributeError) as ex:
            logger.debug("Не удалось записать время отклика: %s", ex)

    def _prepare_captcha(self, captcha_url: str | None) -> None:
        """Картинка капчи для владельца: её пришлёт Telegram-бот."""
        if not captcha_url:
            return
        try:
            hhcaptcha.prepare(self.tool, captcha_url)
        except Exception as ex:
            logger.warning("Не удалось подготовить картинку капчи: %s", ex)

    def _forget_vacancies(self) -> None:
        """Кэш живёт только пока идёт работа над одной вакансией."""
        self.__dict__.pop("_vacancy_cache", None)

    def _full_vacancy(self, vacancy_id: str | int) -> dict:
        """Вакансия целиком, не больше одного запроса на вакансию за прогон.

        hh включает капчу, если открывать вакансии слишком часто, поэтому
        между запросами — пауза, а капча останавливает прогон (VacancyCaptcha),
        а не превращается в «пустую» вакансию.
        """
        vacancy_id = str(vacancy_id)
        cache = self.__dict__.setdefault("_vacancy_cache", {})
        if vacancy_id in cache:
            return cache[vacancy_id]
        low, high = self.vacancy_fetch_delay
        if high > 0:
            time.sleep(random.uniform(low, high))
        try:
            full = self.api_client.get(f"/vacancies/{vacancy_id}")
        except CaptchaRequired as ex:
            raise VacancyCaptcha(ex.captcha_url) from ex
        if len(cache) >= VACANCY_CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[vacancy_id] = full
        return full

    def _vacancy_description(self, vacancy: SearchVacancy) -> str | None:
        """Описание и ключевые навыки; None — вакансию получить не удалось."""
        try:
            full = self._full_vacancy(vacancy["id"])
        except ApiError as ex:
            logger.warning("Не удалось получить вакансию %s: %s", vacancy["id"], ex)
            return None
        return (
            strip_tags(full.get("description") or "")
            + " "
            + " ".join(s.get("name", "") for s in full.get("key_skills") or [])
        )

    def _is_included(self, vacancy: SearchVacancy) -> bool:
        if not self.included_filter:
            return True
        pattern = re.compile(self.included_filter, re.IGNORECASE)
        if pattern.search(self._vacancy_summary(vacancy)):
            return True
        if "name" in (getattr(self, "search_field", None) or []):
            # Ищем по названию — в описание не лезем: каждый лишний просмотр
            # вакансии приближает капчу hh
            return False
        description = self._vacancy_description(vacancy)
        # Не смогли проверить — не отбрасываем: поиск hh уже нашёл её по словам
        return description is None or bool(pattern.search(description))

    def _is_excluded(self, vacancy: SearchVacancy) -> bool:
        if not self.excluded_filter:
            return False

        snippet = vacancy.get("snippet", {})
        vacancy_summary = " ".join(
            filter(
                None,
                [
                    vacancy.get("name"),
                    snippet.get("requirement"),
                    snippet.get("responsibility"),
                ],
            )
        )

        logger.debug(vacancy_summary)

        excluded_pat: re.Pattern = re.compile(
            self.excluded_filter, re.IGNORECASE
        )

        if "name" in (getattr(self, "search_field", None) or []):
            # Ищем по названию — и стоп-слова смотрим в названии и компании:
            # в описаниях «Java», «junior», «Сбер» встречаются мимоходом
            # («будете менторить junior») и отсекали подходящие вакансии
            title = " ".join(
                filter(
                    None,
                    [vacancy.get("name"), (vacancy.get("employer") or {}).get("name")],
                )
            )
            return bool(excluded_pat.search(title))

        if excluded_pat.search(vacancy_summary):
            return True

        # Полный текст — только если сниппет не сработал. Берём из API:
        # страница hh.ru/vacancy/… с сервера часто отвечает 403 (антибот)
        description = self._vacancy_description(vacancy)
        if description is None:
            return False
        logger.debug(description[:2047])
        return bool(excluded_pat.search(description))

    def _is_vacancy_already_skipped(
        self, vacancy: SearchVacancy, resume_id: str | None = None
    ) -> bool:
        try:
            vacancy_id = vacancy["id"]

            if resume_id:
                if any(
                    self.tool.storage.skipped_vacancies.find(
                        resume_id=resume_id,
                        vacancy_id=vacancy_id,
                    )
                ):
                    return True

            return any(
                self.tool.storage.skipped_vacancies.find(
                    resume_id="",
                    vacancy_id=vacancy_id,
                )
            )

        except Exception:
            return False

    def _save_skipped_vacancy(
        self, vacancy: SearchVacancy, reason: str, resume_id: str | None = None
    ) -> None:
        try:
            employer = vacancy.get("employer", {})
            self.tool.storage.skipped_vacancies.save(
                {
                    "resume_id": resume_id or "",
                    "vacancy_id": vacancy["id"],
                    "reason": reason,
                    "alternate_url": vacancy.get("alternate_url"),
                    "name": vacancy.get("name"),
                    "employer_name": employer.get("name"),
                    "created_at": datetime.now(),
                }
            )
        except Exception as ex:
            logger.warning(f"Не удалось сохранить пропущенную вакансию: {ex}")
