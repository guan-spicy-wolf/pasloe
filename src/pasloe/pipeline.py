"""Background pipeline workers for two-stage Pasloe architecture.

Flow:
1. API writes accepted ingress rows.
2. committer moves ingress -> committed events + outbox(projector/webhook).
3. projector/webhook workers consume outbox with lease + retry.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import async_sessionmaker

from . import store
from .webhook_delivery import fire_webhooks

if TYPE_CHECKING:
    from .projections import ProjectionRegistry

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    poll_interval_seconds: float = 0.5
    batch_size: int = 64
    lease_seconds: int = 30
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 60.0


class PipelineRuntime:
    """Owns committer/projector/webhook worker tasks."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        projection_registry: "ProjectionRegistry | None",
        config: PipelineConfig,
    ) -> None:
        self._session_factory = session_factory
        self._projection_registry = projection_registry
        self._config = config
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._instance_id = uuid.uuid4().hex[:8]

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._committer_loop(), name="pasloe-committer"),
            asyncio.create_task(self._projector_loop(), name="pasloe-projector"),
            asyncio.create_task(self._webhook_loop(), name="pasloe-webhook"),
        ]
        logger.info("Pasloe pipeline started (instance=%s)", self._instance_id)

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("Pasloe pipeline stopped (instance=%s)", self._instance_id)

    async def _sleep_or_stop(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._config.poll_interval_seconds,
            )
        except asyncio.TimeoutError:
            return

    async def _committer_loop(self) -> None:
        worker_id = f"committer-{self._instance_id}"
        while not self._stop_event.is_set():
            try:
                async with self._session_factory() as db:
                    claimed_ids = await store.claim_ingress_batch(
                        db,
                        worker_id=worker_id,
                        limit=self._config.batch_size,
                        lease_seconds=self._config.lease_seconds,
                    )
                    await db.commit()

                if not claimed_ids:
                    await self._sleep_or_stop()
                    continue

                for event_id in claimed_ids:
                    async with self._session_factory() as db:
                        row = await store.get_ingress_for_worker(
                            db, event_id=event_id, worker_id=worker_id
                        )
                        if row is None:
                            await db.commit()
                            continue
                        try:
                            await store.commit_ingress(db, row)
                        except Exception as exc:
                            logger.exception("committer failed for ingress event %s", event_id)
                            await store.mark_ingress_retry(
                                db,
                                row,
                                error=str(exc),
                                base_delay_seconds=self._config.retry_base_seconds,
                                max_delay_seconds=self._config.retry_max_seconds,
                            )
                        await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("committer loop error")
                await self._sleep_or_stop()

    async def _projector_loop(self) -> None:
        worker_id = f"projector-{self._instance_id}"
        while not self._stop_event.is_set():
            try:
                async with self._session_factory() as db:
                    claimed_ids = await store.claim_outbox_batch(
                        db,
                        pipeline=store.PIPELINE_PROJECTOR,
                        worker_id=worker_id,
                        limit=self._config.batch_size,
                        lease_seconds=self._config.lease_seconds,
                    )
                    await db.commit()

                if not claimed_ids:
                    await self._sleep_or_stop()
                    continue

                for outbox_id in claimed_ids:
                    async with self._session_factory() as db:
                        row = await store.get_outbox_for_worker(
                            db,
                            outbox_id=outbox_id,
                            worker_id=worker_id,
                        )
                        if row is None:
                            await db.commit()
                            continue
                        try:
                            warnings = await store.apply_projection_for_outbox(
                                db,
                                row,
                                projection_registry=self._projection_registry,
                            )
                            if warnings:
                                logger.warning(
                                    "projection warnings for event %s: %s",
                                    row.event_id,
                                    "; ".join(warnings),
                                )
                            await store.mark_outbox_done(db, row)
                        except Exception as exc:
                            logger.exception("projector failed for outbox event %s", outbox_id)
                            await store.mark_outbox_retry(
                                db,
                                row,
                                error=str(exc),
                                base_delay_seconds=self._config.retry_base_seconds,
                                max_delay_seconds=self._config.retry_max_seconds,
                            )
                        await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("projector loop error")
                await self._sleep_or_stop()

    async def _webhook_loop(self) -> None:
        worker_id = f"webhook-{self._instance_id}"
        while not self._stop_event.is_set():
            try:
                async with self._session_factory() as db:
                    claimed_ids = await store.claim_outbox_batch(
                        db,
                        pipeline=store.PIPELINE_WEBHOOK,
                        worker_id=worker_id,
                        limit=self._config.batch_size,
                        lease_seconds=self._config.lease_seconds,
                    )
                    await db.commit()

                if not claimed_ids:
                    await self._sleep_or_stop()
                    continue

                for outbox_id in claimed_ids:
                    payload = None
                    webhooks = []
                    async with self._session_factory() as db:
                        row = await store.get_outbox_for_worker(
                            db,
                            outbox_id=outbox_id,
                            worker_id=worker_id,
                        )
                        if row is None:
                            await db.commit()
                            continue
                        payload = store.outbox_event_payload(row)
                        webhooks = await store.list_webhooks_for_event(
                            db,
                            event_type=row.type,
                            source_id=row.source_id,
                        )
                        await db.commit()

                    success = await fire_webhooks(webhooks, payload)

                    async with self._session_factory() as db:
                        row = await store.get_outbox_for_worker(
                            db,
                            outbox_id=outbox_id,
                            worker_id=worker_id,
                        )
                        if row is None:
                            await db.commit()
                            continue
                        if success:
                            await store.mark_outbox_done(db, row)
                        else:
                            await store.mark_outbox_retry(
                                db,
                                row,
                                error="webhook delivery failed",
                                base_delay_seconds=self._config.retry_base_seconds,
                                max_delay_seconds=self._config.retry_max_seconds,
                            )
                        await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("webhook loop error")
                await self._sleep_or_stop()

