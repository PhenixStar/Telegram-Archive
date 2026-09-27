"""Config hardening ported from upstream: bad values degrade safely instead of wedging."""

import os
import sys
from unittest.mock import patch

import pytest

from src.config import Config
from src.message_utils import normalize_configured_chat_ids


def _config(tmp_path, **env):
    base = {"CHAT_TYPES": "private,groups,channels", "BACKUP_PATH": str(tmp_path)}
    with patch.dict(os.environ, {**base, **env}, clear=True):
        return Config()


def test_misspelled_timezone_falls_back_to_utc(tmp_path):
    assert _config(tmp_path, VIEWER_TIMEZONE="Asia/Manilla").viewer_timezone == "UTC"
    assert _config(tmp_path, VIEWER_TIMEZONE="Asia/Manila").viewer_timezone == "Asia/Manila"


@pytest.mark.parametrize("raw", ["24", "-1", "three"])
def test_out_of_range_stats_hour_uses_the_default(tmp_path, raw):
    assert _config(tmp_path, STATS_CALCULATION_HOUR=raw).stats_calculation_hour == 3


def test_zero_media_size_limit_means_no_limit(tmp_path):
    assert _config(tmp_path, MAX_MEDIA_SIZE_MB="0").get_max_media_size_bytes() == sys.maxsize
    assert _config(tmp_path, MAX_MEDIA_SIZE_MB="5").get_max_media_size_bytes() == 5 * 1024 * 1024


def test_excluding_topic_1_excludes_general_messages(tmp_path):
    config = _config(tmp_path, SKIP_TOPIC_IDS="-1001:1")
    assert config.should_skip_topic(-1001, None) is True  # General carries no topic id
    assert config.should_skip_topic(-1001, 5) is False
    assert config.should_skip_topic(-2002, None) is False  # other chats untouched


def test_normalize_configured_chat_ids():
    existing = {-1001234567890, 42}
    normalized, corrected, unresolved = normalize_configured_chat_ids({1234567890, 42, 777}, existing)
    assert normalized == {-1001234567890, 42, 777}
    assert (corrected, unresolved) == (1, 1)


def test_config_filters_and_topic_keys_are_auto_corrected(tmp_path):
    config = _config(tmp_path, GLOBAL_EXCLUDE_CHAT_IDS="1234567890", SKIP_TOPIC_IDS="1234567890:3")
    corrected, unresolved = config.normalize_filter_ids({-1001234567890})
    assert config.global_exclude_ids == {-1001234567890}
    assert config.skip_topic_ids == {-1001234567890: {3}}
    assert (corrected, unresolved) == (2, 0)
