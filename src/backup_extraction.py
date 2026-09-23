"""Data extraction mixin for TelegramBackup."""

import base64
import logging
import re

from telethon.tl.types import (
    Channel,
    Chat,
    Message,
    MessageActionChannelMigrateFrom,
    MessageActionChatMigrateTo,
    MessageMediaPoll,
    PeerChannel,
    PeerChat,
    PeerUser,
    TextWithEntities,
    User,
)
from telethon.utils import get_peer_id

from .message_utils import extract_extended_media_details, extract_webpage_preview, sender_display_name
from .telegram_stall_guard import TELEGRAM_CALL_TIMEOUT_SECONDS, with_call_timeout

logger = logging.getLogger(__name__)


def _service_action_type(action: object) -> str:
    """Normalize a Telethon ``MessageAction`` class name to snake_case.

    ``MessageActionTopicCreate`` -> ``"topic_create"``,
    ``MessageActionChatEditTitle`` -> ``"chat_edit_title"``.
    """
    name = type(action).__name__.removeprefix("MessageAction")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# Two Telethon class names do not snake_case into the name the stored format uses.
# The stored vocabulary follows Telegram's own Bot API names, which is what the
# viewer renders against: MessageEntityStrike is "strikethrough" there, and a
# mention by user id is displayed exactly like a plain mention.
_ENTITY_TYPE_ALIASES = {"strike": "strikethrough", "mention_name": "mention"}


def _entity_type(entity: object) -> str:
    """Normalize a Telethon ``MessageEntity`` class name to the stored type (#402).

    ``MessageEntityTextUrl`` -> ``"text_url"``, ``MessageEntityBold`` ->
    ``"bold"`` — mirrors ``_service_action_type`` above, then applies the
    aliases above so the stored name matches what the viewer renders.
    """
    name = type(entity).__name__.removeprefix("MessageEntity")
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return _ENTITY_TYPE_ALIASES.get(snake, snake)


