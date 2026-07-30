# Preserve ontology term identities across rebuilds

A Full Rebuild re-induces the TBox from all current Source Documents, but the prior Schema Card remains an identity registry for published Ontology Terms. Unambiguous semantic matches reuse existing IRIs, unsupported terms disappear, and ambiguous matches receive new IRIs with warnings instead of pausing for user mapping. This preserves clear downstream references without treating prior extraction output as a cache, while deliberately accepting possible near-duplicate terms to keep the Skill automatic and simple.
