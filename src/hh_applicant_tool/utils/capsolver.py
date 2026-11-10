"""Распознавание капчи через CapSolver (ImageToTextTask).

Документация: https://docs.capsolver.com/en/guide/recognition/ImageToTextTask/

Сервис этих капч не решает токенами, а возвращает текст прямо в ответе на
createTask. Поэтому опрос getTaskResult нужен только на случай, если задача
всё же не успела выполниться.
"""

from __future__ import annotations

import base64
import logging
import random
import time
from typing import Any

import requests

logger = logging.getLogger(__package__)

CREATE_TASK_URL = "https://api.capsolver.com/createTask"
GET_TASK_RESULT_URL = "https://api.capsolver.com/getTaskResult"

# Ошибки, после которых повторять бессмысленно
_FATAL_CODES = {
    "ERROR_KEY_DOES_NOT_EXIST",
    "ERROR_ZERO_BALANCE",
    "ERROR_NO_SLOT_AVAILABLE",
    "ERROR_INVALID_TASK_DATA",
    "ERROR_TASK_NOT_SUPPORTED",
}


class CapSolverError(Exception):
    pass


def _request(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    response = requests.post(
        url, json=payload, timeout=timeout, proxies=proxies
    )
    try:
        data = response.json()
    except ValueError as ex:
        raise CapSolverError(
            f"CapSolver вернул не JSON: HTTP {response.status_code}"
        ) from ex
    if not isinstance(data, dict):
        raise CapSolverError("CapSolver вернул неожиданный ответ")
    return data


def _raise_for_error(data: dict[str, Any]) -> None:
    if data.get("errorId", 0) == 0:
        return
    code = str(data.get("errorCode") or "ERROR_UNKNOWN")
    text = str(data.get("errorDescription") or code)
    raise CapSolverError(f"{code}: {text}")


def solve_image(
    api_key: str,
    image_data: bytes,
    *,
    website_url: str = "https://hh.ru/account/captcha",
    module: str = "common",
    timeout: float = 60.0,
    poll_interval: float = 1.0,
    max_retries: int = 3,
    proxy_url: str | None = None,
) -> str:
    """Возвращает текст с картинки.

    Ошибки баланса/ключа не повторяются, сетевые сбои и перегрузка сервиса —
    повторяются с паузой. Пустой ответ считается ошибкой.
    """
    if not api_key:
        raise CapSolverError("Не задан 'capsolver.api_key'")

    payload = {
        "clientKey": api_key,
        "task": {
            "type": "ImageToTextTask",
            "websiteURL": website_url,
            "module": module,
            "body": base64.b64encode(image_data).decode("ascii"),
        },
    }

    last_error = "нет ответа"
    for attempt in range(1, max_retries + 1):
        try:
            data = _request(CREATE_TASK_URL, payload, timeout, proxy_url)
        except (requests.RequestException, CapSolverError) as ex:
            last_error = str(ex)
            logger.warning(
                "CapSolver: попытка %d/%d не удалась: %s",
                attempt,
                max_retries,
                ex,
            )
            time.sleep(min(2.0**attempt, 10.0) + random.uniform(0, 1.0))
            continue

        try:
            _raise_for_error(data)
        except CapSolverError as ex:
            code = str(data.get("errorCode") or "")
            if code in _FATAL_CODES or attempt >= max_retries:
                raise
            last_error = str(ex)
            logger.warning("CapSolver: попытка %d: %s", attempt, ex)
            time.sleep(min(2.0**attempt, 10.0) + random.uniform(0, 1.0))
            continue

        task_id = data.get("taskId")
        if data.get("status") != "ready" and task_id:
            for _ in range(int(timeout / max(poll_interval, 0.5))):
                time.sleep(poll_interval)
                result = _request(
                    GET_TASK_RESULT_URL,
                    {"clientKey": api_key, "taskId": task_id},
                    timeout,
                    proxy_url,
                )
                _raise_for_error(result)
                if result.get("status") == "ready":
                    data = result
                    break
            else:
                last_error = "истекло время ожидания"
                continue

        solution = data.get("solution") or {}
        text = solution.get("text") or (solution.get("answers") or [None])[0]
        if text:
            return str(text).strip()
        last_error = "пустой ответ"
        logger.warning("CapSolver: пустой ответ, попытка %d", attempt)
        time.sleep(poll_interval)

    raise CapSolverError(f"Капчу через CapSolver решить не удалось: {last_error}")


def get_balance(api_key: str, timeout: float = 15.0) -> float:
    """Текущий баланс аккаунта CapSolver (для проверки настройки)."""
    data = _request(
        "https://api.capsolver.com/getBalance", {"clientKey": api_key}, timeout
    )
    _raise_for_error(data)
    return float(data.get("balance") or 0.0)
