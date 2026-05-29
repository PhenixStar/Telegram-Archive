"""Regression tests for frontend boot-time failures."""

from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


@pytest.mark.skip(
    reason="Base-only markup: the fork ships a redesigned single-file Vue 3 index.html "
    "that has no `showMediaGallery` ref/watcher (media gallery implemented differently), "
    "so this assertion on base's specific declaration/watcher ordering does not apply."
)
def test_media_gallery_refs_are_initialized_before_watcher():
    """The root Vue setup must not touch media gallery refs before their const declarations."""
    html = INDEX_HTML.read_text()

    state_index = html.index("const showMediaGallery = ref(false)")
    watcher_index = html.index("watch(showMediaGallery")

    assert state_index < watcher_index
