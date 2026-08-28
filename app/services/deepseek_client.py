from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class GeneratedAsset:
    lib_type: str
    category: str
    title: str
    content: str
    tags: list[str]
    reason: str


class DeepSeekClient:
    def __init__(self, timeout_seconds: float = 90.0) -> None:
        self.timeout_seconds = timeout_seconds

    def _api_key(self) -> str:
        if not settings.deepseek_key_file.exists():
            raise RuntimeError("DEEPSEEK_KEY_NOT_FOUND")
        key = settings.deepseek_key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError("DEEPSEEK_KEY_EMPTY")
        return key

    def generate_support_assets(self, profile: dict, knowledge_titles: list[str]) -> list[GeneratedAsset]:
        prompt = {
            "task": "为贵州文旅博主生成素材库和算法库测试条目",
            "profile": profile,
            "verified_knowledge_titles": knowledge_titles,
            "requirements": {
                "material_count": 5,
                "algorithm_count": 3,
                "rules": [
                    "只生成创作模板和平台策略，不新增或编造景点、店铺、价格和收益事实",
                    "素材库必须贴合博主风格",
                    "算法库必须贴合博主平台",
                    "每条必须包含生成理由",
                ],
            },
            "output_schema": {
                "assets": [
                    {
                        "lib_type": "material|algorithm",
                        "category": "string",
                        "title": "string",
                        "content": "string",
                        "tags": ["string"],
                        "reason": "string",
                    }
                ]
            },
        }
        response = httpx.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json={
                "model": settings.deepseek_model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是严谨的贵州文旅内容资产架构师，只输出合法 JSON。",
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        assets = [GeneratedAsset(**item) for item in parsed.get("assets", [])]
        material_count = sum(item.lib_type == "material" for item in assets)
        algorithm_count = sum(item.lib_type == "algorithm" for item in assets)
        if material_count < 5 or algorithm_count < 3:
            raise RuntimeError("DEEPSEEK_ASSET_COUNT_INVALID")
        if any(item.lib_type not in {"material", "algorithm"} for item in assets):
            raise RuntimeError("DEEPSEEK_LIB_TYPE_INVALID")
        return assets


class FakeDeepSeekClient(DeepSeekClient):
    def generate_support_assets(self, profile: dict, knowledge_titles: list[str]) -> list[GeneratedAsset]:
        style = profile.get("style", "口播")
        platform = profile.get("platform", "抖音")
        materials = [
            GeneratedAsset(
                lib_type="material",
                category=f"{style}模板",
                title=f"{style}内容模板{i + 1}",
                content=f"围绕可信知识条目组织的{style}创作结构，第{i + 1}种节奏。",
                tags=[style, "创作模板"],
                reason="用于测试素材库数量、风格匹配和向量检索",
            )
            for i in range(5)
        ]
        algorithms = [
            GeneratedAsset(
                lib_type="algorithm",
                category=f"{platform}策略",
                title=f"{platform}发布策略{i + 1}",
                content=f"面向{platform}的第{i + 1}项内容结构检查规则。",
                tags=[platform, "平台策略"],
                reason="用于测试算法库数量和平台匹配",
            )
            for i in range(3)
        ]
        return materials + algorithms
