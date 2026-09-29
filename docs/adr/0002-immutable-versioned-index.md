# ADR 0002: Immutable, versioned vector index

**Status:** accepted

## Context

Routing quality depends on the exact (catalog, embedding model) pair. If one replica runs a newer catalog against an old index, results become silently inconsistent, and "which index answered this?" gets hard to answer.

## Decision

- The catalog version is a SHA-256 over every agent file's path and content (line endings normalized).
- The collection name is `<QDRANT_COLLECTION>_<catalog-version>_<embedder-signature>`.
- On startup a replica indexes only if its collection is missing. Existing collections are never mutated.
- The catalog is a pinned git submodule baked into the image, so the image tag fully determines the index.

## Consequences

- Rollouts and rollbacks are safe: old and new replicas each read their own collection.
- Old collections accumulate. A cleanup job should drop collections no deployment references (not implemented yet).
- `/readyz` reports `catalog_version`, which ties a request to the index version that served it.
