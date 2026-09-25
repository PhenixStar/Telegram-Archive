"""Telegram Rich Text Editor messages (semantic port of upstream #471).

Messages composed in Telegram's Rich Text Editor arrive with an empty wire text
and their content in ``message.rich_message``, an Instant View block tree.
Reading only the wire text stored every such message as an empty row. The tree
is rendered here into the same text + entity contract ordinary formatted
messages use (headings bold, quotes blockquote, code pre/code, lists with
bullets and numbers, tables as pipe-separated rows), so search, export and the
viewer need no new path; the tree itself is kept alongside as JSON.

Pure functions only: no Telethon imports, types are matched by class name.
"""

import base64
from datetime import datetime

from .message_utils import serialize_message_entity

_RICH_TEXT_ENTITY_BY_NODE = {
    "TextBold": "bold",
    "TextItalic": "italic",
    "TextUnderline": "underline",
    "TextStrike": "strikethrough",
    "TextFixed": "code",
    "TextSpoiler": "spoiler",
    "TextUrl": "text_url",
    "TextEmail": "email",
    "TextAutoEmail": "email",
    "TextPhone": "phone",
    "TextAutoPhone": "phone",
    "TextAutoUrl": "url",
    "TextMention": "mention",
    "TextMentionName": "mention",
    "TextHashtag": "hashtag",
    "TextCashtag": "cashtag",
    "TextBotCommand": "bot_command",
    "TextBankCard": "bank_card",
}
_RICH_HEADING_BLOCKS = frozenset(
    {
        "PageBlockTitle",
        "PageBlockSubtitle",
        "PageBlockHeader",
        "PageBlockSubheader",
        "PageBlockKicker",
        "PageBlockHeading1",
        "PageBlockHeading2",
        "PageBlockHeading3",
        "PageBlockHeading4",
        "PageBlockHeading5",
        "PageBlockHeading6",
    }
)
_RICH_MEDIA_BLOCK_LABELS = {
    "PageBlockPhoto": "photo",
    "PageBlockVideo": "video",
    "PageBlockAudio": "audio",
    "PageBlockCollage": "collage",
    "PageBlockSlideshow": "slideshow",
    "PageBlockMap": "map",
    "PageBlockEmbed": "embed",
    "PageBlockEmbedPost": "post",
}
_RICH_DIVIDER = "———"
_ZERO_WIDTH_SPACE = "​"


