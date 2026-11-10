from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from hh_applicant_tool.utils import capsolver


def make_response(data):
    response = MagicMock()
    response.json.return_value = data
    response.status_code = 200
    return response


def test_solve_returns_text(monkeypatch):
    post = MagicMock(
        return_value=make_response(
            {"errorId": 0, "status": "ready", "solution": {"text": "abc12"}}
        )
    )
    monkeypatch.setattr(capsolver.requests, "post", post)

    assert capsolver.solve_image("CAP-key", b"PNG") == "abc12"
    payload = post.call_args.kwargs["json"]
    assert payload["clientKey"] == "CAP-key"
    assert payload["task"]["type"] == "ImageToTextTask"
    assert payload["task"]["body"] == "UE5H"


def test_zero_balance_is_not_retried(monkeypatch):
    post = MagicMock(
        return_value=make_response(
            {"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE"}
        )
    )
    monkeypatch.setattr(capsolver.requests, "post", post)

    with pytest.raises(capsolver.CapSolverError, match="ERROR_ZERO_BALANCE"):
        capsolver.solve_image("CAP-key", b"PNG", max_retries=3)
    assert post.call_count == 1


def test_network_error_is_retried(monkeypatch):
    post = MagicMock(side_effect=requests.ConnectionError("boom"))
    monkeypatch.setattr(capsolver.requests, "post", post)
    monkeypatch.setattr(capsolver.time, "sleep", lambda _: None)

    with pytest.raises(capsolver.CapSolverError):
        capsolver.solve_image("CAP-key", b"PNG", max_retries=2)
    assert post.call_count == 2


def test_polls_when_task_is_processing(monkeypatch):
    create = MagicMock(
        return_value=make_response(
            {"errorId": 0, "status": "processing", "taskId": "t1"}
        )
    )
    result = MagicMock(
        return_value=make_response(
            {"errorId": 0, "status": "ready", "solution": {"text": "ok"}}
        )
    )

    def post(url, **kwargs):
        return create() if url == capsolver.CREATE_TASK_URL else result()

    monkeypatch.setattr(capsolver.requests, "post", post)
    monkeypatch.setattr(capsolver.time, "sleep", lambda _: None)

    assert capsolver.solve_image("CAP-key", b"PNG") == "ok"


def test_empty_solution_is_error(monkeypatch):
    post = MagicMock(
        return_value=make_response({"errorId": 0, "status": "ready", "solution": {}})
    )
    monkeypatch.setattr(capsolver.requests, "post", post)
    monkeypatch.setattr(capsolver.time, "sleep", lambda _: None)

    with pytest.raises(capsolver.CapSolverError, match="пустой ответ"):
        capsolver.solve_image("CAP-key", b"PNG", max_retries=2)
