"""Media kinds Telegram sends as message payloads rather than files.

Venue, dice, invoice, story, giveaways, live location, game and unsupported media
have no text and nothing to download, so the archive stored a message with no
text and no media row and the viewer showed an empty bubble where the official
apps show a placeholder. They are captured into raw_data, the way polls already
were, so the viewer can render a typed chip without a media row that would look
like a pending download.
"""

from datetime import datetime

import pytest
from telethon.tl.types import (
    Game,
    GeoPoint,
    MessageMediaDice,
    MessageMediaGame,
    MessageMediaGeoLive,
    MessageMediaGiveaway,
    MessageMediaGiveawayResults,
    MessageMediaInvoice,
    MessageMediaPhoto,
    MessageMediaUnsupported,
    MessageMediaVenue,
)

from src.message_utils import (
    METADATA_ONLY_MEDIA_TYPES,
    classify_extended_media,
    extract_extended_media_details,
)


def _geo(lat=14.5995, long=120.9842):
    return GeoPoint(long=long, lat=lat, access_hash=0, accuracy_radius=None)


class TestClassification:
    def test_each_kind_is_named(self):
        cases = [
            (MessageMediaDice(value=3, emoticon="🎲"), "dice"),
            (MessageMediaUnsupported(), "unsupported"),
            (MessageMediaGeoLive(geo=_geo(), period=60), "geo_live"),
            (
                MessageMediaVenue(
                    geo=_geo(), title="t", address="a", provider="p", venue_id="1", venue_type="cafe"
                ),
                "venue",
            ),
        ]
        for media, expected in cases:
            assert classify_extended_media(media) == expected

    def test_a_downloadable_kind_is_not_claimed(self):
        # Photos and documents must keep going down the download path.
        assert classify_extended_media(MessageMediaPhoto(photo=None)) is None

    def test_none_is_handled(self):
        assert classify_extended_media(None) is None

    def test_an_unrelated_object_is_inert(self):
        # Name-based classification, so a test double must not be mistaken for media.
        from unittest.mock import MagicMock

        assert classify_extended_media(MagicMock()) is None

    def test_every_named_kind_is_metadata_only(self):
        # The viewer and the download bookkeeping both key off this set.
        for kind in ("venue", "dice", "invoice", "story", "giveaway", "giveaway_results", "geo_live", "game"):
            assert kind in METADATA_ONLY_MEDIA_TYPES


class TestDetails:
    def test_dice_keeps_the_roll(self):
        kind, details = extract_extended_media_details(MessageMediaDice(value=5, emoticon="🎯"))
        assert kind == "dice"
        assert details == {"emoticon": "🎯", "value": 5}

    def test_venue_keeps_place_and_coordinates(self):
        media = MessageMediaVenue(
            geo=_geo(), title="Rizal Park", address="Roxas Blvd", provider="foursquare", venue_id="1", venue_type="park"
        )
        kind, details = extract_extended_media_details(media)
        assert kind == "venue"
        assert details["title"] == "Rizal Park"
        assert details["address"] == "Roxas Blvd"
        assert details["lat"] == pytest.approx(14.5995)

    def test_invoice_keeps_the_amount_in_its_smallest_unit(self):
        media = MessageMediaInvoice(
            title="Subscription",
            description="One month",
            currency="PHP",
            total_amount=50000,
            start_param="x",
            test=True,
        )
        kind, details = extract_extended_media_details(media)
        assert kind == "invoice"
        assert details["currency"] == "PHP"
        assert details["total_amount"] == 50000
        assert details["test"] is True

    def test_live_location_keeps_coordinates_and_period(self):
        media = MessageMediaGeoLive(geo=_geo(), period=3600)
        kind, details = extract_extended_media_details(media)
        assert kind == "geo_live"
        assert details["period"] == 3600
        assert details["long"] == pytest.approx(120.9842)

    def test_giveaway_keeps_counts(self):
        media = MessageMediaGiveaway(
            channels=[1, 2, 3], quantity=10, months=6, until_date=datetime(2026, 12, 1), stars=None
        )
        kind, details = extract_extended_media_details(media)
        assert kind == "giveaway"
        assert details["quantity"] == 10
        assert details["months"] == 6

    def test_giveaway_results_keeps_the_winner_count(self):
        media = MessageMediaGiveawayResults(
            winners_count=3,
            unclaimed_count=0,
            winners=[],
            months=1,
            until_date=datetime(2026, 12, 1),
            launch_msg_id=1,
            additional_peers_count=None,
            channel_id=1,
            prize_description=None,
        )
        kind, details = extract_extended_media_details(media)
        assert kind == "giveaway_results"
        assert details["winners_count"] == 3

    def test_game_keeps_its_name(self):
        media = MessageMediaGame(
            game=Game(id=1, access_hash=1, short_name="chess", title="Chess", description="Play", photo=None)
        )
        kind, details = extract_extended_media_details(media)
        assert kind == "game"
        assert details["title"] == "Chess"
        assert details["short_name"] == "chess"

    def test_unsupported_media_carries_no_payload(self):
        # Its presence is the whole signal: the viewer shows the label alone.
        assert extract_extended_media_details(MessageMediaUnsupported()) == ("unsupported", {})

    def test_only_primitives_are_stored(self):
        # raw_data is serialized to JSON, so a stray Telethon object must not leak in.
        media = MessageMediaVenue(
            geo=_geo(), title="Cafe", address="Main St", provider="p", venue_id="1", venue_type="cafe"
        )
        _, details = extract_extended_media_details(media)
        assert all(isinstance(value, str | int | float | bool) for value in details.values())

    def test_a_downloadable_kind_returns_nothing(self):
        assert extract_extended_media_details(MessageMediaPhoto(photo=None)) is None
