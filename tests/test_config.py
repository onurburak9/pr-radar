from pathlib import Path

import pytest

from pr_radar.config import EXAMPLE_CONFIG_PATH, ConfigError, load_config


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_example_config_loads():
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    assert cfg.github_login == "your-github-login"
    assert cfg.repos == ("your-org/your-repo", "your-org/another-repo")


def test_missing_config_points_at_example(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
        load_config(tmp_path / "config.toml")


def test_defaults_apply(tmp_path):
    cfg = load_config(write(tmp_path, 'github_login = "me"\nrepos = ["o/r"]\n'))
    assert cfg.poll_interval_seconds == 120
    assert cfg.include_team_review_requests is False


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown"):
        load_config(write(tmp_path, 'github_login = "me"\nrepos = ["o/r"]\nrepoz = []\n'))


def test_bad_repo_rejected(tmp_path):
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(write(tmp_path, 'github_login = "me"\nrepos = ["widgets"]\n'))


def test_poll_interval_floor(tmp_path):
    with pytest.raises(ConfigError, match=">= 30"):
        load_config(write(tmp_path, 'github_login = "me"\nrepos = ["o/r"]\npoll_interval_seconds = 5\n'))
