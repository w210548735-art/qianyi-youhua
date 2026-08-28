from __future__ import annotations

import copy

import pytest
from test_feedback_agent import snapshot

from app.services.feedback_agent import FakeFeedbackAgent
from app.services.feedback_validation_service import (
    FeedbackValidationError,
    FeedbackValidationService,
)


def valid_result(*, source_type: str = "manual", quality: str = "ok") -> tuple[dict, dict]:
    frozen = snapshot(source_type=source_type, quality=quality)
    return FakeFeedbackAgent().analyze([], frozen, request_id="valid"), frozen


def test_validation_accepts_complete_feedback_and_normalizes_evidence_refs():
    payload, frozen = valid_result()
    normalized = FeedbackValidationService().validate_and_normalize(payload, frozen)

    assert normalized["summary"]
    assert normalized["suit_type_candidates"][0]["evidence_refs"][0] == {
        "evidence_type": "metric",
        "ref_id": 20,
    }
    assert {item["lib_type"] for item in normalized["library_evolution"]} == {
        "knowledge",
        "material",
        "algorithm",
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda value: value["suit_type_candidates"][0].update(
                {"evidence_refs": [{"evidence_type": "metric", "ref_id": 999}]}
            ),
            "FEEDBACK_EVIDENCE_INVALID",
        ),
        (
            lambda value: value["asset_effects"][0].update({"asset_id": 999}),
            "FEEDBACK_EVIDENCE_INVALID",
        ),
        (
            lambda value: value["asset_effects"][0].update({"effect_weight": 1.5}),
            "FEEDBACK_RANGE_INVALID",
        ),
        (
            lambda value: value["place_effects"][0].update({"adjust": "multiply"}),
            "FEEDBACK_STRUCTURE_INVALID",
        ),
    ],
)
def test_validation_rejects_invalid_evidence_ownership_and_ranges(mutation, expected_code):
    payload, frozen = valid_result()
    mutation(payload)
    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert error.value.code == expected_code


def test_validation_rejects_cross_blogger_soft_deleted_and_untrusted_snapshot_rows():
    payload, frozen = valid_result()
    cross = copy.deepcopy(frozen)
    cross["assets"][0]["blogger_id"] = 2
    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, cross)
    assert error.value.code == "FEEDBACK_SNAPSHOT_INVALID"

    forged = copy.deepcopy(frozen)
    forged["evidence_whitelist"].append(
        {"evidence_type": "place", "ref_id": 999, "claim": "伪造地点"}
    )
    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, forged)
    assert error.value.code == "FEEDBACK_SNAPSHOT_INVALID"

    deleted = copy.deepcopy(frozen)
    deleted["assets"][0]["deleted_at"] = "2026-01-01T00:00:00"
    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, deleted)
    assert error.value.code == "FEEDBACK_SNAPSHOT_INVALID"

    weak = copy.deepcopy(frozen)
    weak["assets"][0]["credibility"] = 1
    weak["assets"][0]["source_document_ids"] = []
    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, weak)
    assert error.value.code == "FEEDBACK_SNAPSHOT_INVALID"


def test_simulated_feedback_can_only_produce_non_applicable_simulation_candidates():
    payload, frozen = valid_result(source_type="simulated")
    place = payload["place_effects"][0]
    place.update({"after": 100.0, "applicable": True, "simulation_only": False})

    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert error.value.code == "FEEDBACK_SIMULATION_BOUNDARY"


def test_null_commercial_value_requires_explicit_user_confirmed_after_value():
    payload, frozen = valid_result()
    place = payload["place_effects"][0]
    place.update({"after": 100.0, "adjust": "up", "applicable": True})

    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert error.value.code == "FEEDBACK_COMMERCIAL_CONFIRMATION_REQUIRED"

    frozen["user_confirmed_place_updates"] = {"40": {"est_benefit": 100.0}}
    normalized = FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert normalized["place_effects"][0]["after"] == 100.0


def test_low_confidence_name_match_is_never_an_applicable_place_change():
    payload, frozen = valid_result()
    frozen["places"][0].update(
        {"association_source": "controlled_name_match", "association_confidence": "low"}
    )
    payload["place_effects"][0].update({"applicable": True, "adjust": "up"})

    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert error.value.code == "FEEDBACK_PLACE_ASSOCIATION_UNCONFIRMED"


def test_insufficient_sample_rejects_decisive_classification_but_accepts_empty_explanation():
    payload, frozen = valid_result(quality="data_insufficient")
    normalized = FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert normalized["data_quality"]["status"] == "data_insufficient"
    assert normalized["library_evolution"] == []
    assert normalized["insufficient_reason"]

    payload["suit_type_candidates"] = [
        {
            "value": "美食探店",
            "reason": "只看单条播放量",
            "evidence_refs": ["metric:20"],
        }
    ]
    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert error.value.code == "FEEDBACK_SAMPLE_INSUFFICIENT"


def test_library_evolution_requires_all_three_libraries_when_sample_is_sufficient():
    payload, frozen = valid_result()
    payload["library_evolution"] = [
        item for item in payload["library_evolution"] if item["lib_type"] != "algorithm"
    ]
    with pytest.raises(FeedbackValidationError) as error:
        FeedbackValidationService().validate_and_normalize(payload, frozen)
    assert error.value.code == "FEEDBACK_LIBRARY_COVERAGE_INVALID"
