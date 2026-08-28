from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.services.report_agent import DeepSeekReportAgent, FakeReportAgent, ReportAgentError
from app.services.report_validation_service import ReportValidationError, ReportValidationService


def snapshot() -> dict:
    return {
        "snapshot_hash": "abc",
        "facts": {
            "money": {"status": "data_insufficient", "actual_net": None},
            "traffic": {"status": "actual", "views": 100},
            "product": {"status": "actual", "output_count": 2},
            "supplier": {"status": "data_insufficient", "places": []},
        },
        "evidence_whitelist": ["metric:1", "output:2"],
        "place_names": [],
        "indicator_names": ["总播放量"],
    }


def test_fake_report_agent_is_offline_deterministic_and_valid() -> None:
    agent = FakeReportAgent()
    first = agent.generate(snapshot(), request_id="same")
    second = agent.generate({"different": True}, request_id="same")

    assert first == second
    assert agent.call_count == 1
    assert ReportValidationService().validate(first, snapshot()) == first


def test_report_validation_rejects_agent_numeric_or_conclusion_overreach() -> None:
    invalid = FakeReportAgent().generate(snapshot())
    invalid["sections"]["money"]["status"] = "actual"
    with pytest.raises(ReportValidationError, match="REPORT_CONCLUSION_MISMATCH"):
        ReportValidationService().validate(invalid, snapshot())

    invalid = FakeReportAgent().generate(snapshot())
    invalid["summary"] = "实际净收益 999 元"
    with pytest.raises(ReportValidationError, match="REPORT_NUMBER_NOT_IN_SNAPSHOT"):
        ReportValidationService().validate(invalid, snapshot())


def test_report_validation_rejects_chart_and_unknown_evidence() -> None:
    invalid = FakeReportAgent().generate(snapshot())
    invalid["charts"] = []
    with pytest.raises(ReportValidationError, match="REPORT_AGENT_OVERREACH"):
        ReportValidationService().validate(invalid, snapshot())

    invalid = FakeReportAgent().generate(snapshot())
    invalid["sections"]["traffic"]["evidence_refs"] = ["metric:999"]
    with pytest.raises(ReportValidationError, match="REPORT_EVIDENCE_INVALID"):
        ReportValidationService().validate(invalid, snapshot())


def test_deepseek_report_agent_repairs_invalid_json_once(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    responses = ["not-json", json.dumps(FakeReportAgent().generate(snapshot()), ensure_ascii=False)]
    calls: list[dict] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": responses.pop(0)}}]}

    def post(*_args, **kwargs):
        calls.append(kwargs["json"])
        return Response()

    key_file = tmp_path / "deepseek.key"
    key_file.write_text("secret", encoding="utf-8")
    import app.services.report_agent as module

    monkeypatch.setattr(module, "settings", replace(module.settings, deepseek_key_file=key_file))
    agent = DeepSeekReportAgent(post=post)
    result = agent.generate(snapshot(), request_id="request-1")

    assert result["sections"]["traffic"]["status"] == "actual"
    assert len(calls) == 2
    assert calls[0]["model"] == "deepseek-v4-flash"


def test_deepseek_report_agent_second_invalid_response_is_stable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "[]"}}]}

    key_file = tmp_path / "deepseek.key"
    key_file.write_text("secret", encoding="utf-8")
    import app.services.report_agent as module

    monkeypatch.setattr(module, "settings", replace(module.settings, deepseek_key_file=key_file))
    agent = DeepSeekReportAgent(post=lambda *_args, **_kwargs: Response())

    with pytest.raises(ReportAgentError) as error:
        agent.generate(snapshot())
    assert error.value.code == "REPORT_INVALID_JSON"
    assert agent.call_count == 2
