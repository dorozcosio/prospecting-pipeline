"""
Structured logging setup.
- JSON logs -> logs/run.log
- Human-readable -> console
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs"


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(level: str = "INFO") -> None:
    LOGS_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return  # Already configured

    root.setLevel(level)

    # Console — human-readable
    console = logging.StreamHandler()
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                          datefmt="%H:%M:%S")
    )
    root.addHandler(console)

    # File — JSON
    file_handler = logging.FileHandler(LOGS_DIR / "run.log")
    file_handler.setFormatter(_JSONFormatter())
    root.addHandler(file_handler)
