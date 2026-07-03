import logging
import os
import sys
from datetime import datetime
from pathlib import Path


def _get_log_path() -> Path:
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    log_dir = Path(appdata) / "Luminque" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    return log_dir / f"sender-{date_str}.log"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(_get_log_path()),
        logging.StreamHandler(sys.stdout),
    ],
)

from luminque.sender.sender import run_sender

if __name__ == "__main__":
    sys.exit(run_sender())
