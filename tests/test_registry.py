"""
Tests for DeleGate worker registry.
"""
import pytest
from datetime import datetime

from delegate.models import (
    WorkerManifest,
    WorkerCapability,
    TrustInfo,
    TrustTier,
    TrustPolicy,
    PerformanceHints,
    WorkerAvailability,
    WorkerAvailabilityInfo,
)
from delegate.registry import WorkerRegistry


class TestWorkerRegistration:
    """Test worker registration"""

    @pytest.fixture
    def registry(self):
        return WorkerRegistry()

    @pytest.mark.asyncio
    async def test_register_worker(self, registry):
        """Worker can be registered"""
        manifest = WorkerManifest(
            worker_id="test-worker",
            worker_name="Test Worker",
            trust=TrustInfo(declared_tier=TrustTier.SANDBOX),
            capabilities=[
                WorkerCapability(
                    tool_name="test_tool",
                    description="A test tool",
                    semantic_tags=["test"],
                ),
            ],
        )

        registered = await registry.register(manifest)

        assert registered.worker_id == "test-worker"
        assert registered.registered_at is not None
        assert registered.last_seen is not None

    @pytest.mark.asyncio
    async def test_update_worker_registration(self, registry):
        """Re-registering updates existing worker"""
        manifest1 = WorkerManifest(
            worker_id="test-worker",
            worker_name="Test Worker v1",
            version="1.0.0",
            trust=TrustInfo(declared_tier=TrustTier.SANDBOX),
        )

        manifest2 = WorkerManifest(
            worker_id="test-worker",
            worker_name="Test Worker v2",
            version="2.0.0",
            trust=TrustInfo(declared_tier=TrustTier.VERIFIED),
        )

        await registry.register(manifest1)
        await registry.register(manifest2)

        worker = await registry.get("test-worker")
        assert worker.worker_name == "Test Worker v2"
        assert worker.version == "2.0.0"

    @pytest.mark.asyncio
    async def test_unregister_worker(self, registry):
        """Worker can be unregistered"""
        manifest = WorkerManifest(
            worker_id="test-worker",
            worker_name="Test",
            trust=TrustInfo(declared_tier=TrustTier.SANDBOX),
        )

        await registry.register(manifest)
        assert await registry.get("test-worker") is not None

        success = await registry.unregister("test-worker")
        assert success
        assert await registry.get("test-worker") is None

    @pytest.mark.asyncio
    async def test_unregister_nonexistent(self, registry):
        """Unregistering nonexistent worker returns False"""
        success = await registry.unregister("nonexistent")
        assert success is False


class TestWorkerSearch:
    """Test worker search functionality"""

    @pytest.fixture
    async def populated_registry(self):
        registry = WorkerRegistry()

        # Register multiple workers
        await registry.register(WorkerManifest(
            worker_id="ocr-1",
            worker_name="OCR Service 1",
            trust=TrustInfo(
                declared_tier=TrustTier.VERIFIED,
                verified_tier=TrustTier.VERIFIED,
            ),
            capabilities=[
                WorkerCapability(
                    tool_name="extract_text",
                    description="Extract text from images and PDFs",
                    semantic_tags=["ocr", "document", "text"],
                ),
            ],
        ))

        await registry.register(WorkerManifest(
            worker_id="ocr-2",
            worker_name="Premium OCR",
            trust=TrustInfo(
                declared_tier=TrustTier.TRUSTED,
                verified_tier=TrustTier.TRUSTED,
            ),
            capabilities=[
                WorkerCapability(
                    tool_name="extract_text_premium",
                    description="High-accuracy OCR with handwriting support",
                    semantic_tags=["ocr", "document", "text", "handwriting"],
                ),
            ],
        ))

        await registry.register(WorkerManifest(
            worker_id="code-gen",
            worker_name="Code Generator",
            trust=TrustInfo(declared_tier=TrustTier.SANDBOX),
            capabilities=[
                WorkerCapability(
                    tool_name="generate_code",
                    description="Generate code from specifications",
                    semantic_tags=["code", "programming", "generation"],
                ),
            ],
        ))

        return registry

    @pytest.mark.asyncio
    async def test_search_by_keyword(self, populated_registry):
        """Search finds workers by keyword"""
        results = await populated_registry.search("ocr")

        assert len(results) == 2
        assert all("ocr" in r.worker_id or "OCR" in r.worker_name for r in results)

    @pytest.mark.asyncio
    async def test_search_by_description(self, populated_registry):
        """Search matches description text"""
        results = await populated_registry.search("extract text from images")

        assert len(results) >= 1
        assert results[0].relevance_score > 0

    @pytest.mark.asyncio
    async def test_search_with_trust_filter(self, populated_registry):
        """Search respects trust tier filter"""
        # Only TRUSTED tier
        results = await populated_registry.search(
            "ocr",
            min_trust_tier=TrustTier.TRUSTED,
        )

        assert len(results) == 1
        assert results[0].worker_id == "ocr-2"

    @pytest.mark.asyncio
    async def test_search_limit(self, populated_registry):
        """Search respects limit parameter"""
        results = await populated_registry.search("text", limit=1)
        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_search_relevance_ordering(self, populated_registry):
        """Results are ordered by relevance"""
        results = await populated_registry.search("extract_text")

        # Results should be ordered by score descending
        for i in range(len(results) - 1):
            assert results[i].relevance_score >= results[i + 1].relevance_score


