from __future__ import annotations

import json

from hh_applicant_tool.utils import Config


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "config.json"
    Config(path).save(a=1)
    Config(path).save(b=2)

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_save_token_keeps_changes_made_by_other_processes(tmp_path):
    from types import SimpleNamespace

    from hh_applicant_tool.main import HHApplicantTool

    path = tmp_path / "config.json"
    Config(path).save(token={"access_token": "old"})

    tool = HHApplicantTool()
    tool.config_dir = tmp_path
    tool.profile_id = None
    stale = tool.config  # снимок до изменений «другого процесса»
    assert stale["token"]["access_token"] == "old"

    # Пока операция работала, бот записал ключ OpenRouter
    Config(path).save(openrouter={"api_key": "sk-or-x"})

    tool.__dict__["api_client"] = SimpleNamespace(
        access_token="new",
        get_access_token=lambda: {"access_token": "new"},
    )
    assert tool.save_token()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["token"] == {"access_token": "new"}
    assert saved["openrouter"] == {"api_key": "sk-or-x"}


def test_device_user_agent_is_generated_once_per_profile(tmp_path):
    from hh_applicant_tool.main import HHApplicantTool

    def make_tool():
        tool = HHApplicantTool()
        tool.config_dir = tmp_path
        tool.profile_id = None
        tool.user_agent = None
        return tool

    Config(tmp_path / "config.json").save(token={"access_token": "t"})
    first = make_tool()._device_user_agent()
    assert first.startswith("ru.hh.android/")
    # Следующий запуск (другой процесс) — то же «устройство»
    assert make_tool()._device_user_agent() == first
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["user_agent"] == first and saved["token"] == {"access_token": "t"}
