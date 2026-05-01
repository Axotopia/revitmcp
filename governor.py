"""
RevitMCP Governance Layer — Traffic Governor
=============================================
Middleware that sits between callers and RevitBridge to prevent
STA-thread flooding from aggressive LLM agent retries.

Three subsystems:
  1. Payload Auditor  — pre-validates requests before they touch the pipe.
  2. Request Dedup    — coalesces identical in-flight requests via shared Futures.
  3. Heartbeat Timer  — emits interim "still processing" responses before client
                        timeout, keeping the real task alive in the background.

Usage:
  from governor import RequestGovernor
  gov = RequestGovernor(bridge)
  result = await gov.run_mcp_tool("get_elements_by_category", {...})
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from dotenv import load_dotenv

from coordinate_translator import translator

load_dotenv()

logger = logging.getLogger("revitmcp.governor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HEARTBEAT_THRESHOLD_S = float(os.getenv("GOVERNOR_HEARTBEAT_THRESHOLD_S", "25"))
CACHE_TTL_S = float(os.getenv("GOVERNOR_CACHE_TTL_S", "10"))
MAX_CONCURRENT = int(os.getenv("GOVERNOR_MAX_CONCURRENT", "1"))
DANGEROUS_CATEGORIES = [
    c.strip()
    for c in os.getenv(
        "GOVERNOR_DANGEROUS_CATEGORIES",
        "Generic Models,Detail Items,Lines",
    ).split(",")
    if c.strip()
]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PayloadViolation(Exception):
    """Raised when pre-validation rejects a request before it reaches the pipe."""

    def __init__(self, message: str, code: int = -32602):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class RequestState:
    """Tracks one logical request through its lifecycle."""

    status: Literal["processing", "completed", "failed"] = "processing"
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())
    started_at: float = field(default_factory=time.monotonic)
    result: Optional[Any] = None
    error: Optional[str] = None
    completed_at: Optional[float] = None
    heartbeat_sent: bool = False


# ---------------------------------------------------------------------------
# RequestGovernor
# ---------------------------------------------------------------------------


class RequestGovernor:
    """
    Wraps a RevitBridge instance with dedup, heartbeat, and payload auditing.

    Exposes the same ``run_mcp_tool`` / ``list_mcp_tools`` interface so callers
    can swap in transparently.
    """

    def __init__(self, bridge: Any):
        self._bridge = bridge
        self._active: dict[str, RequestState] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._throttle_delay = 0.5  # Give Revit UI time to breathe between requests
        self._stats = {
            "total_requests": 0,
            "deduped_requests": 0,
            "cache_hits": 0,
            "payload_rejections": 0,
            "heartbeats_sent": 0,
        }

    # ------------------------------------------------------------------
    # Signature computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_signature(method: str, params: Any) -> str:
        """SHA-256 of (method, canonical JSON params) → dedup key."""
        canonical = json.dumps(params, sort_keys=True, default=str)
        raw = f"{method}::{canonical}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # 1. Payload Auditing
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_payload(method: str, params: dict) -> None:
        """
        Pre-validate a request. Raises PayloadViolation if the payload
        is dangerous or malformed.  Runs BEFORE the request enters the queue.
        """
        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            # Rule: geometry on dangerous/massive categories without filters
            if tool_name in ["get_elements_by_category", "query_model"]:
                # query_model nests its params inside an "input" key: {"input": {"categories": [...]}}
                # get_elements_by_category uses a flat "category" string at the top level.
                if tool_name == "query_model":
                    inner = arguments.get("input", {})
                    cats = inner.get("categories", [])
                    if not isinstance(cats, list):
                        cats = [inner.get("category", "")]
                    include_geo = inner.get("include_geometry", False)
                else:
                    cats = [arguments.get("category", "")]
                    include_geo = arguments.get("include_geometry", False)

                for category in cats:
                    if include_geo and category in DANGEROUS_CATEGORIES:
                        raise PayloadViolation(
                            f"Rejected: querying '{category}' with include_geometry=True "
                            f"is extremely expensive. Add a filter or set include_geometry "
                            f"to False. Dangerous categories: {DANGEROUS_CATEGORIES}",
                            code=-32602,
                        )

                # Rule: empty / missing category for get_elements_by_category only
                # (query_model without categories is valid — it queries all elements)
                if tool_name == "get_elements_by_category":
                    if not cats or not any(cats):
                        raise PayloadViolation(
                            "Rejected: 'category' parameter is required for get_elements_by_category "
                            "and cannot be empty. Specify a Revit category such as 'OST_Walls', 'OST_Doors', etc.",
                            code=-32602,
                        )

            # Rule: blank tool name
            if not tool_name or not tool_name.strip():
                raise PayloadViolation(
                    "Rejected: 'name' parameter (tool name) is required. "
                    "Use tools/list to discover available tools.",
                    code=-32602,
                )

    # ------------------------------------------------------------------
    # 2. Request Deduplication
    # ------------------------------------------------------------------

    def _purge_stale(self) -> None:
        """Remove completed/failed entries that have exceeded their TTL."""
        now = time.monotonic()
        stale_keys = [
            sig
            for sig, state in self._active.items()
            if state.status in ("completed", "failed")
            and state.completed_at is not None
            and (now - state.completed_at) > CACHE_TTL_S
        ]
        for key in stale_keys:
            del self._active[key]
            logger.debug("Purged stale cache entry: %s…", key[:12])

    async def _dedup_or_enqueue(self, signature: str) -> tuple[bool, RequestState]:
        """
        Check if an identical request is already in flight or cached.

        Returns:
            (is_new, state) — if is_new is False, the caller should await
            state.future or use state.result directly.
        """
        async with self._lock:
            self._purge_stale()

            if signature in self._active:
                existing = self._active[signature]

                if existing.status == "processing":
                    # Duplicate of an in-flight request — coalesce.
                    self._stats["deduped_requests"] += 1
                    logger.info(
                        "DEDUP: Request %s… already processing (%.1fs elapsed). "
                        "Coalescing duplicate.",
                        signature[:12],
                        time.monotonic() - existing.started_at,
                    )
                    return False, existing

                if existing.status == "completed":
                    # Cache hit — return the stored result.
                    self._stats["cache_hits"] += 1
                    logger.info(
                        "CACHE HIT: Request %s… served from cache (age %.1fs).",
                        signature[:12],
                        time.monotonic() - (existing.completed_at or existing.started_at),
                    )
                    return False, existing

                # If failed, allow retry — remove and fall through.
                del self._active[signature]

            # New request — register it.
            loop = asyncio.get_running_loop()
            state = RequestState(future=loop.create_future())
            self._active[signature] = state
            self._stats["total_requests"] += 1
            logger.info("NEW: Request %s… registered.", signature[:12])
            return True, state

    # ------------------------------------------------------------------
    # 3. Heartbeat / Keepalive Timer
    # ------------------------------------------------------------------

    def _make_interim_response(self, elapsed: float) -> dict:
        """Build the interim 'still processing' response for the LLM."""
        return {
            "_governor_status": "processing",
            "_message": (
                "Tool execution in progress. The Revit host is processing "
                "a complex operation on its main thread. Wait and do not retry."
            ),
            "_elapsed_seconds": round(elapsed, 1),
            "_estimated_remaining": "unknown",
        }

    async def _execute_with_heartbeat(
        self,
        signature: str,
        state: RequestState,
        name: str,
        arguments: dict,
    ) -> Any:
        """
        Execute the real bridge call with a heartbeat watchdog.

        If the call exceeds HEARTBEAT_THRESHOLD_S, return an interim response
        to the *current* awaiter.  The real result is cached under the
        signature so the next request gets the actual data.
        """
        async def _throttled_coro():
            async with self._semaphore:
                try:
                    res = await self._bridge.run_mcp_tool(name, arguments)
                    return res
                finally:
                    # Brief pause to let Revit process its UI message queue
                    await asyncio.sleep(self._throttle_delay)

        task = asyncio.create_task(_throttled_coro())

        try:
            # Wait up to the heartbeat threshold for the real result.
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=HEARTBEAT_THRESHOLD_S
            )
            
            # Apply Coordinate Translation (Path A) before finalizing
            result = await translator.translate_payload(self, result)
            
        except asyncio.TimeoutError:
            # The task is still running — emit an interim heartbeat.
            elapsed = time.monotonic() - state.started_at
            state.heartbeat_sent = True
            self._stats["heartbeats_sent"] += 1
            logger.warning(
                "HEARTBEAT: Request %s… exceeded %.0fs threshold (%.1fs elapsed). "
                "Sending interim response.",
                signature[:12],
                HEARTBEAT_THRESHOLD_S,
                elapsed,
            )

            # Spawn a background continuation that caches the real result.
            asyncio.create_task(
                self._background_completion(signature, state, task)
            )

            return self._make_interim_response(elapsed)

        # Fast path — completed within threshold.
        self._finalize(signature, state, result=result)
        return result

    async def _background_completion(
        self,
        signature: str,
        state: RequestState,
        task: asyncio.Task,
    ) -> None:
        """Wait for a long-running task and cache its result when it finishes."""
        try:
            result = await task
            
            # Apply Coordinate Translation (Path A) before finalizing
            result = await translator.translate_payload(self, result)
            
            self._finalize(signature, state, result=result)
            logger.info(
                "BACKGROUND COMPLETE: Request %s… finished after %.1fs.",
                signature[:12],
                time.monotonic() - state.started_at,
            )
        except Exception as exc:
            self._finalize(signature, state, error=str(exc))
            logger.error(
                "BACKGROUND FAILED: Request %s… errored after %.1fs: %s",
                signature[:12],
                time.monotonic() - state.started_at,
                exc,
            )

    def _finalize(
        self,
        signature: str,
        state: RequestState,
        result: Any = None,
        error: Optional[str] = None,
    ) -> None:
        """Mark a request as completed or failed and resolve its Future."""
        if error:
            state.status = "failed"
            state.error = error
        else:
            state.status = "completed"
            state.result = result
        state.completed_at = time.monotonic()

        # Resolve the shared future so any coalesced waiters unblock.
        if not state.future.done():
            if error:
                state.future.set_exception(Exception(error))
            else:
                state.future.set_result(result)

    # ------------------------------------------------------------------
    # Public API (mirrors RevitBridge)
    # ------------------------------------------------------------------

    async def run_mcp_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """
        Governed replacement for RevitBridge.run_mcp_tool().

        Pipeline: audit → dedup → execute-with-heartbeat → return.
        """
        method = "tools/call"
        params = {"name": name, "arguments": arguments}

        # 1. Payload audit
        try:
            self._audit_payload(method, params)
        except PayloadViolation as pv:
            self._stats["payload_rejections"] += 1
            logger.warning("PAYLOAD REJECTED: %s", pv.message)
            raise

        # 2. Dedup check
        signature = self._compute_signature(method, params)
        is_new, state = await self._dedup_or_enqueue(signature)

        if not is_new:
            if state.status == "completed" and state.result is not None:
                return state.result
            if state.status == "processing":
                # Another caller is already running this — check heartbeat.
                elapsed = time.monotonic() - state.started_at
                if elapsed > HEARTBEAT_THRESHOLD_S:
                    # Already past threshold — send interim immediately.
                    self._stats["heartbeats_sent"] += 1
                    return self._make_interim_response(elapsed)
                # Wait for the original to finish (up to threshold).
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(state.future),
                        timeout=max(0, HEARTBEAT_THRESHOLD_S - elapsed),
                    )
                except asyncio.TimeoutError:
                    self._stats["heartbeats_sent"] += 1
                    return self._make_interim_response(
                        time.monotonic() - state.started_at
                    )

        # 3. Execute with heartbeat watchdog
        return await self._execute_with_heartbeat(signature, state, name, arguments)

    async def list_mcp_tools(self) -> Any:
        """
        Governed replacement for RevitBridge.list_mcp_tools().

        tools/list is lightweight, so we skip heartbeat but still dedup.
        """
        method = "tools/list"
        params = {}

        signature = self._compute_signature(method, params)
        is_new, state = await self._dedup_or_enqueue(signature)

        if not is_new:
            if state.status == "completed" and state.result is not None:
                return state.result
            if state.status == "processing":
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(state.future), timeout=HEARTBEAT_THRESHOLD_S
                    )
                except asyncio.TimeoutError:
                    return self._make_interim_response(
                        time.monotonic() - state.started_at
                    )

        # Direct call — no heartbeat needed for tool listing.
        try:
            result = await self._bridge.list_mcp_tools()
            self._finalize(signature, state, result=result)
            return result
        except Exception as exc:
            self._finalize(signature, state, error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Snapshot of governor state for the /governor/status endpoint."""
        now = time.monotonic()
        active = []
        for sig, state in self._active.items():
            entry = {
                "signature": sig[:12] + "…",
                "status": state.status,
                "elapsed_s": round(now - state.started_at, 1),
                "heartbeat_sent": state.heartbeat_sent,
            }
            if state.completed_at:
                entry["age_s"] = round(now - state.completed_at, 1)
            active.append(entry)

        return {
            "config": {
                "heartbeat_threshold_s": HEARTBEAT_THRESHOLD_S,
                "cache_ttl_s": CACHE_TTL_S,
                "max_concurrent": MAX_CONCURRENT,
                "dangerous_categories": DANGEROUS_CATEGORIES,
            },
            "stats": dict(self._stats),
            "active_requests": active,
        }