class TestCapabilityMatching:
    """Test capability matching for intent"""

    @pytest.fixture
    async def registry(self):
        registry = WorkerRegistry()

        await registry.register(WorkerManifest(
            worker_id="doc-processor",
            worker_name="Document Processor",
            trust=TrustInfo(
                declared_tier=TrustTier.VERIFIED,
                verified_tier=TrustTier.VERIFIED,
            ),
            capabilities=[
                WorkerCapability(
                    tool_name="process_invoice",
                    description="Process and extract data from invoices",
                    semantic_tags=["invoice", "document", "extraction"],
                ),
                WorkerCapability(
                    tool_name="process_receipt",
                    description="Process and extract data from receipts",
                    semantic_tags=["receipt", "document", "extraction"],
                ),
            ],
        ))

        return registry

    @pytest.mark.asyncio
    async def test_match_intent(self, registry):
        """Match workers to intent"""
        results = await registry.match_intent("extract data from invoice")

        assert len(results) >= 1
        assert "process_invoice" in results[0].matched_capabilities

    @pytest.mark.asyncio
    async def test_match_with_trust_policy(self, registry):
        """Trust policy is respected in matching"""
        results = await registry.match_intent(
            "process invoice",
            trust_policy=TrustPolicy(minimum_worker_tier=TrustTier.TRUSTED),
        )

        # No workers meet TRUSTED tier
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_get_worker_for_tool(self, registry):
        """Get specific worker for tool"""
        worker = await registry.get_worker_for_tool("process_invoice")

        assert worker is not None
        assert worker.worker_id == "doc-processor"


class TestWorkerAvailability:
    """Test worker availability tracking"""

    @pytest.fixture
    async def registry(self):
        registry = WorkerRegistry()

        await registry.register(WorkerManifest(
            worker_id="worker-1",
            worker_name="Worker 1",
            trust=TrustInfo(declared_tier=TrustTier.SANDBOX),
            availability=WorkerAvailabilityInfo(
                status=WorkerAvailability.READY,
                current_load=0.5,
            ),
        ))

        return registry

    @pytest.mark.asyncio
    async def test_update_availability(self, registry):
        """Worker availability can be updated"""
        success = await registry.update_worker_status(
            "worker-1",
            WorkerAvailabilityInfo(
                status=WorkerAvailability.DEGRADED,
                current_load=0.9,
            ),
        )

        assert success
        worker = await registry.get("worker-1")
        assert worker.availability.status == WorkerAvailability.DEGRADED
        assert worker.availability.current_load == 0.9

    @pytest.mark.asyncio
    async def test_offline_workers_not_searched(self, registry):
        """Offline workers are excluded from search"""
        await registry.update_worker_status(
            "worker-1",
            WorkerAvailabilityInfo(status=WorkerAvailability.OFFLINE),
        )

        results = await registry.search("worker")
        assert len(results) == 0


class TestRegistryStats:
    """Test registry statistics"""

    @pytest.mark.asyncio
    async def test_stats_empty_registry(self):
        """Empty registry stats"""
        registry = WorkerRegistry()
        stats = registry.get_stats()

        assert stats["total_workers"] == 0
        assert stats["total_capabilities"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_workers(self):
        """Stats reflect registered workers"""
        registry = WorkerRegistry()

        await registry.register(WorkerManifest(
            worker_id="w1",
            worker_name="W1",
            trust=TrustInfo(declared_tier=TrustTier.SANDBOX),
            capabilities=[
                WorkerCapability(tool_name="t1", description="T1"),
                WorkerCapability(tool_name="t2", description="T2"),
            ],
        ))

        await registry.register(WorkerManifest(
            worker_id="w2",
            worker_name="W2",
            trust=TrustInfo(declared_tier=TrustTier.VERIFIED),
            capabilities=[
                WorkerCapability(tool_name="t3", description="T3"),
            ],
        ))

        stats = registry.get_stats()

        assert stats["total_workers"] == 2
        assert stats["total_capabilities"] == 3
        assert stats["trust_tiers"]["SANDBOX"] == 1
        assert stats["trust_tiers"]["VERIFIED"] == 1