def rich_message_of(message: object) -> object | None:
    """``message.rich_message`` when the wire text is empty and it is a real RichMessage, else None.

    A message that carries its own text wins even if a block tree is also
    present: the wire entities index into that text, not into a rendering.
    """
    for attribute in ("raw_text", "message"):
        wire = getattr(message, attribute, None)
        if isinstance(wire, str) and wire:
            return None
    rich = getattr(message, "rich_message", None)
    if type(rich).__name__ != "RichMessage":
        return None
    return rich if isinstance(getattr(rich, "blocks", None), list) else None


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _RichMessageRenderer:
    """Flattens a block tree into text plus entity spans in UTF-16 code units."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.units = 0  # UTF-16 code units emitted so far: the entity offset space
        self.entities: list[dict] = []

    def emit(self, text: str) -> None:
        if text:
            self.parts.append(text)
            self.units += _utf16_units(text)

    def end_line(self) -> None:
        self.emit("\n")

    def close(self, start: int, entity_type: str, **extras: object) -> None:
        """Record an entity from ``start`` to now, excluding the line breaks this renderer added."""
        end = self.units
        index = len(self.parts)
        while index > 0 and self.parts[index - 1] == "\n" and end > start:
            index -= 1
            end -= 1
        if end > start:
            record: dict = {"type": entity_type, "offset": start, "length": end - start}
            record.update({key: value for key, value in extras.items() if value is not None})
            self.entities.append(record)

    # -- inline text nodes ------------------------------------------------

    def render_text(self, node: object) -> None:
        name = type(node).__name__
        if isinstance(node, str):
            self.emit(node)
        elif name == "TextPlain":
            text = getattr(node, "text", None)
            if isinstance(text, str):
                # The editor pads empty paragraphs with zero-width spaces.
                self.emit(text.replace(_ZERO_WIDTH_SPACE, ""))
        elif name == "TextConcat":
            for child in getattr(node, "texts", None) or []:
                self.render_text(child)
        elif name in ("TextEmpty", "TextImage"):
            return
        elif name == "TextCustomEmoji":
            alt = getattr(node, "alt", None)
            start = self.units
            self.emit(alt if isinstance(alt, str) else "")
            document_id = getattr(node, "document_id", None)
            self.close(start, "custom_emoji", document_id=document_id if isinstance(document_id, int) else None)
        elif name == "TextMath":
            source = getattr(node, "source", None)
            self.emit(source if isinstance(source, str) else "")
        elif name == "TextWithEntities":
            start = self.units
            text = getattr(node, "text", None)
            self.emit(text if isinstance(text, str) else "")
            for entity in getattr(node, "entities", None) or []:
                entity = serialize_message_entity(entity)
                entity["offset"] += start
                self.entities.append(entity)
        elif name in _RICH_TEXT_ENTITY_BY_NODE:
            start = self.units
            self.render_text(getattr(node, "text", None))
            url = getattr(node, "url", None)
            user_id = getattr(node, "user_id", None)
            self.close(
                start,
                _RICH_TEXT_ENTITY_BY_NODE[name],
                url=url if isinstance(url, str) and url else None,
                user_id=user_id if isinstance(user_id, int) else None,
            )
        else:
            # TextAnchor, TextMarked, TextSubscript, TextSuperscript, TextDate
            # and anything newer: keep the words, drop the decoration.
            inner = getattr(node, "text", None)
            if inner is not None and not isinstance(inner, (int, float, bool)):
                self.render_text(inner)

    def render_line(self, node: object) -> None:
        """A text node on a line of its own, or nothing when it renders empty."""
        start = self.units
        self.render_text(node)
        if self.units > start:
            self.end_line()

    def render_heading(self, node: object) -> None:
        start = self.units
        self.render_text(node)
        self.close(start, "bold")
        if self.units > start:
            self.end_line()

    # -- blocks -------------------------------------------------------------

    def render_blocks(self, blocks: object) -> None:
        for block in blocks if isinstance(blocks, list) else []:
            self.render_block(block)

    def render_block(self, block: object) -> None:
        name = type(block).__name__
        if name == "PageBlockParagraph":
            self.render_text(getattr(block, "text", None))
            self.end_line()
        elif name in _RICH_HEADING_BLOCKS:
            self.render_heading(getattr(block, "text", None))
        elif name == "PageBlockDivider":
            self.emit(_RICH_DIVIDER)
            self.end_line()
        elif name in ("PageBlockBlockquote", "PageBlockPullquote"):
            start = self.units
            self.render_text(getattr(block, "text", None))
            self.close(start, "blockquote")
            if self.units > start:
                self.end_line()
            self.render_line(getattr(block, "caption", None))
        elif name == "PageBlockBlockquoteBlocks":
            start = self.units
            self.render_blocks(getattr(block, "blocks", None))
            self.close(start, "blockquote")
            self.render_line(getattr(block, "caption", None))
        elif name == "PageBlockList":
            for item in getattr(block, "items", None) or []:
                self.render_list_item(item, "• ")
        elif name == "PageBlockOrderedList":
            first = getattr(block, "start", None)
            for position, item in enumerate(getattr(block, "items", None) or []):
                label = getattr(item, "num", None)
                if not isinstance(label, str) or not label:
                    value = getattr(item, "value", None)
                    if not isinstance(value, int):
                        value = (first if isinstance(first, int) else 1) + position
                    label = str(value)
                self.render_list_item(item, f"{label}. ")
        elif name == "PageBlockPreformatted":
            start = self.units
            self.render_text(getattr(block, "text", None))
            language = getattr(block, "language", None)
            self.close(start, "pre", language=language if isinstance(language, str) and language else None)
            if self.units > start and not self.parts[-1].endswith("\n"):
                self.end_line()
        elif name == "PageBlockTable":
            self.render_heading(getattr(block, "title", None))
            for row in getattr(block, "rows", None) or []:
                self.render_table_row(row)
        elif name == "PageBlockDetails":
            self.render_heading(getattr(block, "title", None))
            self.render_blocks(getattr(block, "blocks", None))
        elif name in _RICH_MEDIA_BLOCK_LABELS:
            # The file itself is not in the message; say what stood here.
            self.emit(f"[{_RICH_MEDIA_BLOCK_LABELS[name]}]")
            self.end_line()
            caption = getattr(block, "caption", None)
            self.render_line(getattr(caption, "text", None))
        elif name == "PageBlockCover":
            self.render_block(getattr(block, "cover", None))
        elif name == "PageBlockAuthorDate":
            self.render_line(getattr(block, "author", None))
        elif name == "PageBlockMath":
            source = getattr(block, "source", None)
            if isinstance(source, str) and source:
                self.emit(source)
                self.end_line()
        elif name == "PageBlockRelatedArticles":
            self.render_line(getattr(block, "title", None))
        else:
            # PageBlockFooter, PageBlockThinking and anything newer with a
            # text: keep the words. Anchors, channels and unsupported blocks
            # have none and render nothing.
            inner = getattr(block, "text", None)
            if inner is not None and not isinstance(inner, (str, int, float, bool)):
                self.render_line(inner)

    def render_list_item(self, item: object, bullet: str) -> None:
        name = type(item).__name__
        checkbox = getattr(item, "checkbox", None)
        if checkbox is True:
            bullet = "☑ " if getattr(item, "checked", None) is True else "☐ "
        self.emit(bullet)
        if name in ("PageListItemBlocks", "PageListOrderedItemBlocks"):
            start = self.units
            self.render_blocks(getattr(item, "blocks", None))
            if self.units == start:
                self.end_line()
        else:
            self.render_text(getattr(item, "text", None))
            self.end_line()

    def render_table_row(self, row: object) -> None:
        cells = getattr(row, "cells", None) or []
        for position, cell in enumerate(cells):
            if position:
                self.emit(" | ")
            start = self.units
            self.render_text(getattr(cell, "text", None))
            if getattr(cell, "header", None) is True:
                self.close(start, "bold")
        self.end_line()

    def result(self) -> tuple[str, list[dict]]:
        text = "".join(self.parts).rstrip("\n")
        limit = _utf16_units(text)
        entities = []
        for entity in self.entities:
            length = min(entity["length"], limit - entity["offset"])
            if length > 0:
                entities.append({**entity, "length": length})
        entities.sort(key=lambda entity: (entity["offset"], -entity["length"]))
        return text, entities


def render_rich_message(rich: object) -> tuple[str, list[dict]]:
    """Flatten a ``RichMessage`` block tree into ``(text, entities)``.

    ``text`` is what the ``messages.text`` column stores and search indexes;
    ``entities`` follow the raw_data.entities shape (UTF-16 offsets)
    so the viewer renders headings, quotes, lists and code through the path
    it already has for ordinary formatted messages.
    """
    renderer = _RichMessageRenderer()
    renderer.render_blocks(getattr(rich, "blocks", None))
    return renderer.result()


def effective_message_text(message: object) -> str:
    """Text to archive for an edited message: the rendered block tree for a Rich
    Text Editor message (whose wire text is empty), otherwise ``message.text``.
    Without this an edit to such a message would blank the archived text."""
    rich = rich_message_of(message)
    if rich is not None:
        return render_rich_message(rich)[0]
    # ``text`` is the client-rendered form the archive stores; without a client
    # Telethon leaves it empty, so fall back to the raw wire text.
    for attribute in ("text", "message"):
        text = getattr(message, attribute, None)
        if isinstance(text, str) and text:
            return text
    return ""


def message_rich_payload(message: object) -> dict | None:
    """``raw_data["rich_message"]`` for a message, or None when it has no block tree to keep."""
    rich = rich_message_of(message)
    return rich_message_payload(rich) if rich is not None else None


def rich_message_payload(rich: object) -> dict:
    """JSON-safe copy of the block tree for ``raw_data["rich_message"]``.

    Blocks only: the ``photos``/``documents`` lists and any ``access_hash``
    are session-bound handles the archive cannot use later. Bytes are
    base64 (photo sizes, file references), datetimes are ISO 8601.
    """
    blocks = [
        _json_safe(block.to_dict())
        for block in getattr(rich, "blocks", None) or []
        if callable(getattr(block, "to_dict", None))
    ]
    payload: dict = {"blocks": blocks}
    if getattr(rich, "rtl", None) is True:
        payload["rtl"] = True
    if getattr(rich, "part", None) is True:
        payload["part"] = True
    return payload


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items() if key != "access_hash"}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
