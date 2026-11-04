import pytest

from robot_voice.application import RuntimeLock
from robot_voice.cli import main, parser
from robot_voice.config import Config, load_config


def test_lazy_audio_import_and_doctor_does_not_need_cloud(config, monkeypatch, capsys):
    def unavailable():
        raise RuntimeError("PortAudio unavailable: install libportaudio2")

    monkeypatch.setattr("robot_voice.audio.frontend.sounddevice", unavailable)
    assert main(["doctor"]) == 1
    assert "libportaudio2" in capsys.readouterr().out


def test_paid_test_requires_explicit_opt_in():
    with pytest.raises(SystemExit):
        parser().parse_args(["live-test"])


def test_external_aec_requires_selected_input():
    with pytest.raises(ValueError):
        Config(audio_mode="external-aec")
    assert (
        Config(audio_mode="external-aec", input_device="OS echo cancel").audio_mode
        == "external-aec"
    )


def test_config_rejects_unknown_or_unbounded_settings(tmp_path):
    for field in [
        {"idle_timeout": -1},
        {"budget_dollars": -1},
        {"unknown": 1},
        {"timezone": "Invalid/Zone"},
    ]:
        with pytest.raises((ValueError, KeyError)):
            Config(**field)
    path = tmp_path / "settings.toml"
    path.write_text('wake_phrase="hey robot"')
    assert load_config(str(path)).wake_phrase == "hey robot"


def test_runtime_lock_prevents_other_runner(config):
    with RuntimeLock(config.database), pytest.raises(RuntimeError):
        with RuntimeLock(config.database):
            pass


def test_offline_demo_history_export_delete(tmp_path, capsys):
    base = ["--database", str(tmp_path / "cli.sqlite3")]
    assert main([*base, "demo"]) == 0
    capsys.readouterr()
    assert main([*base, "history"]) == 0
    import json

    session = json.loads(capsys.readouterr().out)[0]["id"]
    assert main([*base, "export", "--session", session, "--format", "text"]) == 0
    assert "bedroom" in capsys.readouterr().out
    assert main([*base, "delete", "--session", session, "--yes"]) == 0
