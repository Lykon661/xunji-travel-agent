"""Read installation completion without launching Python or a browser on each UI poll."""
import hashlib
import json
import re

from .config import CRAWLER_REVISION, settings


def installation_ready():
    environment = settings.crawler_python.parent.parent
    try:
        marker = json.loads((environment / "travel-agent-ready.json").read_text(encoding="utf-8-sig"))
        version = (environment / "pyvenv.cfg").read_text(encoding="utf-8-sig")
        requirements = (settings.crawler_dir / "requirements.txt").read_bytes()
        return bool(settings.crawler_python.is_file() and (settings.crawler_dir / "main.py").is_file()
                    and re.search(r"(?m)^version\s*=\s*3\.11\.", version)
                    and marker.get("revision") == CRAWLER_REVISION
                    and marker.get("complete") is True
                    and marker.get("requirements_sha256", "").lower() == hashlib.sha256(requirements).hexdigest())
    except (OSError, ValueError, TypeError, AttributeError):
        return False
