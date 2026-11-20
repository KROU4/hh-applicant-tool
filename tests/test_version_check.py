from __future__ import annotations

import logging
from types import SimpleNamespace

from hh_applicant_tool.utils import mixins


class Settings:
    def __init__(self, values: dict) -> None:
        self.values = values

    def get_value(self, key, default=None):
        return self.values.get(key, default)

    def set_value(self, key, value):
        self.values[key] = value


def make_tool(**values):
    return SimpleNamespace(storage=SimpleNamespace(settings=Settings(values)))


def test_outdated_version_warns_once_a_day(monkeypatch, caplog):
    monkeypatch.setattr(mixins, "get_package_version", lambda: "1.0.0")
    tool = make_tool(
        _latest_version="2.0.0",
        _next_version_check=10**12,
        _next_version_warn=0,
    )

    with caplog.at_level(logging.WARNING):
        mixins.VersionChecker._check_version(tool)
        mixins.VersionChecker._check_version(tool)

    assert sum("УСТАРЕЛА" in r.message for r in caplog.records) == 1
    assert tool.storage.settings.get_value("_next_version_warn") > 0
