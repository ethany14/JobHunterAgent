# Human material-quality review

This evaluation complements deterministic regression tests with 10–20
**anonymized real resume/job-description pairs**. Synthetic cases from
`evals/stability_cases.json` must not be relabeled or counted as real material.

## Privacy gate

The contributor who owns or has permission to use the material must anonymize it
before it enters this directory. Replace names, email addresses, phone numbers,
street addresses, profile URLs, employer/client names when confidential, and any
other identifying information. Preserve facts needed to judge grounding, such as
technologies, responsibilities, durations, and already-public job requirements.

Do not copy data from the production SQLite database. Do not commit source PDFs.
Keep the private source outside Git and assign an opaque `case_id` such as
`real-backend-01`.

## Dataset

Copy `cases.template.json` to `cases.local.json` and add 10–20 cases. The local
file is ignored by Git. Each case records an anonymized resume, job description,
and the generated artifact being reviewed. It must declare
`source_kind: "anonymized_real"` and `consent_confirmed: true`.

`cohort_plan.json` defines the initial 12-case target across Backend, Data Analyst,
Business Analyst, and AI Engineer roles. Filling those slots requires material from
people who have confirmed its use; the repository intentionally does not fabricate
or relabel synthetic examples as real resumes.

Validate the packet before review:

```powershell
python -m evals.human_quality_review.validate cases.local.json
```

## Review rubric

Use at least two reviewers when possible. Reviewers score each dimension from 1
to 5 and include concrete notes.

1. **Factual accuracy** — every material claim is supported by the anonymized
   resume or explicitly confirmed evidence. A fabricated employer, date, metric,
   responsibility, or technology is an automatic failure.
2. **Non-duplication** — repeated wording within one source entry is removed;
   similar facts from distinct employers/projects remain when they describe
   separate experience.
3. **Application readiness** — the material can be submitted after ordinary
   personal editing, rather than requiring factual reconstruction or a rewrite.

Set `failure_reasons` for every score below 4. Use stable reason codes:

- `unsupported_claim`
- `invented_metadata`
- `within_entry_duplicate`
- `cross_entry_fact_removed`
- `poor_targeting`
- `not_application_ready`
- `other`

## Failure feedback loop

Copy `reviews.template.json` to `reviews.local.json`, enter reviews, then run:

```powershell
python -m evals.human_quality_review.validate cases.local.json --reviews reviews.local.json
```

Every failed case must receive a nonempty `regression_test` field describing the
smallest deterministic or scripted-model test that would prevent recurrence.
After implementing the test, record its repository path in `regression_test`.
Raw real material remains local; only a separately minimized and re-reviewed
synthetic regression fixture may be committed under `tests/` or `evals/`.
