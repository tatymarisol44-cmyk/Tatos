"""Where a kind of document may not go (ADR 0014).

A psychotherapy note and a patient's own diary entry are shut out of the places that
reuse text: shared retrieval (RAG), preference memory, insights, campaigns, model use and
the audit export. The shutout does not depend on the tenant's pack: it is the floor. A
pack can only add surfaces to it (`Document` validates that, and `packs._merge` keeps the
union across `extends`), so a tenant on the `general` pack is protected too.

The floor comes from the data class of each kind (`classification.py`): every clinical
kind, not only those two, is shut out of the sinks its class may not reach, and a kind
nobody has classified yet is treated as the strictest class.

Only free-text sinks need a runtime check. Memory is a closed vocabulary of preferences,
and insights and campaigns are computed from CRM rows, so none of them can receive a note;
retrieval is the one sink that takes arbitrary text, and `ensure_allowed` guards it."""

from __future__ import annotations

from orchestrator.classification import DOCUMENT_CLASS, document_class, excluded
from orchestrator.packs import Pack, Surface

HARD_EXCLUSIONS: dict[str, tuple[Surface, ...]] = {
    kind: excluded(data_class)
    for kind, data_class in DOCUMENT_CLASS.items()
    if kind is not None and excluded(data_class)
}


class SurfaceDenied(PermissionError):
    """A document of this kind may not enter that surface."""


def excluded_surfaces(pack: Pack, kind: str) -> set[Surface]:
    """Every surface a document of `kind` is shut out of for a tenant on `pack`."""
    surfaces: set[Surface] = set(excluded(document_class(kind)))
    for doc in pack.documents:
        if doc.kind == kind:
            surfaces.update(doc.excluded_from)
    return surfaces


def ensure_allowed(pack: Pack, kind: str | None, surface: Surface) -> None:
    """Raise `SurfaceDenied` if `kind` is shut out of `surface`. `None` is ordinary
    company content (an FAQ, a price list), which has no restriction."""
    if kind is not None and surface in excluded_surfaces(pack, kind):
        raise SurfaceDenied(f"a {kind} may not enter {surface}")
