import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rotkehlchen.assets.asset import Asset, AssetWithOracles
from rotkehlchen.assets.converters import LOCATION_TO_ASSET_MAPPING, asset_from_common_identifier
from rotkehlchen.db.dbhandler import DBHandler
from rotkehlchen.db.drivers.gevent import DBCursor
from rotkehlchen.db.history_events import DBHistoryEvents
from rotkehlchen.errors.misc import InputError
from rotkehlchen.exchanges.data_structures import AssetMovement, MarginPosition, Trade
from rotkehlchen.history.events.structures.base import HistoryBaseEntry
from rotkehlchen.history.events.structures.types import HistoryEventSubType, HistoryEventType
from rotkehlchen.serialization.deserialize import deserialize_asset_amount, deserialize_timestamp
from rotkehlchen.types import AssetAmount, Fee, Location, TimestampMS

ITEMS_PER_DB_WRITE = 400


class BaseExchangeImporter(ABC):
    def __init__(self, db: DBHandler) -> None:
        self.db = db
        self.history_db = DBHistoryEvents(self.db)
        self._trades: list[Trade] = []
        self._margin_trades: list[MarginPosition] = []
        self._asset_movements: list[AssetMovement] = []
        self._history_events: list[HistoryBaseEntry] = []

    def import_csv(self, filepath: Path, **kwargs: Any) -> tuple[bool, str]:
        try:
            with self.db.user_write() as write_cursor:
                self._import_csv(write_cursor, filepath=filepath, **kwargs)
                self.flush_all(write_cursor)
        except InputError as e:
            return False, str(e)
        else:
            return True, ''

    @abstractmethod
    def _import_csv(self, write_cursor: DBCursor, filepath: Path, **kwargs: Any) -> None:
        """The method that processes csv. Should be implemented by subclasses.
        May raise:
        - InputError if one of the rows is malformed
        """

    def add_trade(self, write_cursor: DBCursor, trade: Trade) -> None:
        self._trades.append(trade)
        self.maybe_flush_all(write_cursor)

    def add_margin_trade(self, write_cursor: DBCursor, margin_trade: MarginPosition) -> None:
        self._margin_trades.append(margin_trade)
        self.maybe_flush_all(write_cursor)

    def add_asset_movement(self, write_cursor: DBCursor, asset_movement: AssetMovement) -> None:
        self._asset_movements.append(asset_movement)
        self.maybe_flush_all(write_cursor)

    def add_history_events(self, write_cursor: DBCursor, history_events: list[HistoryBaseEntry]) -> None:  # noqa: E501
        self._history_events.extend(history_events)
        self.maybe_flush_all(write_cursor)

    def maybe_flush_all(self, cursor: DBCursor) -> None:
        if len(self._trades) + len(self._margin_trades) + len(self._asset_movements) + len(self._history_events) >= ITEMS_PER_DB_WRITE:  # noqa: E501
            self.flush_all(cursor)

    def flush_all(self, write_cursor: DBCursor) -> None:
        self.db.add_trades(write_cursor, trades=self._trades)
        self.db.add_margin_positions(write_cursor, margin_positions=self._margin_trades)
        self.db.add_asset_movements(write_cursor, asset_movements=self._asset_movements)
        self.history_db.add_history_events(write_cursor, history=self._history_events)
        self._trades = []
        self._margin_trades = []
        self._asset_movements = []
        self._history_events = []


class UnsupportedCSVEntry(Exception):
    """Thrown for external exchange exported entries we can't import"""


def process_rotki_generic_import_csv_fields(
        csv_row: dict[str, Any],
        currency_colname: str,
) -> tuple[AssetWithOracles, Fee | None, Asset | None, Location, TimestampMS]:
    """
    Process the imported csv for generic rotki trades and events
    """
    location = Location.deserialize(csv_row['Location'])
    timestamp = TimestampMS(deserialize_timestamp(csv_row['Timestamp']))
    fee = Fee(deserialize_asset_amount(csv_row['Fee'])) if csv_row['Fee'] else None
    asset_mapping = LOCATION_TO_ASSET_MAPPING.get(location, asset_from_common_identifier)
    asset = asset_mapping(csv_row[currency_colname])
    fee_currency = (
        asset_mapping(csv_row['Fee Currency'])
        if csv_row['Fee Currency'] and fee is not None else None
    )
    return asset, fee, fee_currency, location, timestamp


def hash_csv_row(csv_row: Mapping[str, Any]) -> str:
    """Convert the row to string and encode it to a hex string to get a unique hash"""
    row_str = str(csv_row).encode()
    return hashlib.sha256(row_str).hexdigest()


def detect_duplicate_event(
        event_type: HistoryEventType,
        event_subtype: HistoryEventSubType,
        amount: AssetAmount,
        asset: Asset,
        timestamp_ms: TimestampMS,
        location: Location,
        event_prefix: str,
        importer: BaseExchangeImporter,
        write_cursor: DBCursor,
) -> bool:
    """Detect if an event with these attributes is already in the database.
    Returns True if the event is found, and False if not found.
    """
    importer.flush_all(write_cursor)  # flush so that the DB check later can work and not miss unwritten events  # noqa: E501
    with importer.db.conn.read_ctx() as read_cursor:
        read_cursor.execute(
            f"SELECT COUNT(*) FROM history_events WHERE "
            f"event_identifier LIKE '{event_prefix}%' "
            "AND asset=? AND amount=? AND timestamp=? AND location=? "
            "AND type=? AND subtype=?",
            (asset.identifier, str(amount), timestamp_ms, location.serialize_for_db(),
             event_type.serialize(), event_subtype.serialize()),
        )
        return read_cursor.fetchone()[0] != 0
