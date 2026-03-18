"""Data extraction mixin for TelegramBackup."""

import base64
import logging

from telethon.tl.types import (
    Channel,
    Chat,
    Message,
    MessageMediaPoll,
    TextWithEntities,
    User,
)
from telethon.utils import get_peer_id

logger = logging.getLogger(__name__)


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
        # Save sender information if available
        if message.sender:
            sender_data = self._extract_user_data(message.sender)
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

        # Capture grouped_id for album detection (multiple photos/videos sent together)
        if message.grouped_id:
            message_data["raw_data"]["grouped_id"] = str(message.grouped_id)

        # Capture forwarded message info (name of original sender)
        if message.fwd_from:
            fwd = message.fwd_from
            # fwd_from.from_name is set when forwarding from hidden users or deleted accounts
            if fwd.from_name:
                message_data["raw_data"]["forward_from_name"] = fwd.from_name
            elif fwd.from_id:
                # Try to resolve the name from the entity
                try:
                    fwd_entity = await self.client.get_entity(fwd.from_id)
                    if hasattr(fwd_entity, "title"):
                        message_data["raw_data"]["forward_from_name"] = fwd_entity.title
                    elif hasattr(fwd_entity, "first_name"):
                        name = fwd_entity.first_name or ""
                        if fwd_entity.last_name:
                            name += " " + fwd_entity.last_name
                        message_data["raw_data"]["forward_from_name"] = name.strip()
                except Exception:
                    # Can't resolve - will fall back to ID in viewer
                    pass

        # Capture channel post author (signature) if available
        if hasattr(message, "post_author") and message.post_author:
            message_data["raw_data"]["post_author"] = message.post_author

        # Get reply text if this is a reply
        if message.reply_to_msg_id and message.reply_to:
            reply_msg = message.reply_to
            if hasattr(reply_msg, "message"):
                # Truncate to first 100 chars like Telegram does
                reply_text = (reply_msg.message or "")[:100]
                message_data["reply_to_text"] = reply_text

        # Handle media
        if message.media:
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
