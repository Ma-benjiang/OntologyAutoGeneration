# Output location defines ontology identity

An output location represents one continuing Ontology Project. The first run generates a stable ontology IRI, and later Full Rebuilds at that location preserve the IRI while regenerating all extraction results; using a new output location creates a new ontology identity. This keeps published entity IRIs stable without introducing extraction caching or a user-managed configuration file.
