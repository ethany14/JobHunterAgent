from evals.run_memory_adherence import DATASET, grade_case, run


def test_memory_adherence_dataset_and_offline_mode():
    assert DATASET.exists()
    report = run(live=False)
    assert report["run_metadata"]["dataset_version"] == "memory_adherence_v1"
    assert report["metrics"] is None
    assert len(report["results"]) == 0


def test_memory_adherence_grading_is_deterministic():
    direct = {
        "expect_memory": True,
        "expected_answer_phrases": ["no more than two"],
    }
    assert grade_case(
        direct,
        response_text="Use no more than two sentences.",
        memory_ids=["memory-1"],
    )["passed"]
    assert not grade_case(
        direct,
        response_text="Use three to five sentences.",
        memory_ids=["memory-1"],
    )["passed"]
