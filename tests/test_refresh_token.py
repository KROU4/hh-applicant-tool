from __future__ import annotations

from hh_applicant_tool.operations import refresh_token


class AuthExpired(Exception):
    pass


def make_tool(raises: bool):
    class Tool:
        def get_redirect_config(self, url):
            if raises:
                raise AuthExpired("Авторизация истекла требуется новая!")
            return {"redirectConfig": {}}

    return Tool()


def test_web_session_expired_marker(capsys):
    refresh_token.Operation._check_web_session(make_tool(True))
    assert refresh_token.WEB_SESSION_EXPIRED_MARKER in capsys.readouterr().out


def test_web_session_ok_is_silent(capsys):
    refresh_token.Operation._check_web_session(make_tool(False))
    assert capsys.readouterr().out == ""


def test_other_errors_do_not_mark_session_dead():
    class Tool:
        def get_redirect_config(self, url):
            raise OSError("network down")

    refresh_token.Operation._check_web_session(Tool())