class BackupExtractionMixin:
    """Extract/transform data from Telethon objects into DB-ready dicts."""

    def _get_marked_id(self, entity) -> int:
        """
        Get the marked ID for an entity (with -100 prefix for channels/supergroups).

        Telegram uses different ID formats:
        - Users: positive ID (e.g., 123456789)
        - Basic groups (Chat): negative ID (e.g., -123456789)
        - Supergroups/Channels: marked with -100 prefix (e.g., -1001234567890)

        This ensures IDs match what users see in Telegram and configure in env vars.
        """
        return get_peer_id(entity)

    def _extract_forward_from_id(self, message: Message) -> int | None:
        """
        Extract forward sender ID safely handling different Peer types.

        Args:
            message: Message object

        Returns:
            ID of the forward sender or None
        """
        if not message.fwd_from or not message.fwd_from.from_id:
            return None

        peer = message.fwd_from.from_id

        # Handle different Peer types
        if hasattr(peer, "user_id"):
            return peer.user_id
        if hasattr(peer, "channel_id"):
            return peer.channel_id
        if hasattr(peer, "chat_id"):
            return peer.chat_id

        return None

    def _extract_fwd_from(self, message: Message) -> dict | None:
        """Build the raw_data.fwd_from provenance dict for a forward (#400).

        Structural fields read straight off ``message.fwd_from`` — unlike the
        ``forward_from_name`` resolution done by ``_resolve_forward_source_name``,
        none of this costs an API call. Keys absent on this particular forward
        are omitted rather than stored as null, per the wave-2 capture contract.
        """
        fwd = message.fwd_from
        data: dict[str, object] = {}

        from_id = self._extract_forward_from_id(message)
        if from_id is not None:
            data["from_id"] = from_id
        if fwd.from_name:
            data["from_name"] = fwd.from_name
        if fwd.channel_post is not None:
            data["channel_post"] = fwd.channel_post
        if fwd.post_author:
            data["post_author"] = fwd.post_author
        if fwd.date is not None:
            data["date"] = fwd.date.isoformat()
        saved_from_peer = getattr(fwd, "saved_from_peer", None)
        if saved_from_peer is not None:
            try:
                data["saved_from_peer"] = get_peer_id(saved_from_peer)
            except (TypeError, ValueError):
                pass

        return data or None

    async def _resolve_forward_source_name(self, message: Message) -> str | None:
        """Resolve a forward's source display name with minimal API cost (#383).

        Order: the message's own ``from_name`` (set for hidden/deleted
        accounts, no lookup needed) -> our local users/chats tables -> at
        most one ``get_entity`` call per distinct source for the lifetime of
        this backup run. A source that can't be resolved is remembered in a
        negative cache, so a forward-heavy channel doesn't retry the same
        failing (and FloodWait-risky) lookup on every message.
        """
        fwd = message.fwd_from
        if fwd.from_name:
            return fwd.from_name
        peer = fwd.from_id
        if peer is None:
            return None

        # Lazily attached per-run caches: TelegramBackup is instantiated once
        # per backup_all()/fill_gaps() run (see connection.py's run_backup),
        # so plain instance attributes are already scoped to "this run"
        # without needing a constructor change in the file the lead owns.
        if not hasattr(self, "_fwd_source_name_cache"):
            self._fwd_source_name_cache: dict[int, str] = {}
            self._fwd_source_unresolved: set[int] = set()

        try:
            marked_id = get_peer_id(peer)
        except (TypeError, ValueError):
            return None

        if marked_id in self._fwd_source_name_cache:
            return self._fwd_source_name_cache[marked_id]
        if marked_id in self._fwd_source_unresolved:
            return None

        name = await self._lookup_forward_source_locally(peer)
        if name is None:
            name = await self._lookup_forward_source_via_api(peer)

        if name:
            self._fwd_source_name_cache[marked_id] = name
        else:
            self._fwd_source_unresolved.add(marked_id)
        return name

    async def _lookup_forward_source_locally(self, peer) -> str | None:
        """Look up a forward source's name in our own users/chats tables.

        Costs a local DB read, not a Telegram API call, so it's safe to try
        for every distinct source instead of budgeting it like get_entity.
        Neither lookup carries ``@retry_on_locked``, and unlike the previous
        code (whose only I/O here was a `get_entity` wrapped in `except
        Exception: pass`), a raised error must not escape into
        `_process_message` — that would abort the whole dialog per the wave-2
        isolation design note, over a lookup that's meant to be an
        optimization, not a hard dependency. A DB error here just means "try
        the API instead," same as "not found locally."
        """
        try:
            if isinstance(peer, PeerUser):
                user = await self.db.get_user_by_id(peer.user_id)
                if not user:
                    return None
                name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
                return name.strip() or user.get("username") or None
            if isinstance(peer, (PeerChannel, PeerChat)):
                chat = await self.db.get_chat_by_id(get_peer_id(peer))
                if not chat:
                    return None
                return chat.get("title") or chat.get("username") or None
        except Exception as e:
            logger.debug(f"Local forward-source lookup failed, falling back to API: {type(e).__name__}: {e}")
            return None
        return None

    async def _lookup_forward_source_via_api(self, peer) -> str | None:
        """Resolve a forward source's name via a single Telegram API call.

        Only reached once per distinct source per run (see
        ``_resolve_forward_source_name``'s caches).
        """
        try:
            entity = await with_call_timeout(self.client.get_entity(peer), TELEGRAM_CALL_TIMEOUT_SECONDS)
        except Exception:
            # Can't resolve - will fall back to ID in viewer
            return None
        if hasattr(entity, "title"):
            return entity.title
        if hasattr(entity, "first_name"):
            name = entity.first_name or ""
            if entity.last_name:
                name += " " + entity.last_name
            return name.strip() or None
        return None

    def _extract_entity(self, entity: object) -> dict:
        """Convert one Telethon ``MessageEntity`` to the raw_data.entities shape (#402)."""
        data: dict[str, object] = {
            "type": _entity_type(entity),
            "offset": entity.offset,
            "length": entity.length,
        }
        url = getattr(entity, "url", None)
        if url:
            data["url"] = url
        user_id = getattr(entity, "user_id", None)
        if user_id is not None:
            data["user_id"] = user_id
        language = getattr(entity, "language", None)
        if language:
            data["language"] = language
        return data

    def _text_with_entities_to_string(self, text_obj) -> str:
        """
        Convert TextWithEntities or string to a plain string.

        Args:
            text_obj: TextWithEntities object or string

        Returns:
            Plain string representation
        """
        if text_obj is None:
            return ""
        if isinstance(text_obj, str):
            return text_obj
        if isinstance(text_obj, TextWithEntities):
            # Extract the text from TextWithEntities
            return text_obj.text if hasattr(text_obj, "text") else str(text_obj)
        # Fallback for any other type
        return str(text_obj)

    async def _process_message(self, message: Message, chat_id: int) -> dict:
        """
        Process and save a single message.

        Args:
            message: Message object from Telegram
            chat_id: Chat identifier
        """
        # Scheduled sweeps snapshot only sender entities already attached by
        # Telethon; resolving a missing sender here would add one API request per
        # message and create avoidable flood risk on large histories.
        sender = message.sender

        # Save sender information if available
        if sender:
            sender_data = self._extract_user_data(sender)
            if sender_data:
                await self.db.upsert_user(sender_data)

        # Extract message data
        # v6.0.0: media_type, media_id, media_path removed - media stored in separate table
        # v6.2.0: reply_to_top_id added for forum topic threading
        reply_to_top_id = None
        if message.reply_to and getattr(message.reply_to, "forum_topic", False):
            reply_to_top_id = getattr(message.reply_to, "reply_to_top_id", None)
            # If reply_to_top_id is not set but it's a forum topic, use reply_to_msg_id
            if reply_to_top_id is None:
                reply_to_top_id = getattr(message.reply_to, "reply_to_msg_id", None)

        message_data = {
            "id": message.id,
            "chat_id": chat_id,
            "sender_id": message.sender_id,
            "sender_name": sender_display_name(sender),
            "date": message.date,
            "text": message.text or "",
            "reply_to_msg_id": message.reply_to_msg_id,
            "reply_to_top_id": reply_to_top_id,
            "reply_to_text": None,
            "forward_from_id": self._extract_forward_from_id(message),
            "edit_date": message.edit_date,
            "raw_data": {},
            "is_outgoing": 1 if message.out else 0,
            "is_pinned": 1 if getattr(message, "pinned", False) else 0,
        }

        # Preserve service-action metadata (e.g. forum topic creations and
        # renames) so historical backfills keep parity with the listener's
        # raw_data convention (service_type / action_type, since v6.0.0).
        # Without this, service events are stored with empty text and no
        # marker, so the viewer renders them as blank regular bubbles and the
        # payload is irrecoverable once the history is archived.
        action = getattr(message, "action", None)
        if action is not None:
            message_data["raw_data"]["service_type"] = "service"
            message_data["raw_data"]["action_type"] = _service_action_type(action)
            action_title = getattr(action, "title", None)
            if action_title is not None:
                message_data["raw_data"]["new_title"] = self._text_with_entities_to_string(action_title)

            # Group ↔ supergroup migration pointers (#228). MessageActionChatMigrateTo
            # carries only ``.channel_id`` (no ``.title``), so the new supergroup id
            # would otherwise be silently dropped; persist it in marked form so a
            # later sweep can reconcile scope even if the migration happened while
            # the archiver was offline. The reverse marker records the old group id.
            if isinstance(action, MessageActionChatMigrateTo):
                message_data["raw_data"]["migrate_to_id"] = get_peer_id(PeerChannel(action.channel_id))
            elif isinstance(action, MessageActionChannelMigrateFrom):
                message_data["raw_data"]["migrate_from_id"] = get_peer_id(PeerChat(action.chat_id))

        # Capture grouped_id for album detection (multiple photos/videos sent together)
        if message.grouped_id:
            message_data["raw_data"]["grouped_id"] = str(message.grouped_id)

        # Capture forwarded message info: sender name (#383 cache, minimizes
        # FloodWait-risky get_entity calls) plus raw provenance (#400).
        if message.fwd_from:
            forward_name = await self._resolve_forward_source_name(message)
            if forward_name:
                message_data["raw_data"]["forward_from_name"] = forward_name
            fwd_from_data = self._extract_fwd_from(message)
            if fwd_from_data:
                message_data["raw_data"]["fwd_from"] = fwd_from_data

        # Capture channel post author (signature) if available
        if hasattr(message, "post_author") and message.post_author:
            message_data["raw_data"]["post_author"] = message.post_author

        # Get quoted-reply excerpt if the sender selected specific text to
        # quote (#362). ``message.reply_to`` is a ``MessageReplyHeader``,
        # which has no ``.message`` attribute — the previous
        # ``hasattr(reply_msg, "message")`` check could therefore never be
        # true, and quote excerpts were silently dropped. A plain reply with
        # no explicit quote leaves ``quote_text`` unset; Telegram does not
        # backfill it with the full replied-to message text.
        if message.reply_to_msg_id and message.reply_to:
            quote_text = getattr(message.reply_to, "quote_text", None)
            if quote_text:
                # Truncate to first 100 chars like Telegram does
                message_data["reply_to_text"] = quote_text[:100]

        # Capture formatting entities (bold/italic/links/spoilers/code/etc.)
        # for later rendering (#402). Entity offsets are UTF-16 code units
        # into ``message.raw_text`` (the unmodified server text), NOT the
        # markdown-rendered ``text`` column set above, so raw_text is stored
        # alongside the entities rather than changing what ``text`` means.
        if message.entities:
            message_data["raw_data"]["raw_text"] = message.raw_text
            message_data["raw_data"]["entities"] = [self._extract_entity(entity) for entity in message.entities]

        # Handle media
        if message.media:
            # Link-preview cards (#323): pure metadata Telegram already
            # resolved when the message was sent, so this always runs here
            # regardless of the poll/download branch below. It's a no-op for
            # any non-webpage media — _process_media/_get_media_type already
            # treat MessageMediaWebPage as an unrecognized media type.
            webpage_preview = extract_webpage_preview(message)
            if webpage_preview:
                message_data["raw_data"]["webpage"] = webpage_preview

            # Venue, dice, invoice, story, giveaway, giveaway results, live
            # location, game and unsupported media (#401). None of these is a
            # downloadable file, and _get_media_type does not recognise them, so
            # the message was stored with no text and no media row and the viewer
            # showed an empty bubble where the official apps show a placeholder.
            # Stored in raw_data like polls already are, so the viewer can render
            # a typed chip without a media row that would look like a pending
            # download.
            extended_media = extract_extended_media_details(message.media)
            if extended_media is not None:
                extended_kind, extended_details = extended_media
                message_data["raw_data"][extended_kind] = extended_details

            # Handle Polls specially (store structure in raw_data, do not download)
            # v6.0.0: Poll type is detected by presence of raw_data['poll']
            if isinstance(message.media, MessageMediaPoll):
                poll = message.media.poll
                results = message.media.results

                # Parse results if available
                results_data = None
                if results:
                    try:
                        results_list = []
                        if results.results:
                            for r in results.results:
                                results_list.append(
                                    {
                                        "option": base64.b64encode(r.option).decode("ascii"),
                                        "voters": r.voters,
                                        "correct": r.correct,
                                    }
                                )
                        results_data = {"total_voters": results.total_voters, "results": results_list}
                    except Exception as e:
                        logger.warning(f"Error parsing poll results: {e}")

                # Store poll structure
                # Convert TextWithEntities to strings for JSON serialization
                question_text = self._text_with_entities_to_string(getattr(poll, "question", ""))
                message_data["raw_data"]["poll"] = {
                    "id": getattr(poll, "id", None),
                    "question": question_text,
                    "answers": [
                        {
                            "text": self._text_with_entities_to_string(getattr(a, "text", "")),
                            "option": base64.b64encode(a.option).decode("ascii"),
                        }
                        for a in poll.answers
                    ],
                    "closed": poll.closed,
                    "public_voters": poll.public_voters,
                    "multiple_choice": poll.multiple_choice,
                    "quiz": poll.quiz,
                    "results": results_data,
                }

            elif self.config.should_download_media_for_chat(chat_id):
                # v6.0.0: Download media and store data for later insertion
                # (media is inserted AFTER message to satisfy FK constraint)
                media_result = await self._process_media(message, chat_id)
                if media_result:
                    message_data["_media_data"] = media_result

        # Extract reactions if available
        reactions_data = []
        if hasattr(message, "reactions") and message.reactions:
            try:
                # Check if reactions.results exists (MessageReactions object)
                if hasattr(message.reactions, "results") and message.reactions.results:
                    for reaction in message.reactions.results:
                        emoji = reaction.reaction
                        # Handle both emoji strings and ReactionEmoji objects
                        if hasattr(emoji, "emoticon"):
                            emoji_str = emoji.emoticon
                        elif hasattr(emoji, "document_id"):
                            # Custom emoji (animated sticker) - use document_id as identifier
                            emoji_str = f"custom_{emoji.document_id}"
                        else:
                            emoji_str = str(emoji)

                        # Get user IDs who reacted (if available)
                        user_ids = []
                        if hasattr(reaction, "recent_reactions") and reaction.recent_reactions:
                            for recent in reaction.recent_reactions:
                                if hasattr(recent, "peer_id"):
                                    peer = recent.peer_id
                                    if hasattr(peer, "user_id"):
                                        user_ids.append(peer.user_id)
                                    elif hasattr(peer, "channel_id"):
                                        user_ids.append(peer.channel_id)

                        reactions_data.append({"emoji": emoji_str, "count": reaction.count, "user_ids": user_ids})

                    if reactions_data:
                        logger.debug(f"Extracted {len(reactions_data)} reactions for message {message.id}")
            except Exception as e:
                logger.warning(f"Error extracting reactions for message {message.id}: {e}")
                import traceback

                logger.debug(traceback.format_exc())

        # Store reactions separately (will be called after message is inserted)
        message_data["reactions"] = reactions_data

        # Return message data for batch processing
        return message_data

    def _extract_chat_data(self, entity, is_archived: bool = False) -> dict:
        """Extract chat data from entity.

        Args:
            entity: Telegram entity (User, Chat, Channel)
            is_archived: Whether this chat is from the archived folder
        """
        # Use marked ID (with -100 prefix for channels/supergroups) for consistency
        chat_data = {"id": self._get_marked_id(entity)}

        if isinstance(entity, User):
            chat_data["type"] = "private"
            chat_data["first_name"] = entity.first_name
            chat_data["last_name"] = entity.last_name
            chat_data["username"] = entity.username
            chat_data["phone"] = entity.phone
        elif isinstance(entity, Chat):
            chat_data["type"] = "group"
            chat_data["title"] = entity.title
            chat_data["participants_count"] = entity.participants_count
        elif isinstance(entity, Channel):
            chat_data["type"] = "channel" if not entity.megagroup else "group"
            chat_data["title"] = entity.title
            chat_data["username"] = entity.username
            # v6.2.0: Detect forum-enabled chats
            if getattr(entity, "forum", False):
                chat_data["is_forum"] = 1

        # v6.2.0: Track archived status (always set explicitly)
        chat_data["is_archived"] = 1 if is_archived else 0

        return chat_data

    def _extract_user_data(self, user) -> dict | None:
        """Extract user data from user entity."""
        if not isinstance(user, User):
            return None

        return {
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "is_bot": user.bot,
        }

    def _get_chat_name(self, entity) -> str:
        """Get a readable name for a chat."""
        if isinstance(entity, User):
            name = entity.first_name or ""
            if entity.last_name:
                name += f" {entity.last_name}"
            if entity.username:
                name += f" (@{entity.username})"
            return name or f"User {entity.id}"
        elif isinstance(entity, (Chat, Channel)):
            return entity.title or f"Chat {entity.id}"
        return f"Unknown {entity.id}"
