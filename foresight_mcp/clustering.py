"""Lightweight semantic clustering for Foresight memory entities."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("foresight_clustering")

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "is",
    "it",
    "no",
    "not",
    "of",
    "off",
    "on",
    "or",
    "s",
    "t",
    "that",
    "the",
    "to",
    "was",
    "with",
}


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = _WORD_RE.findall(text)
    return [token for token in tokens if token not in _STOP_WORDS and len(token) > 2]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union)


@dataclass(frozen=True)
class ClusterResult:
    """Output of a memory clustering run."""

    cluster_entities: list[dict[str, Any]]
    memory_links: list[dict[str, Any]]


def cluster_memories(
    memories: list[dict[str, Any]],
    *,
    min_similarity: float = 0.25,
    min_cluster_size: int = 2,
    max_clusters: int | None = 20,
) -> ClusterResult:
    """Group memories into semantic clusters without requiring embeddings.

    The implementation uses token-set Jaccard similarity as a cheap
    stand-in for semantic distance and merges the densest pairs greedily.
    This matches the current repository's local-only dependency
    constraint while keeping the behavior deterministic and testable.
    """
    if len(memories) < min_cluster_size:
        return ClusterResult(cluster_entities=[], memory_links=[])

    cleaned = _clean_memories(memories)
    if len(cleaned) < min_cluster_size:
        return ClusterResult(cluster_entities=[], memory_links=[])

    affinity = _find_best_pair_affinity(cleaned, min_similarity)
    if affinity < min_similarity:
        return ClusterResult(cluster_entities=[], memory_links=[])

    adjacency = _build_adjacency(cleaned, affinity)
    cluster_entities, memory_links = _form_clusters(cleaned, adjacency, min_cluster_size, affinity)
    cluster_entities, memory_links = _apply_cluster_limit(cluster_entities, memory_links, max_clusters)

    return ClusterResult(cluster_entities=cluster_entities, memory_links=memory_links)


def _clean_memories(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clean and tokenize memories for clustering."""
    cleaned: list[dict[str, Any]] = []
    for memory in memories:
        content = str(memory.get("content") or "")
        tokens = _tokenize(content)
        if not tokens:
            continue
        cleaned.append(
            {
                "id": str(memory.get("id")),
                "user_id": str(memory.get("user_id")),
                "tenant_id": str(memory.get("tenant_id") or "default"),
                "content": content,
                "tokens": set(tokens),
            }
        )
    return cleaned


def _find_best_pair_affinity(cleaned: list[dict[str, Any]], min_similarity: float) -> float:
    """Find the highest Jaccard similarity between any pair of memories."""
    n = len(cleaned)
    best = (-1.0, -1, -1)
    for i in range(n):
        for j in range(i + 1, n):
            score = _jaccard(cleaned[i]["tokens"], cleaned[j]["tokens"])
            if score > best[0]:
                best = (score, i, j)
    return best[0]


def _build_adjacency(cleaned: list[dict[str, Any]], affinity: float) -> list[set[int]]:
    """Build adjacency list based on affinity threshold."""
    n = len(cleaned)
    adjacency: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            score = _jaccard(cleaned[i]["tokens"], cleaned[j]["tokens"])
            if score >= affinity:
                adjacency[i].add(j)
                adjacency[j].add(i)
    return adjacency


def _form_clusters(
    cleaned: list[dict[str, Any]], adjacency: list[set[int]], min_cluster_size: int, affinity: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Form clusters from adjacency list and create cluster entities and memory links."""
    seen: set[int] = set()
    cluster_entities: list[dict[str, Any]] = []
    memory_links: list[dict[str, Any]] = []

    for i in range(len(cleaned)):
        if i in seen:
            continue
        component = set()
        queue = [i]
        while queue:
            node = queue.pop()
            if node in component:
                continue
            component.add(node)
            queue.extend(adjacency[node] - component)

        if len(component) < min_cluster_size:
            seen.update(component)
            continue

        members = [cleaned[idx] for idx in sorted(component)]
        seen.update(component)

        cluster_label = "cluster"
        label_candidates = []
        for member in members:
            tokens = sorted(member["tokens"])
            label_candidates.extend(tokens[:3])

        if label_candidates:
            best_label = sorted(
                set(label_candidates),
                key=lambda token: (-label_candidates.count(token), token),
            )[0]
            cluster_label = f"{cluster_label}:{best_label}"

        cluster_id = _build_cluster_id(cluster_label, members[0]["tenant_id"])
        cluster_entities.append(
            {
                "id": cluster_id,
                "tenant_id": members[0]["tenant_id"],
                "user_id": members[0]["user_id"],
                "name": cluster_label,
                "entity_type": "cluster",
                "description": f"{len(members)} memories clustered by {round(affinity, 2)} similarity",
                "properties": {
                    "size": len(members),
                    "affinity": round(affinity, 2),
                    "member_ids": [member["id"] for member in members],
                },
            }
        )

        for member in members:
            memory_links.append(
                {
                    "memory_id": member["id"],
                    "entity_id": cluster_id,
                    "tenant_id": member["tenant_id"],
                    "user_id": member["user_id"],
                    "relevance_score": affinity,
                }
            )

    return cluster_entities, memory_links


def _apply_cluster_limit(
    cluster_entities: list[dict[str, Any]], memory_links: list[dict[str, Any]], max_clusters: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply maximum cluster limit if specified."""
    if max_clusters is not None and len(cluster_entities) > max_clusters:
        cluster_entities = cluster_entities[:max_clusters]
        allowed_ids = {entity["id"] for entity in cluster_entities}
        memory_links = [link for link in memory_links if link["entity_id"] in allowed_ids]
    return cluster_entities, memory_links


def _build_cluster_id(cluster_name: str, tenant_id: str) -> str:
    raw = f"{tenant_id}:{cluster_name}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"cluster:{digest}"
