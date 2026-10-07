"""The archive names itself in Telegram's device list (Settings > Devices)."""

import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import __version__
from src.config import (
    DEFAULT_TELEGRAM_DEVICE_MODEL,
    Config,
    build_telegram_client_kwargs,
    telegram_device_kwargs,
    telegram_device_model_from_env,
    telegram_system_version,
)
from src.connection import TelegramConnection


@pytest.fixture
def base_env():
    backup = tempfile.mkdtemp()
    yield {
        "CHAT_TYPES": "private",
        "BACKUP_PATH": backup,
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "x",
        "TELEGRAM_PHONE": "+1",
    }
    shutil.rmtree(backup, ignore_errors=True)


def test_device_kwargs_name_the_device_os_release_and_language():
    with (
        patch("src.config.platform.system", return_value="Linux"),
        patch("src.config.platform.release", return_value="6.1.0-99-generic"),
    ):
        kwargs = telegram_device_kwargs("Test Device A")
    assert kwargs == {
        "device_model": "Test Device A",
        "system_version": "Linux 6.1.0",
        "app_version": __version__,
        "lang_code": "en",
        "system_lang_code": "en",
    }


def test_system_version_keeps_a_release_without_suffix():
    with (
        patch("src.config.platform.system", return_value="Darwin"),
        patch("src.config.platform.release", return_value="24.0.0"),
    ):
        assert telegram_system_version() == "Darwin 24.0.0"


def test_system_version_is_never_blank():
    with (
        patch("src.config.platform.system", return_value=""),
        patch("src.config.platform.release", return_value=""),
    ):
        assert telegram_system_version() == "Unknown"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_env_gives_the_default_name(base_env, value):
    env = dict(base_env)
    if value is not None:
        env["TELEGRAM_DEVICE_MODEL"] = value
    with patch.dict(os.environ, env, clear=True):
        config = Config()
        assert telegram_device_model_from_env() == DEFAULT_TELEGRAM_DEVICE_MODEL
        assert build_telegram_client_kwargs()["device_model"] == "Telegram Archive"
    assert config.get_telegram_client_kwargs()["device_model"] == "Telegram Archive"


def test_env_override_is_trimmed_and_reaches_both_builders(base_env):
    with patch.dict(os.environ, {**base_env, "TELEGRAM_DEVICE_MODEL": "  Test Archive B  "}, clear=True):
        config = Config()
        module_kwargs = build_telegram_client_kwargs()
        config_kwargs = config.get_telegram_client_kwargs()
    assert config.telegram_device_model == "Test Archive B"
    assert config_kwargs["device_model"] == module_kwargs["device_model"] == "Test Archive B"
    assert config_kwargs["app_version"] == module_kwargs["app_version"] == __version__


def test_device_fields_sit_beside_flood_and_proxy_settings(base_env):
    env = {
        **base_env,
        "FLOOD_SLEEP_THRESHOLD": "0",
        "TELEGRAM_PROXY_TYPE": "socks5",
        "TELEGRAM_PROXY_ADDR": "127.0.0.1",
        "TELEGRAM_PROXY_PORT": "1080",
    }
    with patch.dict(os.environ, env, clear=True):
        kwargs = Config().get_telegram_client_kwargs()
    assert kwargs["flood_sleep_threshold"] == 0
    assert kwargs["proxy"]["addr"] == "127.0.0.1"
    assert kwargs["device_model"] == DEFAULT_TELEGRAM_DEVICE_MODEL


@pytest.mark.asyncio
async def test_connection_builds_the_client_with_the_device_identity(base_env, tmp_path):
    with patch.dict(os.environ, {**base_env, "TELEGRAM_DEVICE_MODEL": "Test Device C"}, clear=True):
        config = Config()
    config.session_path = str(tmp_path / "session")
    (tmp_path / "session.session").write_bytes(b"placeholder")

    client = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_me = AsyncMock(return_value=MagicMock())
    client.session = MagicMock()
    client.session._conn = None

    with patch("src.connection.TelegramClient", return_value=client) as client_cls:
        await TelegramConnection(config).connect()

    _, kwargs = client_cls.call_args
    assert kwargs["device_model"] == "Test Device C"
    assert kwargs["app_version"] == __version__
