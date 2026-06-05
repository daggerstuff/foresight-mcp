import time

from foresight_mcp.narrative_cache import NarrativeCache


def test_narrative_cache_put_and_get(tmp_path) -> None:
    cache = NarrativeCache(tmp_path / "narratives.sqlite3")

    cache.put(
        "report-1",
        "cached narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-a",
    )

    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        == "cached narrative"
    )


def test_narrative_cache_tenant_isolation(tmp_path) -> None:
    cache = NarrativeCache(tmp_path / "narratives.sqlite3")
    cache.put(
        "report-1",
        "tenant-a narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-a",
    )

    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        == "tenant-a narrative"
    )
    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-b",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        is None
    )
    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-a",
            user_id="user-2",
            model_version="model-a",
            insights_hash="hash-a",
        )
        is None
    )


def test_narrative_cache_lru_eviction(tmp_path) -> None:
    cache = NarrativeCache(tmp_path / "narratives.sqlite3", max_entries=2)
    for report_id in ("oldest", "middle"):
        cache.put(
            report_id,
            f"{report_id} narrative",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash=report_id,
        )
        time.sleep(0.01)

    assert (
        cache.get(
            "oldest",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="oldest",
        )
        == "oldest narrative"
    )
    time.sleep(0.01)
    cache.put(
        "newest",
        "newest narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="newest",
    )

    assert (
        cache.get(
            "oldest",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="oldest",
        )
        == "oldest narrative"
    )
    assert (
        cache.get(
            "middle",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="middle",
        )
        is None
    )
    assert cache.stats()["size"] == 2


def test_narrative_cache_ttl_expiry(tmp_path) -> None:
    cache = NarrativeCache(tmp_path / "narratives.sqlite3", ttl_seconds=0.01)
    cache.put(
        "report-1",
        "short-lived narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-a",
    )
    time.sleep(0.02)

    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        is None
    )
    assert cache.stats()["size"] == 0


def test_narrative_cache_survives_reopen(tmp_path) -> None:
    path = tmp_path / "narratives.sqlite3"
    cache = NarrativeCache(path)
    cache.put(
        "report-1",
        "persistent narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-a",
    )
    cache.close()

    reopened = NarrativeCache(path)

    assert (
        reopened.get(
            "report-1",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        == "persistent narrative"
    )


def test_narrative_cache_clear_by_tenant(tmp_path) -> None:
    cache = NarrativeCache(tmp_path / "narratives.sqlite3")
    cache.put(
        "report-1",
        "tenant-a narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-a",
    )
    cache.put(
        "report-1",
        "tenant-b narrative",
        tenant_id="tenant-b",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-a",
    )

    assert cache.clear(tenant_id="tenant-a") == 1
    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        is None
    )
    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-b",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        == "tenant-b narrative"
    )


def test_narrative_cache_stats(tmp_path) -> None:
    cache = NarrativeCache(tmp_path / "narratives.sqlite3", max_entries=1)
    cache.put(
        "report-1",
        "report 1 narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-a",
    )

    assert (
        cache.get(
            "report-1",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        == "report 1 narrative"
    )
    assert (
        cache.get(
            "missing",
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="model-a",
            insights_hash="hash-a",
        )
        is None
    )
    cache.put(
        "report-2",
        "report 2 narrative",
        tenant_id="tenant-a",
        user_id="user-1",
        model_version="model-a",
        insights_hash="hash-b",
    )

    stats = cache.stats()
    assert stats["size"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5
    assert stats["eviction_count"] == 1
