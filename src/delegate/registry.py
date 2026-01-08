"""
DeleGate Worker Registry

Live registry of available workers through MCP introspection.
Per SPEC-DG-0000 Worker Registry section.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from collections import defaultdict

from delegate.models import (
    WorkerManifest,
    WorkerCapability,
    WorkerSearchResult,
    TrustInfo,
    TrustTier,
    VerificationStatus,
    WorkerAvailability,
    WorkerAvailabilityInfo,
    TrustPolicy,
)
from delegate.config import get_settings

logger = logging.getLogger(__name__)


class WorkerRegistry:
    """
    Live worker registry with capability matching.

    Phase 1: Manual registration via API
    Phase 2: MCP server introspection for live discovery
    """

    def __init__(self):
        self._workers: dict[str, WorkerManifest] = {}
        self._capability_index: dict[str, set[str]] = defaultdict(set)
        self._tag_index: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._cache_ttl = get_settings().capability_cache_ttl_seconds

    async def register(self, manifest: WorkerManifest) -> WorkerManifest:
        """
        Register a worker with the registry.
        Updates existing registration if worker_id already exists.
        """
        async with self._lock:
            now = datetime.utcnow()
            manifest.registered_at = manifest.registered_at or now
            manifest.last_seen = now

            # Perform trust verification (Phase 1: honor system with logging)
            manifest = await self._verify_trust(manifest)

            # Update indexes
            old_manifest = self._workers.get(manifest.worker_id)
            if old_manifest:
                self._remove_from_indexes(old_manifest)

            self._workers[manifest.worker_id] = manifest
            self._add_to_indexes(manifest)

            logger.info(
                f"Worker registered",
                extra={
                    "worker_id": manifest.worker_id,
                    "worker_name": manifest.worker_name,
                    "capabilities": len(manifest.capabilities),
                    "trust_tier": manifest.trust.declared_tier,
                }
            )

            return manifest

    async def unregister(self, worker_id: str) -> bool:
        """Unregister a worker from the registry"""
        async with self._lock:
            if worker_id not in self._workers:
                return False

            manifest = self._workers.pop(worker_id)
            self._remove_from_indexes(manifest)

            logger.info(f"Worker unregistered", extra={"worker_id": worker_id})
            return True

    async def get(self, worker_id: str) -> Optional[WorkerManifest]:
        """Get a worker by ID"""
        return self._workers.get(worker_id)

    async def list_all(self) -> list[WorkerManifest]:
        """List all registered workers"""
        return list(self._workers.values())

    async def search(
        self,
        query: str,
        min_trust_tier: Optional[TrustTier] = None,
        limit: int = 10,
    ) -> list[WorkerSearchResult]:
        """
        Search workers by capability description.
        Phase 1: Simple keyword matching
        Phase 2: Semantic search via embeddings
        """
        query_lower = query.lower()
        query_words = set(query_lower.split())
        results = []

        for worker_id, manifest in self._workers.items():
            # Skip workers below trust threshold
            if min_trust_tier is not None:
                effective_tier = manifest.trust.verified_tier or manifest.trust.declared_tier
                if effective_tier < min_trust_tier:
                    continue

            # Skip offline workers
            if manifest.availability.status == WorkerAvailability.OFFLINE:
                continue

            # Calculate relevance score
            score = 0.0
            matched_capabilities = []

            for cap in manifest.capabilities:
                cap_score = self._calculate_capability_match(
                    query_lower, query_words, cap
                )
                if cap_score > 0:
                    score = max(score, cap_score)
                    matched_capabilities.append(cap.tool_name)

            # Also match against worker name
            if query_lower in manifest.worker_name.lower():
                score = max(score, 0.5)

            if score > 0:
                results.append(WorkerSearchResult(
                    worker_id=worker_id,
                    worker_name=manifest.worker_name,
                    relevance_score=min(1.0, score),
                    matched_capabilities=matched_capabilities,
                    trust=manifest.trust,
                    availability=manifest.availability,
                ))

        # Sort by relevance score descending
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        return results[:limit]

    async def match_intent(
        self,
        intent: str,
        constraints: dict[str, Any] = None,
        trust_policy: Optional[TrustPolicy] = None,
    ) -> list[WorkerSearchResult]:
        """
        Match workers to an intent with constraint filtering.
        Returns ranked list of workers that can fulfill the intent.
        """
        constraints = constraints or {}
        min_tier = None
        if trust_policy:
            min_tier = trust_policy.minimum_worker_tier

        # Search for matching workers
        results = await self.search(intent, min_trust_tier=min_tier, limit=50)

        # Apply additional constraints
        filtered = []
        for result in results:
            manifest = self._workers.get(result.worker_id)
            if not manifest:
                continue

            # Check availability
            if constraints.get("require_ready", False):
                if manifest.availability.status != WorkerAvailability.READY:
                    continue

            # Check load threshold
            max_load = constraints.get("max_load", 1.0)
            if manifest.availability.current_load > max_load:
                continue

            # Check signature requirement
            if trust_policy and trust_policy.require_signatures:
                if not manifest.trust.signature:
                    continue

            filtered.append(result)

        return filtered

    async def update_worker_status(
        self,
        worker_id: str,
        availability: WorkerAvailabilityInfo,
    ) -> bool:
        """Update a worker's availability status"""
        async with self._lock:
            if worker_id not in self._workers:
                return False

            self._workers[worker_id].availability = availability
            self._workers[worker_id].last_seen = datetime.utcnow()
            return True

    async def get_worker_for_tool(
        self,
        tool_name: str,
        trust_policy: Optional[TrustPolicy] = None,
    ) -> Optional[WorkerManifest]:
        """
        Find a worker that provides a specific tool.
        Returns the best available worker based on trust and availability.
        """
        candidates = []

        for worker_id in self._capability_index.get(tool_name.lower(), set()):
            manifest = self._workers.get(worker_id)
            if not manifest:
                continue

            # Check trust policy
            if trust_policy:
                effective_tier = manifest.trust.verified_tier or manifest.trust.declared_tier
                if effective_tier < trust_policy.minimum_worker_tier:
                    continue
                if trust_policy.require_signatures and not manifest.trust.signature:
                    continue

            # Skip unavailable workers
            if manifest.availability.status == WorkerAvailability.OFFLINE:
                continue

            candidates.append(manifest)

        if not candidates:
            return None

        # Sort by: availability (ready first), then trust tier, then load
        def sort_key(m: WorkerManifest):
            avail_score = 0 if m.availability.status == WorkerAvailability.READY else 1
            tier = m.trust.verified_tier or m.trust.declared_tier
            return (avail_score, -tier, m.availability.current_load)

        candidates.sort(key=sort_key)
        return candidates[0]

    def _calculate_capability_match(
        self,
        query_lower: str,
        query_words: set[str],
        capability: WorkerCapability,
    ) -> float:
        """Calculate match score between query and capability"""
        score = 0.0

        # Exact tool name match
        if query_lower == capability.tool_name.lower():
            score = 1.0

        # Tool name contains query
        elif query_lower in capability.tool_name.lower():
            score = max(score, 0.8)

        # Description contains query
        elif query_lower in capability.description.lower():
            score = max(score, 0.6)

        # Tag matching
        tag_matches = sum(
            1 for tag in capability.semantic_tags
            if any(word in tag.lower() for word in query_words)
        )
        if tag_matches > 0:
            score = max(score, 0.4 + 0.1 * min(tag_matches, 4))

        # Word overlap in description
        desc_words = set(capability.description.lower().split())
        overlap = len(query_words & desc_words)
        if overlap > 0:
            score = max(score, 0.3 + 0.1 * min(overlap, 3))

        return score

    def _add_to_indexes(self, manifest: WorkerManifest):
        """Add worker to capability and tag indexes"""
        for cap in manifest.capabilities:
            self._capability_index[cap.tool_name.lower()].add(manifest.worker_id)
            for tag in cap.semantic_tags:
                self._tag_index[tag.lower()].add(manifest.worker_id)

    def _remove_from_indexes(self, manifest: WorkerManifest):
        """Remove worker from capability and tag indexes"""
        for cap in manifest.capabilities:
            self._capability_index[cap.tool_name.lower()].discard(manifest.worker_id)
            for tag in cap.semantic_tags:
                self._tag_index[tag.lower()].discard(manifest.worker_id)

    async def _verify_trust(self, manifest: WorkerManifest) -> WorkerManifest:
        """
        Verify worker trust claims.
        Phase 1: Honor system with logging
        Phase 4: Cryptographic signature verification
        """
        settings = get_settings()

        # Check if untrusted workers are allowed
        if manifest.trust.declared_tier == TrustTier.UNTRUSTED:
            if not settings.allow_untrusted_workers:
                logger.warning(
                    f"Untrusted worker registration blocked",
                    extra={"worker_id": manifest.worker_id}
                )
                raise ValueError("Untrusted worker registration is disabled")

        # Phase 1: Honor system - trust what they declare but log
        if manifest.trust.verified_tier is None:
            # In Phase 1, we accept declared tier but mark verification as unknown
            manifest.trust.verified_tier = manifest.trust.declared_tier
            manifest.trust.verification_status = VerificationStatus.UNKNOWN

            logger.info(
                f"Worker trust accepted on honor system",
                extra={
                    "worker_id": manifest.worker_id,
                    "declared_tier": manifest.trust.declared_tier,
                }
            )

        # Phase 4: Would verify signatures here
        if settings.require_signatures_production:
            if manifest.trust.declared_tier >= TrustTier.VERIFIED:
                if not manifest.trust.signature:
                    logger.warning(
                        f"Worker missing required signature",
                        extra={"worker_id": manifest.worker_id}
                    )
                    manifest.trust.verification_status = VerificationStatus.FAIL
                    manifest.trust.verified_tier = TrustTier.SANDBOX

        return manifest

    def get_stats(self) -> dict[str, Any]:
        """Get registry statistics"""
        tier_counts = defaultdict(int)
        status_counts = defaultdict(int)

        for manifest in self._workers.values():
            tier = manifest.trust.verified_tier or manifest.trust.declared_tier
            tier_counts[tier.name] += 1
            status_counts[manifest.availability.status.value] += 1

        return {
            "total_workers": len(self._workers),
            "total_capabilities": sum(
                len(m.capabilities) for m in self._workers.values()
            ),
            "trust_tiers": dict(tier_counts),
            "availability": dict(status_counts),
            "indexed_tools": len(self._capability_index),
            "indexed_tags": len(self._tag_index),
        }


# Global registry instance
_registry: Optional[WorkerRegistry] = None


def get_registry() -> WorkerRegistry:
    """Get the global worker registry"""
    global _registry
    if _registry is None:
        _registry = WorkerRegistry()
    return _registry


async def init_registry() -> WorkerRegistry:
    """Initialize the global worker registry"""
    return get_registry()
