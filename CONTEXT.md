# Ontology Auto Generation

This context describes how business Markdown becomes a constrained ontology while keeping generated knowledge auditable.

## Language

**Ontology Project**:
The continuing identity of an ontology associated with one output location. It has exactly one declared ontology IRI, which Full Rebuilds preserve while regenerating all extracted knowledge.
_Avoid_: Generation run, extraction cache

**Entity Namespace**:
The stable IRI prefix derived from the Ontology Project IRI and ending in `#`, under which generated classes, properties, and individuals are named.
_Avoid_: Ontology IRI, source URL

**Source Document**:
A Markdown file inside the current workspace that the user explicitly selects as evidence for one generation run. Its canonical source identity is its workspace-relative POSIX path.
_Avoid_: Input directory, automatically discovered document

**TBox View**:
The larger-context projection of Source Documents used to discover schema-level concepts and relationships without admitting business facts.
_Avoid_: Shared chunk stream, schema cache

**ABox View**:
The evidence-focused projection of Source Documents used to extract Candidate Entities and Candidate Assertions after the Schema Card is locked.
_Avoid_: TBox chunk, admitted ABox

**TBox**:
The generated ontology schema containing the supported classes, properties, hierarchies, and schema-level axioms.
_Avoid_: Schema proposal, raw extraction

**ABox**:
The admitted individuals and assertions extracted from Source Documents under the constraints of the locked TBox.
_Avoid_: All extracted facts, candidate data

**Schema Card**:
The canonical, machine-readable representation of the generated TBox and the sole source from which schema OWL and ABox extraction constraints are derived.
_Avoid_: TIP, parallel ontology design document

**Ontology Term**:
A class or property with a published identity in an Ontology Project. A Full Rebuild may update its description or remove it when unsupported, but an unambiguous semantic match retains its IRI.
_Avoid_: Fresh schema proposal, generated label

**Candidate Entity**:
An entity mention extracted from a Source Document before identity resolution and admission.
_Avoid_: Individual

**Canonical Entity**:
An admitted entity identity produced after conservative identity resolution; it becomes an OWL named individual. A business identifier gives it project-wide identity, while an entity without one is scoped to its Source Document, class, and normalized name.
_Avoid_: Mention, automatically merged name

**Observed Alias**:
An evidence-backed surface name retained when multiple Candidate Entities consolidate into one Canonical Entity. It never participates in identity resolution or propagates a business identifier.
_Avoid_: Merge key, inferred alias

**Candidate Assertion**:
A typed data-property or object-property statement extracted from a Source Document before deterministic admission checks.
_Avoid_: Fact

**Candidate Critic**:
A constrained semantic-fidelity review stage that explicitly retains, rejects, or requests re-extraction for every contract-valid candidate based on direct Source Document evidence. Its decisions do not admit candidates, and it cannot create or rewrite an entity or assertion.
_Avoid_: Fact generator, confidence scorer

**Failed Chunk**:
An ABox View chunk whose Entity pass, Assertion pass, or Candidate Critic cannot reach a complete, contract-valid result within its bounded attempts. The chunk is excluded atomically from admission and remains visible in ABox Coverage.
_Avoid_: Skipped chunk, partial chunk, empty result

**ABox Coverage**:
The accounting of every expected ABox View chunk in a Full Rebuild. It is complete only when every expected chunk reaches completion and none is a Failed Chunk; explicit Rejections and valid empty results remain complete coverage.
_Avoid_: Candidate count, ontology size

**Admitted Assertion**:
A Candidate Assertion that has source evidence, conforms to the Schema Card, passes datatype and identity checks, and has no unresolved conflict.
_Avoid_: High-confidence assertion

**Rejection**:
A candidate entity or assertion excluded from the ABox with an explicit machine-readable reason.
_Avoid_: Ignored result, low-confidence result

**Evidence Record**:
A sidecar record linking an admitted or rejected candidate to its source file, heading path, line range, and quotation without adding provenance statements to the ontology.
_Avoid_: OWL annotation

**Full Rebuild**:
A run that regenerates every extraction and ontology artifact from the complete current set of Source Documents without reusing prior extraction results.
_Avoid_: Incremental update, cached rebuild

**Semantic Work Item**:
An immutable, contract-bound unit of CQ, SRD, Schema Card, extraction, criticism, QA, or repair work within a Full Rebuild. Its submitted result cannot advance the rebuild until deterministic validation succeeds.
_Avoid_: Prompt, unconstrained model call

**Release Snapshot**:
An immutable, content-addressed set of terminal artifacts from one Full Rebuild, including its Delivery Status and audit trail whether or not it contains a deliverable ontology.
_Avoid_: Mutable output directory, successful build

**Latest Attempt**:
The most recent terminal Release Snapshot for an Ontology Project, including one with Delivery Status `FAILED`.
_Avoid_: Latest successful build, current ontology

**Latest Delivery**:
The most recent Release Snapshot for an Ontology Project whose Delivery Status is `PASS` or `FORCED_WITH_ERRORS`.
_Avoid_: Latest attempt, guaranteed-valid ontology

**Delivery Status**:
The terminal classification of a Full Rebuild as `PASS`, `FORCED_WITH_ERRORS`, or `FAILED`, derived from ABox Coverage, ontology parseability, and QA results.
_Avoid_: QA Gate status, model verdict

**Forced Delivery**:
A parseable ontology delivered with Delivery Status `FORCED_WITH_ERRORS` because ABox Coverage is incomplete or validation failures remain after bounded repair.
_Avoid_: Successful delivery, failed delivery, best-effort pass
