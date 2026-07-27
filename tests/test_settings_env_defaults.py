"""
tests/test_settings_env_defaults.py

Regression test for a real bug a user hit: `os.getenv(key, default)`
only falls back to `default` when the key is *absent* from the
environment, not when it's present but blank. `.env.example` used to
show `SUPPLIER_INTEL_DB_PATH=` with nothing after the "=" -- if that
line ends up in a real `.env`, python-dotenv loads it as an empty
string, `os.getenv` returns that empty string instead of the intended
default, and `Path("").resolve()` resolves to the current working
directory. SQLite then tries to open that *directory* as if it were a
database file, producing a confusing "unable to open database file"
error that looks like OS/permissions interference but is actually just
this.

These tests reproduce the exact scenario and confirm DB_PATH/LOG_LEVEL
now treat a blank env var the same as an absent one.
"""

from __future__ import annotations

import importlib


def _reload_settings_with_env(monkeypatch, **env):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import config.settings as settings
    importlib.reload(settings)
    return settings


class TestDbPathBlankEnvVar:

    def test_blank_env_var_falls_back_to_the_default_not_cwd(self, monkeypatch):
        """The exact bug: SUPPLIER_INTEL_DB_PATH="" (present, blank)
        must resolve the same as it being unset entirely."""
        settings = _reload_settings_with_env(monkeypatch, SUPPLIER_INTEL_DB_PATH="")
        assert str(settings.DB_PATH).endswith("suppliers.db")
        assert settings.DB_PATH != settings.DATA_DIR  # never just the bare directory

    def test_unset_env_var_uses_the_same_default(self, monkeypatch):
        settings = _reload_settings_with_env(monkeypatch, SUPPLIER_INTEL_DB_PATH=None)
        assert str(settings.DB_PATH).endswith("suppliers.db")

    def test_real_override_value_is_still_respected(self, monkeypatch, tmp_path):
        """The fix must not break the legitimate override case --
        Railway setting a real path must still work."""
        real_path = str(tmp_path / "custom.db")
        settings = _reload_settings_with_env(monkeypatch, SUPPLIER_INTEL_DB_PATH=real_path)
        assert str(settings.DB_PATH) == real_path


class TestLogLevelBlankEnvVar:

    def test_blank_env_var_falls_back_to_info(self, monkeypatch):
        settings = _reload_settings_with_env(monkeypatch, SUPPLIER_INTEL_LOG_LEVEL="")
        assert settings.LOG_LEVEL == "INFO"

    def test_unset_env_var_falls_back_to_info(self, monkeypatch):
        settings = _reload_settings_with_env(monkeypatch, SUPPLIER_INTEL_LOG_LEVEL=None)
        assert settings.LOG_LEVEL == "INFO"

    def test_real_override_value_is_still_respected(self, monkeypatch):
        settings = _reload_settings_with_env(monkeypatch, SUPPLIER_INTEL_LOG_LEVEL="debug")
        assert settings.LOG_LEVEL == "DEBUG"
