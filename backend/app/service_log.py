"""Log layout for the background service: timestamps, redacted secrets and startup rotation."""
import logging
import re
from pathlib import Path

SECRET_PARAMETER = re.compile(r"((?:^|[?&\s\"'])(?:key|jscode|securityjscode|api_key|access_token)=)[^&\s\"']+", re.I)


def redact(value):
    return SECRET_PARAMETER.sub(r"\1[已隐藏]", value) if isinstance(value, str) else value


class RedactSecrets(logging.Filter):
    """Mask credential query parameters, e.g. map keys in proxied request paths."""

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


def rotate_log(file: Path, limit: int = 5 * 1024 * 1024, keep: int = 3) -> bool:
    """Rotate before the service opens the log; Windows cannot rename a file that is still open."""
    try:
        if not file.is_file() or file.stat().st_size < limit:
            return False
        for index in range(keep - 1, 0, -1):
            older = file.with_name(f"{file.name}.{index}")
            if older.exists():
                older.replace(file.with_name(f"{file.name}.{index + 1}"))
        file.replace(file.with_name(f"{file.name}.1"))
        return True
    except OSError:
        # Another program holding the log must not prevent the service from starting.
        return False


LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact": {"()": RedactSecrets}},
    "formatters": {
        "default": {"()": "uvicorn.logging.DefaultFormatter", "fmt": "%(asctime)s %(levelprefix)s %(message)s", "use_colors": False},
        "access": {"()": "uvicorn.logging.AccessFormatter", "use_colors": False,
                   "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'},
        "plain": {"format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s"},
    },
    "handlers": {
        "default": {"class": "logging.StreamHandler", "formatter": "default", "filters": ["redact"], "stream": "ext://sys.stderr"},
        "access": {"class": "logging.StreamHandler", "formatter": "access", "filters": ["redact"], "stream": "ext://sys.stdout"},
        "plain": {"class": "logging.StreamHandler", "formatter": "plain", "filters": ["redact"], "stream": "ext://sys.stderr"},
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        # Project loggers report progress; third-party INFO (e.g. httpx request URLs with map keys) stays out.
        "app": {"level": "INFO"},
    },
    "root": {"handlers": ["plain"], "level": "WARNING"},
}
