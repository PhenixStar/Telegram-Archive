"""Every `self.db.<method>(` the capture code calls must exist on the real adapter.

Tests usually wire `db` as an AsyncMock, which invents any attribute, so a call
to a method that does not exist passed every test and would only fail in
production (the listener once called a `resolve_message_chat_id` that never
existed). This checks the real class instead.
"""

import re
from pathlib import Path

import pytest

from src.db.adapter import DatabaseAdapter

SRC = Path(__file__).resolve().parent.parent / "src"
CALLERS = ["listener.py", "telegram_backup.py", "backup_media.py", "backup_extraction.py", "scheduler.py"]


@pytest.mark.parametrize("filename", CALLERS)
def test_db_calls_resolve_to_adapter_methods(filename):
    text = (SRC / filename).read_text()
    called = set(re.findall(r"self\.db\.([A-Za-z_][A-Za-z0-9_]*)\(", text))
    missing = sorted(name for name in called if not hasattr(DatabaseAdapter, name))
    assert not missing, f"{filename} calls DatabaseAdapter methods that do not exist: {missing}"
