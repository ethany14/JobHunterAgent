import pytest

from evals.human_quality_review.validate import validate_cases, validate_reviews


def cases(count=10):
    return [{
        "case_id": f"real-{index:02d}",
        "source_kind": "anonymized_real",
        "consent_confirmed": True,
        "resume_text": "Anonymized resume evidence.",
        "job_description": "Anonymized job requirements.",
        "generated_artifact": "Generated material.",
    } for index in range(count)]


def test_human_review_requires_ten_to_twenty_anonymized_real_cases():
    validate_cases(cases())
    with pytest.raises(ValueError, match="between 10 and 20"):
        validate_cases(cases(9))
    invalid = cases()
    invalid[0]["source_kind"] = "synthetic"
    with pytest.raises(ValueError, match="anonymized real"):
        validate_cases(invalid)


def test_failed_human_review_requires_reason_and_regression_test():
    values = cases()
    reviews = [{
        "case_id": case["case_id"],
        "reviewer_id": "reviewer",
        "factual_accuracy": 5,
        "non_duplication": 5,
        "application_readiness": 5,
        "failure_reasons": [],
        "regression_test": "",
    } for case in values]
    reviews[0].update(factual_accuracy=2, failure_reasons=["unsupported_claim"])
    with pytest.raises(ValueError, match="without a regression test"):
        validate_reviews(values, reviews)
    reviews[0]["regression_test"] = "tests/test_regression.py::test_case"
    validate_reviews(values, reviews)
