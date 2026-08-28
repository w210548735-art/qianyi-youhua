from __future__ import annotations

import copy

import pytest

from app.services.output_agent import FakeOutputAgent
from app.services.output_validation_service import OutputValidationError, OutputValidationService


def snapshot() -> dict:
    return {
        "blogger_id": 1,
        "profile": {"id": 1, "platform": "抖音", "style": "口播", "content_types": ["美食"]},
        "sources": [{"id": 101, "title": "官方"}],
        "assets": [
            {
                "id": 11,
                "blogger_id": 1,
                "lib_type": "knowledge",
                "title": "酸汤鱼",
                "content": "事实",
                "credibility": 5,
                "source_document_ids": [101],
            },
            {
                "id": 12,
                "blogger_id": 1,
                "lib_type": "material",
                "title": "模板",
                "content": "模板",
                "credibility": 1,
            },
            {
                "id": 13,
                "blogger_id": 1,
                "lib_type": "algorithm",
                "title": "策略",
                "content": "策略",
                "credibility": 1,
            },
        ],
        "places": [
            {
                "id": 21,
                "blogger_id": 1,
                "name": "酸汤体验点",
                "category": "美食",
                "location": "黔东南",
                "est_cost": None,
                "est_benefit": None,
                "like_level": None,
                "fits_koc": None,
                "fits_shoot": None,
            }
        ],
    }


def valid_script() -> dict:
    return FakeOutputAgent().generate_script([], snapshot(), request_id="script")


def test_validation_accepts_script_and_normalizes_single_source_refs():
    service = OutputValidationService()
    script = valid_script()
    normalized = service.validate_and_normalize(script, "script", snapshot())
    assert normalized["type"] == "script"
    assert normalized["source_refs"]
    assert normalized["source_refs"][0]["asset_id"] == 11


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda value: value["source_refs"].__setitem__(0, {"asset_id": 999}), "OUTPUT_EVIDENCE_INVALID"),
        (
            lambda value: value["source_refs"].__setitem__(0, {"asset_id": 11, "source_document_id": 999}),
            "OUTPUT_EVIDENCE_INVALID",
        ),
        (lambda value: value.update({"platform": "小红书"}), "OUTPUT_INVALID_JSON"),
        (lambda value: value.update({"style": "直播"}), "OUTPUT_INVALID_JSON"),
        (lambda value: value.update({"price": 100}), "OUTPUT_EVIDENCE_INVALID"),
    ],
)
def test_validation_rejects_false_reference_platform_style_and_commercial_data(mutation, expected_code):
    service = OutputValidationService()
    script = valid_script()
    mutation(script)
    with pytest.raises(OutputValidationError) as error:
        service.validate_script(script, snapshot())
    assert error.value.code == expected_code


def test_validation_rejects_soft_deleted_cross_blogger_and_low_trust_knowledge():
    service = OutputValidationService()
    base = valid_script()

    deleted_snapshot = copy.deepcopy(snapshot())
    deleted_snapshot["assets"][0]["deleted_at"] = "2026-01-01"
    with pytest.raises(OutputValidationError) as error:
        service.validate_script(base, deleted_snapshot)
    assert error.value.code == "OUTPUT_EVIDENCE_INVALID"

    cross_snapshot = copy.deepcopy(snapshot())
    cross_snapshot["assets"].append(
        {
            "id": 99,
            "blogger_id": 2,
            "lib_type": "knowledge",
            "title": "其他博主事实",
            "credibility": 5,
            "source_document_ids": [101],
        }
    )
    cross = copy.deepcopy(base)
    cross["source_refs"] = [{"asset_id": 99}]
    with pytest.raises(OutputValidationError) as error:
        service.validate_script(cross, cross_snapshot)
    assert error.value.code == "OUTPUT_EVIDENCE_INVALID"

    weak_snapshot = copy.deepcopy(snapshot())
    weak_snapshot["assets"][0]["credibility"] = 1
    weak_snapshot["assets"][0]["source_document_ids"] = []
    with pytest.raises(OutputValidationError) as error:
        service.validate_script(base, weak_snapshot)
    assert error.value.code == "OUTPUT_EVIDENCE_INVALID"


def test_storyboard_requires_script_version_and_validates_each_shot():
    service = OutputValidationService()
    script = valid_script()
    script.update({"id": 100, "version": 2})
    storyboard = FakeOutputAgent().generate_storyboard([], snapshot(), script, request_id="storyboard")
    result = service.validate_storyboard(storyboard, script, snapshot())
    assert result["script_id"] == 100
    assert result["script_version"] == 2
    assert len(result["shots"]) >= 1

    missing = copy.deepcopy(storyboard)
    missing.pop("script_id")
    with pytest.raises(OutputValidationError) as error:
        service.validate_storyboard(missing, script, snapshot())
    assert error.value.code == "STORYBOARD_SCRIPT_REQUIRED"

    invalid_shot = copy.deepcopy(storyboard)
    invalid_shot["shots"][0]["source_refs"] = [{"asset_id": 999}]
    with pytest.raises(OutputValidationError) as error:
        service.validate_storyboard(invalid_shot, script, snapshot())
    assert error.value.code == "OUTPUT_EVIDENCE_INVALID"


def test_route_and_schedule_preserve_null_commercial_semantics_and_references():
    service = OutputValidationService()
    route = {
        "type": "route_rec",
        "stops": [{"place_id": 21, "sequence": 1, "source_refs": [{"asset_id": 11}]}],
    }
    normalized_route = service.validate_route(route, snapshot())
    assert normalized_route["stops"][0]["place_id"] == 21

    fabricated = copy.deepcopy(route)
    fabricated["stops"][0]["est_cost"] = 0
    with pytest.raises(OutputValidationError) as error:
        service.validate_route(fabricated, snapshot())
    assert error.value.code == "OUTPUT_EVIDENCE_INVALID"

    schedule = {
        "items": [
            {
                "plan_date": "2026-09-01",
                "platform": "抖音",
                "content_type": "script",
                "title": "酸汤鱼",
            }
        ]
    }
    normalized_schedule = service.validate_schedule(schedule, snapshot())
    assert normalized_schedule["items"][0]["plan_date"] == "2026-09-01"
