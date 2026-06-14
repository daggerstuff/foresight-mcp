"""Minimal local clustering service for Foresight memory entities."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("foresight_cluster_service")

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


def cluster_memories(  # noqa: PLR0912
    memories: list[dict[str, Any]],
    *,
    min_similarity: float = 0.25,
    min_cluster_size: int = 2,
    max_clusters: int | None = 20,
) -> ClusterResult:
    """Group memories into semantic clusters without embeddings.

    Uses token-set Jaccard similarity as a local fallback for semantic
    distance. Implemented with only stdlib deps so it matches the current
    repository constraints.
    """
    if len(memories) < min_cluster_size:
        return ClusterResult(cluster_entities=[], memory_links=[])

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

    if len(cleaned) < min_cluster_size:
        return ClusterResult(cluster_entities=[], memory_links=[])

    adjacency: list[set[int]] = [set() for _ in range(len(cleaned))]
    for i in range(len(cleaned)):
        for j in range(i + 1, len(cleaned)):
            score = _jaccard(cleaned[i]["tokens"], cleaned[j]["tokens"])
            if score >= min_similarity:
                adjacency[i].add(j)
                adjacency[j].add(i)

    seen: set[int] = set()
    cluster_entities: list[dict[str, Any]] = []
    memory_links: list[dict[str, Any]] = []

    for i in range(len(cleaned)):
        if i in seen:
            continue
        component: set[int] = set()
        queue = [i]
        while queue:
            node = queue.pop()
            if node in component:
                continue
            component.add(node)
            queue.extend(sorted(adjacency[node] - component))

        if len(component) < min_cluster_size:
            seen.update(component)
            continue

        members = [cleaned[idx] for idx in sorted(component)]
        seen.update(component)

        cluster_name = "cluster"
        label_candidates: list[str] = []
        for member in members:
            label_candidates.extend(sorted(member["tokens"])[:3])
        if label_candidates:
            cluster_name = f"{cluster_name}:{sorted(set(label_candidates), key=lambda token: (-label_candidates.count(token), token))[0]}"
        cluster_id = _build_cluster_id(cluster_name, members[0]["tenant_id"])
        cluster_entities.append(
            {
                "id": cluster_id,
                "tenant_id": members[0]["tenant_id"],
                "user_id": members[0]["user_id"],
                "name": cluster_name,
                "entity_type": "cluster",
                "description": f"{len(members)} memories",
                "properties": {
                    "size": len(members),
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
                    "relevance_score": 1.0,
                }
            )

    if max_clusters is not None and len(cluster_entities) > max_clusters:
        cluster_entities = cluster_entities[:max_clusters]
        allowed_ids = {cluster["id"] for cluster in cluster_entities}
        memory_links = [link for link in memory_links if link["entity_id"] in allowed_ids]

    return ClusterResult(cluster_entities=cluster_entities, memory_links=memory_links)


def _build_cluster_id(cluster_name: str, tenant_id: str) -> str:
    raw = f"{tenant_id}:{cluster_name}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"cluster:{digest}"
