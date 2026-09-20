# Career Evidence Vault v0.1

Career Evidence stores candidate career statements separately from Memory (preferences and profile context), Skills (procedures), Job requirements (employer requests), and Application artifacts (generated output). Neither a JD nor generated text is a permitted evidence source. Confirmation means the user accepted the statement or a resume quote matched an accepted source; it is **not independent fact checking**.

Each item has a stable UUID and an optimistic state version. Immutable content versions have distinct UUIDs and deterministic hashes. A proposed revision creates a candidate version without rewriting the historical confirmed version. State changes and append-only events commit in one transaction. Application links cite a specific confirmed version; deleting an application removes its links, not the reusable evidence. Historical version records remain after supersession or archiving.

Resume imports validate each exact quote against the original resume using the existing whitespace/case normalization, preserve its existing `ResumeEvidence.evidence_id` as an external ID, and deduplicate replay by source and content hash. Manual user statements enter as candidates and require explicit confirmation. Document claims need a quote and source reference; this release does not ingest or verify external documents. Metrics must carry their original wording, value, unit, and qualifier. A confirmed statement authorizes no stronger wording or inferred leadership, impact, scale, or metrics.

For a Session model call, the retriever selects only confirmed evidence relevant to the current linked Application or query. It uses a separate token budget and renders an explicitly bounded `CONFIRMED CAREER EVIDENCE` block. Prepared Context Snapshots record exactly the included evidence IDs, version IDs, and content hashes. Snapshot recovery checks those immutable versions. The existing LangGraph resume workflow remains unchanged; this Vault is not yet wired into resume Writer or Verifier.

The `/api/evidence` endpoints and Chrome Context manager are for trusted, local single-user use. There is no account authorization or remote access control. SQLite stores career statements and quotes in plaintext; use local filesystem protections and backups. Archiving hides evidence from new retrieval but keeps audit history and historical links. Hard deletion, retention policies, external document ingestion, interviewer-generated evidence, and automatic conversation extraction are deferred.

To try the local API, start FastAPI after migration and submit a user-attested candidate. Confirmation is deliberately separate:

```json
POST /api/evidence/candidates
{"category":"experience","source_type":"user_attested","claim_text":"Built Python APIs.","exact_quote":"Built Python APIs."}

POST /api/evidence/{evidence_id}/confirm
{"expected_version":1}
```

The Context tab can create, confirm, reject, search, filter, and inspect version history. Reload the unpacked extension after changing its files. Open a saved Application before opening Context to see its evidence-link indicators. Manual Chrome interaction was not automated by the Python suite.
