from __future__ import annotations

import dataclasses
import json
import logging
import random
import time
from dataclasses import dataclass
from functools import cached_property
from threading import Lock
from typing import Any, Callable, Literal, TypeVar
from urllib.parse import urlencode, urljoin

import requests
from requests import Response, Session

from hh_applicant_tool.api.user_agent import generate_android_useragent

from . import errors
from .client_keys import (
    ANDROID_CLIENT_ID,
    ANDROID_CLIENT_SECRET,
)
from .datatypes import AccessToken

__all__ = ("ApiClient", "OAuthClient")

HH_API_URL = "https://api.hh.ru/"
HH_OAUTH_URL = "https://hh.ru/oauth/"
DEFAULT_DELAY = 0.345
# Разброс паузы между запросами: 0.3 = ±15 %. Ровные интервалы выдают бота.
DEFAULT_JITTER = 0.3
MAX_RETRIES = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_BACKOFF = 30.0

AllowedMethods = Literal["GET", "POST", "PUT", "DELETE"]
T = TypeVar("T")


logger = logging.getLogger(__package__)


# Thread-safe
@dataclass
class BaseClient:
    base_url: str
    _: dataclasses.KW_ONLY
    user_agent: str | None = None
    session: Session | None = None
    delay: float | None = None
    jitter: float = DEFAULT_JITTER
    max_retries: int = MAX_RETRIES
    _previous_request_time: float = 0.0

    def __post_init__(self) -> None:
        assert self.base_url.endswith("/"), "base_url must ends with /"
        self.delay = self.delay or DEFAULT_DELAY
        self.user_agent = self.user_agent or generate_android_useragent()

        # logger.debug(f"user agent: {self.user_agent}")

        if not self.session:
            logger.debug("create new session")
            self.session = requests.session()

        self.lock = Lock()

    @property
    def proxies(self):
        return self.session.proxies

    def _default_headers(self) -> dict[str, str]:
        # Как у официального Android-приложения
        return {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "X-Force-App-Access": "true",
            "X-HH-App-Active": "true",
        }

    def _retry_delay(self, attempt: int, response: Response | None) -> float:
        """Пауза перед повтором: Retry-After сервера или экспонента с джиттером."""
        retry_after = None
        if response is not None:
            try:
                retry_after = float(response.headers.get("Retry-After") or 0)
            except ValueError:
                retry_after = None
        base = retry_after or min(2.0**attempt, MAX_BACKOFF)
        return base + random.uniform(0, max(base * 0.2, 0.5))

    def request(
        self,
        method: AllowedMethods,
        endpoint: str,
        params: dict[str, Any] | None = None,
        delay: float | None = None,
        as_json: bool = False,
        **kwargs: Any,
    ) -> T:
        # Не знаю насколько это "правильно"
        assert method in AllowedMethods.__args__
        params = dict(params or {})
        params.update(kwargs)
        url = self.resolve_url(endpoint)
        has_body = method in ["POST", "PUT"]
        payload_key = ["data", "json"][as_json] if has_body else "params"

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            with self.lock:
                base_delay = self.delay if delay is None else delay
                # Джиттер делает интервалы неодинаковыми, как у человека
                if base_delay and self.jitter:
                    base_delay *= random.uniform(
                        1 - self.jitter / 2, 1 + self.jitter / 2
                    )
                # На серваке какая-то анти-DDOS система
                if (
                    wait := base_delay
                    - time.monotonic()
                    + self._previous_request_time
                ) > 0:
                    logger.debug("wait %fs before request", wait)
                    time.sleep(wait)
                try:
                    response = self.session.request(
                        method,
                        url,
                        **{payload_key: params},
                        headers=self._default_headers(),
                        allow_redirects=False,
                    )
                except (requests.ConnectionError, requests.Timeout) as ex:
                    last_exc = ex
                    response = None
                    logger.warning(
                        "(%d/%d) сбой соединения: %s %s: %s",
                        attempt,
                        self.max_retries,
                        method,
                        url,
                        ex,
                    )
                finally:
                    self._previous_request_time = time.monotonic()

            if response is not None:
                try:
                    # У этих лошков сервер не отдает Content-Length, а кривое API
                    # отдает пустые ответы, например, при отклике на вакансии,
                    # и мы не можем узнать содержит ли ответ тело
                    # 'Server': 'ddos-guard'
                    # ...
                    # 'Transfer-Encoding': 'chunked'
                    try:
                        rv = response.json() if response.text else {}
                    except json.JSONDecodeError as ex:
                        raise errors.BadResponse(
                            f"Can't decode JSON: {method} {url} ({response.status_code})"
                        ) from ex
                finally:
                    logger.debug(
                        "%d %s %s with params: %.1000s",
                        response.status_code,
                        method,
                        url,
                        params or "-",
                    )

                # Перегрузка и временные сбои сервера — повторяем тем же
                # запросом, выдерживая Retry-After, если он есть
                if (
                    response.status_code in RETRY_STATUSES
                    and attempt < self.max_retries
                ):
                    wait = self._retry_delay(attempt, response)
                    logger.warning(
                        "HH ответил %d, повтор через %.1fс (%d/%d)",
                        response.status_code,
                        wait,
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(wait)
                    continue

                errors.ApiError.raise_for_status(response, rv)
                assert 300 > response.status_code >= 200, (
                    f"Unexpected status code for {method} {url}: {response.status_code}"
                )
                return rv

            # Сбой соединения — пауза и повтор
            if attempt < self.max_retries:
                time.sleep(self._retry_delay(attempt, None))

        assert last_exc is not None
        raise last_exc

    def get(self, *args, **kwargs) -> T:
        return self.request("GET", *args, **kwargs)

    def post(self, *args, **kwargs) -> T:
        return self.request("POST", *args, **kwargs)

    def put(self, *args, **kwargs) -> T:
        return self.request("PUT", *args, **kwargs)

    def delete(self, *args, **kwargs) -> T:
        return self.request("DELETE", *args, **kwargs)

    def resolve_url(self, url: str) -> str:
        return urljoin(self.base_url, url.lstrip("/"))


@dataclass
class OAuthClient(BaseClient):
    client_id: str | None = None
    client_secret: str | None = None
    _: dataclasses.KW_ONLY
    base_url: str = HH_OAUTH_URL
    state: str = ""
    scope: str = ""
    redirect_uri: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.client_id = self.client_id or ANDROID_CLIENT_ID
        self.client_secret = self.client_secret or ANDROID_CLIENT_SECRET

    @property
    def authorize_url(self) -> str:
        params = dict(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            response_type="code",
            scope=self.scope,
            state=self.state,
        )
        params_qs = urlencode({k: v for k, v in params.items() if v})
        return self.resolve_url(f"/authorize?{params_qs}")

    def request_access_token(
        self, endpoint: str, params: dict[str, Any] | None = None, **kw: Any
    ) -> AccessToken:
        tok = self.post(endpoint, params, **kw)
        return {
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token"),
            "access_expires_at": int(time.time()) + tok.pop("expires_in", 0),
        }

    def authenticate(self, code: str) -> AccessToken:
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
        return self.request_access_token("/token", params)

    def refresh_access_token(self, refresh_token: str) -> AccessToken:
        # refresh_token можно использовать только один раз и только по
        # истечению срока действия access_token.
        return self.request_access_token(
            "/token",
            grant_type="refresh_token",
            refresh_token=refresh_token,
        )


@dataclass
class ApiClient(BaseClient):
    # Например, для просмотра информации о компании токен не нужен
    access_token: str | None = None
    refresh_token: str | None = None
    access_expires_at: int = 0
    _: dataclasses.KW_ONLY
    client_id: str | None = None
    client_secret: str | None = None
    base_url: str = HH_API_URL
    # Автоматическое решение капчи: callback получает URL капчи и возвращает,
    # принял ли hh ответ. Пока True — запрос повторяется.
    captcha_handler: Callable[[str], bool] | None = None
    max_captcha_attempts: int = 3
    _captcha_lock: Lock = dataclasses.field(
        default_factory=Lock, init=False, repr=False
    )

    @property
    def is_access_expired(self) -> bool:
        return time.time() >= (self.access_expires_at or 0)

    @cached_property
    def oauth_client(self) -> OAuthClient:
        return OAuthClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            user_agent=self.user_agent,
            session=self.session,
        )

    def _default_headers(
        self,
    ) -> dict[str, str]:
        headers = super()._default_headers()
        if not self.access_token:
            return headers
        # Это очень интересно, что access token'ы начинаются с USER, т.е. API может содержать какую-то уязвимость, связанную с этим
        assert self.access_token.startswith("USER")
        return headers | {"authorization": f"Bearer {self.access_token}"}

    # Реализовано автоматическое обновление токена
    def request(
        self,
        method: AllowedMethods,
        endpoint: str,
        params: dict[str, Any] | None = None,
        delay: float | None = None,
        as_json: bool = False,
        **kwargs: Any,
    ) -> T:
        def do_request():
            return BaseClient.request(
                self, method, endpoint, params, delay, as_json, **kwargs
            )

        captcha_tries = 0
        while True:
            try:
                return do_request()
            # Капчу пытаемся решить автоматически и повторить тот же запрос
            except errors.CaptchaRequired as ex:
                handler = self.captcha_handler
                if not handler or not ex.captcha_url:
                    raise
                with self._captcha_lock:
                    if captcha_tries >= self.max_captcha_attempts:
                        raise
                    captcha_tries += 1
                    logger.info(
                        "hh запросил капчу, попытка %d/%d",
                        captcha_tries,
                        self.max_captcha_attempts,
                    )
                    accepted = handler(ex.captcha_url)
                if not accepted:
                    raise
            # TODO: добавить класс для ошибок типа AccessTokenExpired
            except errors.Forbidden as ex:
                if not self.is_access_expired or not self.refresh_token:
                    raise ex
                logger.info("try to refresh access_token")
                # Пробуем обновить токен
                self.refresh_access_token()
                # И повторно отправляем запрос
                return do_request()

    def handle_access_token(self, token: AccessToken) -> None:
        for field in ("access_token", "refresh_token", "access_expires_at"):
            if field in token and hasattr(self, field):
                setattr(self, field, token[field])

    def refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise ValueError("Refresh token required.")
        token = self.oauth_client.refresh_access_token(self.refresh_token)
        self.handle_access_token(token)

    def get_access_token(self) -> AccessToken:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_at": self.access_expires_at,
        }
