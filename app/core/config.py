from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    app_name: str = "黔衣有话"
    database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'qianyi_youhua.db').as_posix()}"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_key_file: Path = ROOT_DIR / "deepseek_apikey.txt"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimension: int = 512
    embedding_batch_size: int = 32
    seed_file: Path = ROOT_DIR / "app" / "seed" / "guizhou_tourism.json"
    tasks_root: Path = ROOT_DIR / "data" / "tasks"


settings = Settings()
