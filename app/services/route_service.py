"""第三阶段收益约束路线的确定性排序与证据落库。"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Asset, AssetPlace, DecisionLog, Output, OutputAsset, OutputPlace, Place
from app.services.context_service import ContextService
from app.services.memory_service import MemoryService
from app.services.output_agent import DeepSeekOutputAgent, OutputAgent
from app.services.output_service import OutputService
from app.services.task_memory_service import TaskMemoryService


class RouteServiceError(RuntimeError):
    """路线编排异常，包含稳定错误码和缺失详情。"""

    def __init__(self, code: str, *, status_code: int = 422, details: Any | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.details = details


class RouteService:
    """以确定性公式排序地点，Agent 只能补充说明文本。"""

    prompt_version = "phase3-route-v1"
    weights = {
        "net_benefit": 0.50,
        "like_level": 0.20,
        "fits_koc": 0.10,
        "fits_shoot": 0.10,
        "profile_fit": 0.10,
    }
    trusted_sources = {"official", "government", "unesco", "ihchina", "trusted", "manual"}
    commercial_fields = ("est_cost", "est_benefit", "like_level", "fits_koc", "fits_shoot")

    def __init__(
        self,
        db: Session,
        *,
        agent: OutputAgent | None = None,
        output_service: OutputService | None = None,
        task_service: TaskMemoryService | None = None,
        context_service: ContextService | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.db = db
        self.agent = agent or DeepSeekOutputAgent()
        self.memory_service = memory_service or MemoryService(db)
        self.task_service = task_service or TaskMemoryService(db)
        self.output_service = output_service or OutputService(
            db,
            agent=self.agent,
            task_service=self.task_service,
            memory_service=self.memory_service,
        )
        self.context_service = context_service or ContextService(
            db,
            memory_service=self.memory_service,
            system_rules=(
                "你只能解释后端已经确定的路线顺序，不得修改排序、补造商业字段或添加地点。"
            ),
        )

    def recommend(
        self,
        blogger_id: int,
        assessment_id: int,
        idempotency_key: str,
        *,
        place_ids: Sequence[int] | None = None,
        title: str = "收益约束路线推荐",
        category: str = "路线",
        user_instruction: str = "",
    ) -> Output:
        """验证全部商业输入后排序、解释并原子保存路线与证据。"""

        self.output_service._ready_assessment(blogger_id, assessment_id, "route_rec")
        key = str(idempotency_key).strip()
        if not key:
            raise RouteServiceError("OUTPUT_INVALID_JSON")
        existing = self.db.scalar(
            select(Output).where(Output.blogger_id == blogger_id, Output.idempotency_key == key)
        )
        if existing is not None:
            return existing
        places = self._places(blogger_id, place_ids)
        missing = self.missing_commercial_data(places)
        if missing:
            raise RouteServiceError("ROUTE_COMMERCIAL_DATA_INCOMPLETE", details=missing)

        ranked = self.rank_places(places, self.output_service._active_blogger(blogger_id))
        snapshot = self.output_service.build_snapshot(blogger_id, assessment_id)
        snapshot_hash = self.output_service.calculate_snapshot_hash(snapshot)
        task_id = f"route-{blogger_id}-{sha256(key.encode('utf-8')).hexdigest()[:16]}"
        task = self.task_service.create_task(
            blogger_id,
            "output_route_rec",
            "收益约束路线推荐",
            task_id=task_id,
            initial_context=user_instruction.strip() or "按已确认商业字段生成收益约束路线",
            metadata={"assessment_id": assessment_id, "snapshot_hash": snapshot_hash},
        )
        self.task_service.create_checkpoint(
            task.id,
            {"phase": "ranked", "snapshot_hash": snapshot_hash, "place_ids": [row["place_id"] for row in ranked]},
            context_snapshot="后端已完成路线确定性排序，等待 Agent 仅生成说明",
        )
        context = self.context_service.assemble_context(
            blogger_id,
            json.dumps(
                {"instruction": user_instruction, "formula": self.weights, "ranked": ranked},
                ensure_ascii=False,
            ),
            task_id=task.id,
        )
        explanation = "路线顺序由后端依据已确认商业数据确定。"
        generate_route = getattr(self.agent, "generate_route", None)
        if callable(generate_route):
            agent_snapshot = dict(snapshot)
            agent_snapshot["places"] = [
                next(item for item in snapshot["places"] if int(item["id"]) == row["place_id"])
                for row in ranked
            ]
            raw = generate_route(context.as_messages(), agent_snapshot)
            if isinstance(raw, Mapping):
                explanation = str(raw.get("summary") or raw.get("reason") or explanation)
        # Agent 只提供说明；停靠顺序、分值及商业输入始终取后端结果。
        asset_refs = self._route_assets(blogger_id, [row["place_id"] for row in ranked])
        content = {
            "type": "route_rec",
            "category": category,
            "title": title,
            "stops": ranked,
            "source_refs": asset_refs,
            "formula": {"weights": self.weights, "normalization": "selection_min_max"},
            "summary": explanation,
            "commercial_data_notice": "仅使用用户明确提供或可信来源确认的非NULL字段",
            "snapshot_hash": snapshot_hash,
        }
        if self.output_service.calculate_snapshot_hash(
            self.output_service.build_snapshot(blogger_id, assessment_id)
        ) != snapshot_hash:
            self.task_service.fail_task(task.id, "OUTPUT_SNAPSHOT_CHANGED", error_code="OUTPUT_SNAPSHOT_CHANGED")
            raise RouteServiceError("OUTPUT_SNAPSHOT_CHANGED", status_code=409)

        try:
            output = Output(
                blogger_id=blogger_id,
                task_id=task.id,
                idempotency_key=key,
                type="route_rec",
                category=category,
                title=title,
                content_json=json.dumps(content, ensure_ascii=False, sort_keys=True),
                status="succeeded",
                assessment_id=assessment_id,
                version=1,
                manual_locked=False,
                prompt_version=self.prompt_version,
                model_name=getattr(self.agent, "model_name", "deterministic"),
            )
            self.db.add(output)
            self.db.flush()
            for row in ranked:
                self.db.add(
                    OutputPlace(
                        output_id=output.id,
                        place_id=row["place_id"],
                        role="route_stop",
                        sequence=row["sequence"],
                        claim=row["reason"],
                    )
                )
            for ref in asset_refs:
                self.db.add(
                    OutputAsset(
                        output_id=output.id,
                        asset_id=ref["asset_id"],
                        usage_type="route_knowledge",
                        claim=ref["claim"],
                    )
                )
            decision = DecisionLog(
                blogger_id=blogger_id,
                decision_type="route_recommendation",
                prompt_version=self.prompt_version,
                input_summary=json.dumps(
                    {
                        "assessment_id": assessment_id,
                        "snapshot_hash": snapshot_hash,
                        "places": [
                            {field: row[field] for field in ("place_id", *self.commercial_fields)}
                            for row in ranked
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                decision=json.dumps(
                    {"formula": self.weights, "ranking": ranked}, ensure_ascii=False, sort_keys=True
                ),
                reason="后端确定性排序；Agent只解释，不修改公式、输入值或顺序",
            )
            self.db.add(decision)
            self.db.flush()
            output.decision_id = decision.id
            self.db.commit()
            self.task_service.complete_task(
                task.id,
                {
                    "output_id": output.id,
                    "snapshot_hash": snapshot_hash,
                    "ranked_place_ids": [row["place_id"] for row in ranked],
                },
                memory_candidates=[
                    {
                        "memory_type": "decision_summary",
                        "title": "路线推荐摘要",
                        "content": explanation,
                        "source_type": "output",
                        "source_id": str(output.id),
                        "confidence": 0.7,
                    }
                ],
            )
            self.memory_service.create_memory(
                blogger_id,
                "decision_summary",
                "路线推荐摘要",
                explanation,
                "decision_log",
                decision.id,
                confidence=0.7,
                status="candidate",
                user_confirmed=False,
            )
            return self.output_service.get_output(blogger_id, output.id)
        except SQLAlchemyError as exc:
            self.db.rollback()
            try:
                self.task_service.fail_task(task.id, "OUTPUT_PERSIST_FAILED", error_code="OUTPUT_PERSIST_FAILED")
            except Exception:
                self.db.rollback()
            raise RouteServiceError("OUTPUT_PERSIST_FAILED", status_code=500) from exc

    recommend_route = recommend

    @classmethod
    def rank_places(cls, places: Sequence[Place], blogger: Any) -> list[dict[str, Any]]:
        """对已完整的商业输入做可复算的 min-max 加权排序。"""

        if not places:
            return []
        nets = [
            cls._commercial_number(row.est_benefit) - cls._commercial_number(row.est_cost)
            for row in places
        ]
        low, high = min(nets), max(nets)
        directions = cls._profile_terms(blogger)
        scored: list[dict[str, Any]] = []
        for place, net in zip(places, nets, strict=True):
            net_score = 1.0 if high == low else (net - low) / (high - low)
            profile_text = " ".join(
                [place.name, place.category, place.location or "", place.specialty or "", place.tags_json or ""]
            ).lower()
            profile_fit = 1.0 if any(term in profile_text for term in directions) else 0.0
            components = {
                "net_benefit": net_score,
                "like_level": cls._commercial_number(place.like_level) / 5.0,
                "fits_koc": 1.0 if place.fits_koc else 0.0,
                "fits_shoot": 1.0 if place.fits_shoot else 0.0,
                "profile_fit": profile_fit,
            }
            score = sum(components[name] * weight for name, weight in cls.weights.items()) * 100
            scored.append(
                {
                    "place_id": place.id,
                    "name": place.name,
                    "est_cost": place.est_cost,
                    "est_benefit": place.est_benefit,
                    "net_benefit": net,
                    "like_level": place.like_level,
                    "fits_koc": place.fits_koc,
                    "fits_shoot": place.fits_shoot,
                    "profile_fit": profile_fit,
                    "components": components,
                    "score": round(score, 4),
                }
            )
        scored.sort(key=lambda item: (-float(item["score"]), -float(item["net_benefit"]), int(item["place_id"])))
        for index, row in enumerate(scored, start=1):
            row["sequence"] = index
            row["reason"] = (
                f"后端得分{row['score']:.2f}；净收益{row['net_benefit']:.2f}；"
                f"喜爱度{row['like_level']}；KOC={row['fits_koc']}；拍摄={row['fits_shoot']}"
            )
        return scored

    @staticmethod
    def _commercial_number(value: float | int | None) -> float:
        if value is None:
            raise RouteServiceError("ROUTE_COMMERCIAL_DATA_INCOMPLETE")
        return float(value)

    @classmethod
    def missing_commercial_data(cls, places: Sequence[Place]) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        for row in places:
            fields = [field for field in cls.commercial_fields if getattr(row, field) is None]
            source_confirmed = row.origin == "manual" or (
                row.source_type in cls.trusted_sources and row.credibility >= 3
            )
            if not source_confirmed:
                fields.append("commercial_source")
            if fields:
                missing.append({"place_id": row.id, "name": row.name, "missing_fields": sorted(set(fields))})
        return missing

    def _places(self, blogger_id: int, place_ids: Sequence[int] | None) -> list[Place]:
        self.output_service._active_blogger(blogger_id)
        statement = select(Place).where(Place.blogger_id == blogger_id, Place.deleted_at.is_(None))
        if place_ids is not None:
            selected = {int(item) for item in place_ids}
            if not selected:
                raise RouteServiceError("ROUTE_COMMERCIAL_DATA_INCOMPLETE", details=[])
            statement = statement.where(Place.id.in_(selected))
        rows = list(self.db.scalars(statement.order_by(Place.id)))
        if place_ids is not None and len(rows) != len(set(place_ids)):
            # 跨博主地点与不存在地点统一视为不可见。
            raise RouteServiceError("OUTPUT_NOT_FOUND", status_code=404)
        if not rows:
            raise RouteServiceError("ROUTE_COMMERCIAL_DATA_INCOMPLETE", details=[])
        return rows

    def _route_assets(self, blogger_id: int, place_ids: Sequence[int]) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(AssetPlace, Asset)
            .join(Asset, Asset.id == AssetPlace.asset_id)
            .where(
                AssetPlace.place_id.in_(place_ids),
                Asset.blogger_id == blogger_id,
                Asset.deleted_at.is_(None),
                Asset.credibility >= 3,
            )
            .order_by(Asset.id)
        )
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        for relation, asset in rows:
            if asset.id in seen:
                continue
            seen.add(asset.id)
            result.append(
                {
                    "evidence_type": "asset",
                    "asset_id": asset.id,
                    "usage_type": "route_knowledge",
                    "claim": f"{relation.relation_type}：{asset.title}",
                }
            )
        return result

    @staticmethod
    def _profile_terms(blogger: Any) -> set[str]:
        raw = " ".join(
            str(value or "")
            for value in (
                getattr(blogger, "routes", ""),
                getattr(blogger, "viral_topic", ""),
                getattr(blogger, "suit_type", ""),
                getattr(blogger, "content_types_json", ""),
            )
        ).lower()
        terms = {item.strip(' [],\"，') for item in raw.replace("，", " ").split()}
        return {item for item in terms if len(item) >= 2}


__all__ = ["RouteService", "RouteServiceError"]
