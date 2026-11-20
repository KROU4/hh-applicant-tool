from __future__ import annotations

from types import SimpleNamespace

from hh_applicant_tool.operations import refresh_token


def make_tool(html: str):
    class Tool:
        session = SimpleNamespace(
            get=lambda url: SimpleNamespace(text=html)
        )

    return Tool()


def test_web_session_expired_marker(capsys):
    refresh_token.Operation._check_web_session(make_tool("  login: null\n"))
    assert refresh_token.WEB_SESSION_EXPIRED_MARKER in capsys.readouterr().out


def test_web_session_ok_is_silent(capsys):
    refresh_token.Operation._check_web_session(
        make_tool('  login: "user@example.com"\n')
    )
    assert capsys.readouterr().out == ""


def test_other_errors_do_not_mark_session_dead(capsys):
    class Tool:
        class session:
            @staticmethod
            def get(url):
                raise OSError("network down")

    refresh_token.Operation._check_web_session(Tool())
    assert capsys.readouterr().out == ""
