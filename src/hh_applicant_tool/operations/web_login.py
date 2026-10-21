"""Вход на hh.ru по коду из SMS или почты — без браузера, по шагам.

Повторяет вход в мобильном приложении (как authorize в upstream
s3rgeym/hh-applicant-tool): страница OAuth-авторизации → запрос кода →
подтверждение кода → редирект на hhandroid://oauthresponse?code=… → токен.
Заодно в сессии появляются cookies сайта, без которых не работают отклики
на вакансии с тестом.

Шаги — отдельные запуски, чтобы Telegram-бот мог между ними спросить у
владельца код или ответ на капчу. Состояние хранится в каталоге профиля.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

HH_URL = "https://hh.ru"
PENDING_FILENAME = "login_pending.json"
IMAGE_FILENAME = "login_captcha.png"
# Коды выхода для Telegram-бота
CAPTCHA_EXIT_CODE = 3
WRONG_CAPTCHA_EXIT_CODE = 4
# Заголовки формы входа сайта
LOGIN_HEADERS = {"X-Hhtmfrom": "", "X-Hhtmsource": "account_login"}
# Незаконченный вход живёт не дольше: код из SMS всё равно протухнет
PENDING_TTL = 30 * 60
MAX_REDIRECTS = 8


class LoginError(Exception):
    pass


class Namespace(BaseNamespace):
    request: str | None
    captcha: str | None
    code: str | None


def pending_path(config_path: Path) -> Path:
    return config_path / PENDING_FILENAME


def image_path(config_path: Path) -> Path:
    return config_path / IMAGE_FILENAME


def load_pending(config_path: Path) -> dict[str, Any] | None:
    try:
        info = json.loads(pending_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - info.get("created_at", 0) > PENDING_TTL:
        return None
    return info


def clear(config_path: Path) -> None:
    pending_path(config_path).unlink(missing_ok=True)
    image_path(config_path).unlink(missing_ok=True)


def normalize_phone(phone: str) -> str:
    phone = re.sub(r"\D", "", phone)
    if len(phone) == 11 and phone[0] == "8":
        phone = "7" + phone[1:]
    return phone


def trust_flags(pubkey: str, payload: dict[str, Any] | None = None) -> str:
    """loginTrustFlags: JSON, зашифрованный публичным ключом со страницы входа."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    data = {**(payload or {}), "ts": int(time.time() * 1000)}
    plaintext = json.dumps(data, separators=(",", ":")).encode()
    key = serialization.load_pem_public_key(pubkey.encode())
    ciphertext = key.encrypt(  # type: ignore[union-attr]
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


class Operation(BaseOperation):
    """Вход на hh.ru по коду из SMS/почты: --request, затем --code."""

    __aliases__ = ("login-code",)

    tool: HHApplicantTool

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--request", metavar="LOGIN", help="Телефон или email: выслать код")
        group.add_argument("--captcha", metavar="TEXT", help="Ответ на капчу при входе")
        group.add_argument("--code", help="Код из SMS или письма")

    def run(self, tool: HHApplicantTool, args: Namespace) -> int | None:
        self.tool = tool
        try:
            if args.request:
                return self.request_code(args.request)
            if args.captcha:
                return self.answer_captcha(args.captcha)
            return self.confirm_code(args.code or "")
        except LoginError as ex:
            print(f"❌ {ex}")
            return 1

    # ------------------------------------------------------------- шаги

    def request_code(self, login: str) -> int:
        login = login.strip()
        is_email = "@" in login
        if not is_email:
            login = normalize_phone(login)
        if not login:
            raise LoginError("Пустой логин")
        clear(self.tool.config_path)
        # Старый _xsrf из cookies.txt может не совпасть с тем, что выдаст
        # страница входа: сервер сверяет заголовок именно с новым
        jar = self.tool.session.cookies
        for cookie in [c for c in jar if c.name == "_xsrf"]:
            jar.clear(cookie.domain, cookie.path, cookie.name)

        r, auth_code = self._follow(self._authorize_url())
        if auth_code:
            # Сайт и так помнит вход — сразу получаем токен
            return self._finish(auth_code)
        redirect_url = r.url
        backurl = (parse_qs(urlsplit(redirect_url).query).get("backurl") or [""])[0]
        if not backurl:
            raise LoginError(f"Неожиданная страница входа: {redirect_url[:150]}")
        state = self.tool.parse_redirect_config(r, check_auth=False)
        pubkey = state.get("loginTrustFlagsSecret")
        if not pubkey:
            raise LoginError("На странице входа нет ключа loginTrustFlagsSecret")

        pending = {
            "login": login,
            "is_email": is_email,
            "redirect_url": redirect_url,
            "backurl": backurl,
            "pubkey": pubkey,
            "created_at": time.time(),
        }
        # Человек не нажимает «Получить код» в ту же секунду
        time.sleep(random.uniform(3.0, 6.0))
        return self._send_otp(pending)

    def answer_captcha(self, text: str) -> int:
        pending = self._pending("captcha")
        return self._send_otp(
            pending,
            {
                "captchaState": pending["captcha_state"],
                "captchaText": text.strip(),
                "captchaKey": pending["captcha_key"],
            },
        )

    def confirm_code(self, code: str) -> int:
        pending = self._pending("code")
        code = re.sub(r"\s", "", code)
        if not code:
            raise LoginError("Пустой код")
        redirect_url = pending["redirect_url"]
        res = self._xhr(
            urljoin(redirect_url, "/account/login/by_code"),
            {
                "username": pending["login"],
                "code": code,
                "backurl": redirect_url,
                "operationType": "applicant_otp_auth",
                "isApplicantSignup": "false",
                "remember": "true",
                "loginTrustFlags": trust_flags(pending["pubkey"]),
                "authScenario": "APPLICANT_AUTH",
            },
            redirect_url,
        )
        if not res.get("success"):
            error = (res.get("error") or {}).get("key") or f"HTTP {res.get('http_status')}"
            # Шаг не сбрасываем: код можно прислать ещё раз
            raise LoginError(f"hh не принял код ({error}). Проверьте код и пришлите снова")
        if not (res.get("verification") or {}).get("success"):
            raise LoginError("hh не подтвердил вход")

        # Код принят — дальше повторять его бессмысленно
        clear(self.tool.config_path)
        r, auth_code = self._follow(urljoin(redirect_url, pending["backurl"]), redirect_url)
        if not auth_code:
            raise LoginError(
                f"Не получили код авторизации (HTTP {r.status_code}) — начните вход заново"
            )
        return self._finish(auth_code)

    def _finish(self, auth_code: str) -> int:
        api_client = self.tool.api_client
        try:
            token = api_client.oauth_client.authenticate(auth_code)
        except Exception as ex:
            raise LoginError(f"hh не выдал токен ({ex}) — начните вход заново") from ex
        api_client.handle_access_token(token)
        # Токен и cookies сохранит main после операции
        clear(self.tool.config_path)
        print("🎉 Вход выполнен: токен и cookies сайта обновлены")
        return 0

    def _follow(self, url: str, referer: str | None = None) -> tuple[Any, str | None]:
        """Переходы по редиректам вручную: последний — hhandroid://…?code=.

        requests не умеет схему hhandroid и упал бы на ней, поэтому код
        авторизации забираем из Location сами.
        """
        headers = {"Referer": referer} if referer else {}
        for _ in range(MAX_REDIRECTS):
            r = self.tool.session.get(url, headers=headers, allow_redirects=False)
            location = r.headers.get("location") or r.headers.get("Location") or ""
            if r.status_code not in (301, 302, 303, 307, 308) or not location:
                return r, None
            parts = urlsplit(location)
            if parts.scheme == "hhandroid":
                code = (parse_qs(parts.query).get("code") or [""])[0]
                if not code:
                    raise LoginError("В ответе hh нет кода авторизации")
                return r, code
            headers = {"Referer": url}
            url = urljoin(url, location)
        raise LoginError("Слишком много переадресаций при входе")

    # --------------------------------------------------------- внутреннее

    def _authorize_url(self) -> str:
        oauth = self.tool.api_client.oauth_client
        query = urlencode(
            {
                "client_id": oauth.client_id,
                "response_type": "code",
                # Без этого hh может предложить войти работодателем
                "force_login": "true",
                "role": "applicant",
                "force_role": "true",
                "skip_choose_account": "true",
            }
        )
        return oauth.resolve_url(f"/authorize?{query}")

    def _pending(self, stage: str) -> dict[str, Any]:
        pending = load_pending(self.tool.config_path)
        if not pending:
            raise LoginError("Вход не начат или устарел — начните заново")
        if pending.get("stage") != stage:
            raise LoginError(f"Сейчас ожидается другой шаг входа ({pending.get('stage')})")
        return pending

    def _save(self, pending: dict[str, Any]) -> None:
        path = pending_path(self.tool.config_path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _xhr(self, url: str, data: dict[str, Any], referer: str) -> dict[str, Any]:
        xsrf = self.tool._cookie_value("_xsrf") or self.tool.xsrf_token
        r = self.tool.session.post(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-XSRFToken": xsrf,
                "Origin": HH_URL,
                "Referer": referer,
                **LOGIN_HEADERS,
            },
            allow_redirects=False,
            timeout=(10, 30),
        )
        logger.debug("%s %s %s", r.request.method, url, r.status_code)
        try:
            res = r.json()
        except ValueError:
            res = {"text": r.text[:300]}
        if not isinstance(res, dict):
            res = {"data": res}
        res.setdefault("http_status", r.status_code)
        return res

    def _send_otp(
        self, pending: dict[str, Any], captcha: dict[str, str] | None = None
    ) -> int:
        params = {
            "login": pending["login"],
            "otpType": "email" if pending["is_email"] else "phone",
            "operationType": "applicant_otp_auth",
            "authScenario": "APPLICANT_AUTH",
            "formatPhone": "true",
            "isSignupPage": "false",
            "loginTrustFlags": trust_flags(
                pending["pubkey"], {"emailOrPhone": {"paste": False, "suggest": True}}
            ),
            "captchaText": "",
            **(captcha or {}),
        }
        redirect_url = pending["redirect_url"]
        res = self._xhr(urljoin(redirect_url, "/account/otp_generate"), params, redirect_url)

        if captcha_state := (res.get("hhcaptcha") or {}).get("captchaState"):
            pending = {**pending, "stage": "captcha", "captcha_state": captcha_state}
            pending["captcha_key"] = self._captcha_image(redirect_url)
            self._save(pending)
            print("🔐 hh просит капчу перед отправкой кода")
            return WRONG_CAPTCHA_EXIT_CODE if captcha else CAPTCHA_EXIT_CODE

        if res.get("success"):
            self._save({**pending, "stage": "code"})
            where = "на почту" if pending["is_email"] else "по SMS"
            print(f"📨 hh отправил код {where}")
            return 0

        clear(self.tool.config_path)
        raise LoginError(f"hh не отправил код: {json.dumps(res, ensure_ascii=False)[:300]}")

    def _captcha_image(self, referer: str) -> str:
        res = self._xhr(f"{HH_URL}/captcha?lang=RU", {}, referer)
        key = res.get("key")
        if not key:
            raise LoginError("hh не выдал картинку капчи")
        picture = self.tool.session.get(
            f"{HH_URL}/captcha/picture",
            params={"key": key},
            headers={"Referer": referer},
        )
        picture.raise_for_status()
        image_path(self.tool.config_path).write_bytes(picture.content)
        return key
