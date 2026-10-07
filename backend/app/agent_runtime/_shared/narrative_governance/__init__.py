"""Phase 2.9C narrative governance — shared schema and types.

The phase establishes a unified governance layer over Phase 2.9A
(deterministic) and Phase 2.9B (dynamic) public-execution updates.
This package defines the data contracts consumed by ``service`` /
``policy`` / ``dedup`` / ``compressor`` / ``quality`` modules and by
the existing emitter boundary.

Design rules followed here:

* Pydantic v2 ``BaseModel`` (matches the project style); sets in
  governance-context are kept as ``list[str]`` in the wire form so the
  payload can be persisted as JSON without further conversion.
* Forward-compatible — old events without governance fields parse as
  defaults of ``None`` / ``{}``.
* No ORM, no DB session, no async client. The service module may add
  the orchestrating layer with optional async dependencies.
"""
# narrative_governance 子包:纵切治理层(在 narrative_composer 输出之上做 cache/dedup/policy/compressor/quality 二次校验)。
