"""Media-path normalisation for a moved archive, and the per-chat media ACL that
a share-token viewer must not be able to walk out of."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.message_utils import normalize_media_path
from src.web import dependencies
from src.web.dependencies import UserContext
from src.web.routes_media import _checked_media_request_path, _enforce_restricted_chat_acl

MEDIA_ROOT = Path("/data/backups/media")


class TestNormalizeMediaPath:
    """A moved archive leaves rows pointing at roots that no longer exist."""

    def test_relative_path_is_unchanged(self):
        assert normalize_media_path("126/photo.jpg", MEDIA_ROOT) == "126/photo.jpg"

    def test_absolute_path_under_the_current_root_is_trimmed(self):
        assert normalize_media_path("/data/backups/media/126/photo.jpg", MEDIA_ROOT) == "126/photo.jpg"

    def test_stale_absolute_root_is_re_anchored(self):
        # 17.9k production rows look exactly like this.
        stale = "/home/dgx/Desktop/tele-private/database/backups/media/-100123/file.mp4"
        assert normalize_media_path(stale, MEDIA_ROOT) == "-100123/file.mp4"

    def test_dot_relative_legacy_path_is_re_anchored(self):
        assert normalize_media_path("./data/backups/media/126/photo.jpg", MEDIA_ROOT) == "126/photo.jpg"

    def test_windows_written_row_is_re_anchored(self):
        stale = r"C:\archive\backups\media\126\photo.jpg"
        assert normalize_media_path(stale, MEDIA_ROOT) == "126/photo.jpg"

    def test_traversal_is_rejected(self):
        assert normalize_media_path("../../etc/passwd", MEDIA_ROOT) is None
        assert normalize_media_path("126/../../etc/passwd", MEDIA_ROOT) is None

    def test_unanchorable_absolute_path_is_rejected(self):
        assert normalize_media_path("/etc/passwd", MEDIA_ROOT) is None

    def test_empty_input_is_rejected(self):
        assert normalize_media_path("", MEDIA_ROOT) is None
        assert normalize_media_path(None, MEDIA_ROOT) is None


class TestMediaRequestPathCheck:
    def test_encoded_traversal_is_refused(self):
        # The ASGI server percent-decodes before routing, so "%2e%2e" arrives here
        # as a real ".." segment.
        with pytest.raises(HTTPException) as exc:
            _checked_media_request_path("x/../-100999/secret.jpg")
        assert exc.value.status_code == 403

    def test_absolute_request_path_is_refused(self):
        with pytest.raises(HTTPException):
            _checked_media_request_path("/etc/passwd")

    def test_plain_path_passes_through_unchanged(self):
        assert _checked_media_request_path("126/photo.jpg") == "126/photo.jpg"


class TestRestrictedChatAcl:
    """A share-token viewer may only reach the chats it was granted."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        # get_user_chat_ids reads the module-level config for the global
        # display_chat_ids filter, which the lifespan normally sets.
        monkeypatch.setattr(dependencies, "config", SimpleNamespace(display_chat_ids=None))

    restricted = UserContext(username="token", role="viewer", allowed_chat_ids={126})
    master = UserContext(username="alaa", role="master")

    def test_own_chat_is_allowed(self):
        _enforce_restricted_chat_acl("126/photo.jpg", self.restricted)

    def test_other_chat_is_denied(self):
        with pytest.raises(HTTPException) as exc:
            _enforce_restricted_chat_acl("-100999/photo.jpg", self.restricted)
        assert exc.value.status_code == 403

    def test_shared_dedup_store_is_denied(self):
        # _shared/ pools blobs from every chat, so it can never be proven safe
        # for a restricted account. It used to pass because int() raised.
        with pytest.raises(HTTPException):
            _enforce_restricted_chat_acl("_shared/ab/cd/blob.jpg", self.restricted)

    def test_non_numeric_folder_is_denied(self):
        with pytest.raises(HTTPException):
            _enforce_restricted_chat_acl("wherever/photo.jpg", self.restricted)

    def test_avatars_stay_available(self):
        _enforce_restricted_chat_acl("avatars/126.jpg", self.restricted)

    def test_master_is_unrestricted(self):
        _enforce_restricted_chat_acl("_shared/ab/cd/blob.jpg", self.master)
        _enforce_restricted_chat_acl("-100999/photo.jpg", self.master)
