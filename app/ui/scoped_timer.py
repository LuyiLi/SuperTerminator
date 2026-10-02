from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)


def _is_deleted(owner: Any) -> bool:
    try:
        return bool(getattr(owner, "is_deleted", False))
    except Exception:
        return True


def _looks_like_deleted_slot_error(exc: RuntimeError) -> bool:
    return "parent slot" in str(exc).lower() and "deleted" in str(exc).lower()


async def _call(callback: Callable[[], Any | Awaitable[Any]]) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


async def _call_in_owner_context(
    owner: Any, callback: Callable[[], Any | Awaitable[Any]]
) -> None:
    """Run callback with NiceGUI's slot stack set to the owner element."""

    try:
        with owner:
            await _call(callback)
    except RuntimeError as exc:
        if _looks_like_deleted_slot_error(exc) or _is_deleted(owner):
            return
        raise


def _cancel_when_deleted(owner: Any, task: asyncio.Task[Any]) -> None:
    original = getattr(owner, "_handle_delete", None)
    if not callable(original):
        return

    def handle_delete() -> None:
        if not task.done():
            task.cancel()
        original()

    try:
        setattr(owner, "_handle_delete", handle_delete)
    except Exception:  # pragma: no cover - defensive for non-NiceGUI fakes
        return


def create_scoped_timer(
    owner: Any,
    interval: float,
    callback: Callable[[], Any | Awaitable[Any]],
    *,
    once: bool = False,
    immediate: bool = True,
) -> asyncio.Task[Any] | None:
    """Run *callback* while *owner* exists, cancelling cleanly when its UI is deleted.

    NiceGUI's ``ui.timer`` can keep firing briefly after a page subtree has been
    removed during navigation, which raises noisy ``parent slot ... deleted``
    exceptions before user callbacks even run. This task-based timer is scoped to
    a stable owner element and stops before touching deleted UI.
    """

    async def runner() -> None:
        if not immediate or once:
            await asyncio.sleep(max(interval, 0))
        while not _is_deleted(owner):
            try:
                await _call_in_owner_context(owner, callback)
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                if _looks_like_deleted_slot_error(exc):
                    return
                log.exception("Scoped UI timer callback failed")
            except Exception:
                log.exception("Scoped UI timer callback failed")
            if once:
                return
            await asyncio.sleep(max(interval, 0))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(runner())
    _cancel_when_deleted(owner, task)
    return task
