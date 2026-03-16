"""Projection system: code-defined acceleration tables for known event types."""
from __future__ import annotations

import abc
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from ..models import EventRecord


class BaseProjection(abc.ABC):
    """
    Base class for all projection definitions.

    Subclasses must define as class attributes (not properties):
      - source: str        — matched source_id
      - event_type: str    — matched event type
      - __tablename__: str — SQLAlchemy table name

    Missing any of these raises TypeError at class definition time via
    __init_subclass__.

    And implement:
      - on_insert(session, event) -> list[str]
      - filter(session, event_ids, filters) -> list[UUID]
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Only enforce on concrete (non-abstract) subclasses
        if not getattr(cls, "__abstractmethods__", None):
            for attr in ("source", "event_type", "__tablename__"):
                if not hasattr(cls, attr):
                    raise TypeError(
                        f"{cls.__name__} must define class attribute '{attr}'"
                    )

    @abc.abstractmethod
    async def on_insert(
        self,
        session: "AsyncSession",
        event: "EventRecord",
    ) -> list[str]:
        """
        Write projection fields from event.data to this projection's table.

        Returns:
            List of extra field names found in event.data that are not in
            the projection schema (projection write is skipped in that case).
            Returns an empty list on clean success.
        """

    @abc.abstractmethod
    async def filter(
        self,
        session: "AsyncSession",
        event_ids: list[UUID],
        filters: dict[str, str],
    ) -> list[UUID]:
        """
        Narrow event_ids to those matching projection-specific filters.

        Args:
            session:   async DB session
            event_ids: ordered list from Phase 1 query (ts|id order)
            filters:   dict of raw query-string values, keyed by field name.
                       String column: split on "," → IN list.
                       Numeric column: split on "," → (lower, upper) bounds.
                       Either bound may be empty string → open-ended.

        Returns:
            Ordered subset of event_ids. Must preserve input order.
            If filters contain no recognised fields, return event_ids unchanged.
        """


class ProjectionRegistry:
    """
    Holds a list of BaseProjection instances and routes events/queries to them.

    Usage:
        registry = ProjectionRegistry([LLMCallProjection(), ...])
        warnings = await registry.on_event(session, event)
        filtered  = await registry.filter(source, type, session, ids, filters)
    """

    def __init__(self, projections: list[BaseProjection]) -> None:
        self._projections = projections

    def find(self, source: str | None, event_type: str | None) -> BaseProjection | None:
        """Return the projection that matches (source, event_type), or None."""
        if source is None or event_type is None:
            return None
        for proj in self._projections:
            if proj.source == source and proj.event_type == event_type:
                return proj
        return None

    async def on_event(
        self,
        session: "AsyncSession",
        event: "EventRecord",
    ) -> list[str]:
        """
        Called after an event is committed. Writes to the matching projection.

        Returns:
            List of formatted warning strings (empty if no issues).
        """
        proj = self.find(event.source_id, event.type)
        if proj is None:
            return []

        extra_fields = await proj.on_insert(session, event)
        if extra_fields:
            field_list = ", ".join(extra_fields)
            return [f"projection skipped: unknown fields: [{field_list}]"]
        return []

    async def filter(
        self,
        source: str | None,
        event_type: str | None,
        session: "AsyncSession",
        event_ids: list[UUID],
        filters: dict[str, str],
    ) -> list[UUID]:
        """
        Apply projection-specific filters to a list of event_ids.

        Returns event_ids unchanged if no projection matches or filters is empty.
        """
        proj = self.find(source, event_type)
        if proj is None:
            return event_ids
        if not filters:
            return event_ids
        return await proj.filter(session, event_ids, filters)
