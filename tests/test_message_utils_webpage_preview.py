"""Tests for `extract_webpage_preview` (#323 link-preview writer).

Our viewer (`getLinkPreview` in index.html) already renders a link-preview
card from `raw_data.webpage` / `raw_data.web_page`; this helper is what
finally writes that data at capture time, in both the backfill sweep
(backup_extraction.py) and the live listener path (listener.py).
"""

from unittest.mock import MagicMock

from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaWebPage,
    WebPage,
    WebPageEmpty,
    WebPagePending,
)

from src.message_utils import extract_webpage_preview


def _message_with_media(media):
    msg = MagicMock()
    msg.media = media
    return msg


def _webpage(**overrides):
    defaults = dict(
        id=1,
        url="https://example.com/article",
        display_url="example.com/article",
        hash=0,
        site_name="Example News",
        title="Breaking Story",
        description="A short summary of the story.",
    )
    defaults.update(overrides)
    return WebPage(**defaults)


def test_extracts_url_site_name_title_description():
    message = _message_with_media(MessageMediaWebPage(webpage=_webpage()))

    preview = extract_webpage_preview(message)

    assert preview == {
        "url": "https://example.com/article",
        "site_name": "Example News",
        "title": "Breaking Story",
        "description": "A short summary of the story.",
    }


def test_never_includes_a_photo_key_even_when_present():
    """Webpage-preview photos are not downloaded in this port; omit `photo`."""
    photo = MagicMock()
    message = _message_with_media(MessageMediaWebPage(webpage=_webpage(photo=photo)))

    preview = extract_webpage_preview(message)

    assert "photo" not in preview


def test_omits_missing_description():
    message = _message_with_media(MessageMediaWebPage(webpage=_webpage(description=None)))

    preview = extract_webpage_preview(message)

    assert "description" not in preview


def test_falls_back_when_title_and_site_name_both_missing():
    message = _message_with_media(MessageMediaWebPage(webpage=_webpage(title=None, site_name=None)))

    assert extract_webpage_preview(message) is None


def test_rejects_non_http_url():
    message = _message_with_media(MessageMediaWebPage(webpage=_webpage(url="tg://resolve?domain=example")))

    assert extract_webpage_preview(message) is None


def test_no_media_returns_none():
    assert extract_webpage_preview(_message_with_media(None)) is None


def test_non_webpage_media_returns_none():
    message = _message_with_media(MagicMock(spec=MessageMediaDocument))

    assert extract_webpage_preview(message) is None


def test_pending_webpage_returns_none():
    """Telegram is still resolving the preview; nothing to show yet."""
    pending = WebPagePending(id=1, date=None, url="https://example.com")
    message = _message_with_media(MessageMediaWebPage(webpage=pending))

    assert extract_webpage_preview(message) is None


def test_empty_webpage_returns_none():
    empty = WebPageEmpty(id=1, url="https://example.com")
    message = _message_with_media(MessageMediaWebPage(webpage=empty))

    assert extract_webpage_preview(message) is None
