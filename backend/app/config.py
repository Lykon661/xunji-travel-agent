import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
load_dotenv(ROOT / ".env")
CRAWLER_REVISION = "d6f7c5bb906b6dac40ddf343ef9e26438a3de092"


def path(value: str) -> Path:
    p = Path(value).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


@dataclass(frozen=True)
class Settings:
    data: Path = path(os.getenv("DATA_DIR", "data"))
    text_url: str = os.getenv("TEXT_BASE_URL", "").rstrip("/")
    text_model: str = os.getenv("TEXT_MODEL", "")
    text_key: str = os.getenv("TEXT_API_KEY", "")
    vision_url: str = os.getenv("VISION_BASE_URL", "").rstrip("/")
    vision_model: str = os.getenv("VISION_MODEL", "")
    vision_key: str = os.getenv("VISION_API_KEY", "")
    model_timeout: int = int(os.getenv("MODEL_TIMEOUT", "120"))
    crawler_enabled: bool = os.getenv("CRAWLER_ENABLED", "false").lower() == "true"
    crawler_dir: Path = path(os.getenv("MEDIACRAWLER_DIR", "../external/MediaCrawler"))
    crawler_python: Path = path(os.getenv("MEDIACRAWLER_PYTHON", "../external/MediaCrawler/.venv/Scripts/python.exe"))
    crawler_timeout: int = int(os.getenv("CRAWLER_TIMEOUT", "240"))
    cache_hours: int = int(os.getenv("CRAWLER_CACHE_HOURS", "24"))
    flyai_command: str = os.getenv("FLYAI_COMMAND", "flyai").strip() or "flyai"
    flyai_enabled: bool = os.getenv("FLYAI_ENABLED", "true").lower() == "true"
    flyai_key: str = os.getenv("FLYAI_API_KEY", "")
    amap_key: str = os.getenv("AMAP_API_KEY", "").strip() or os.getenv("AMAP_MAPS_API_KEY", "").strip()


settings = Settings()
