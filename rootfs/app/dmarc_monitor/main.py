from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import load_mqtt_settings, load_settings
from .db import Database
from .mailbox import MailboxRunner
from .metrics import build_metrics, run_daily_maintenance
from .mqtt import MqttPublisher

OPTIONS_PATH = Path("/data/options.json")
DATABASE_PATH = Path("/data/dmarc.sqlite3")
LOGGER = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        settings = load_settings(OPTIONS_PATH)
        mqtt_settings = load_mqtt_settings(os.environ)
    except ValueError as exc:
        logging.basicConfig(level=logging.INFO)
        LOGGER.error("Invalid startup configuration: %s", exc)
        return 2

    _configure_logging(settings.log_level)
    database = Database(DATABASE_PATH)
    publisher: MqttPublisher | None = None

    try:
        database.initialize()
        run_daily_maintenance(database, settings.retention_days)

        publisher = MqttPublisher(
            mqtt_settings,
            __version__,
            snapshot_provider=lambda: build_metrics(database),
        )
        publisher.start()
        publisher.publish_snapshot(build_metrics(database))

        stop_event = threading.Event()

        def request_stop(signum: int, frame: object) -> None:
            del signum, frame
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        runner = MailboxRunner(settings, database, publisher, stop_event)
        runner.run_forever()
        return 0
    except Exception as exc:
        LOGGER.error("DMARC Monitor stopped after runtime error (%s): %s", type(exc).__name__, " ".join(str(exc).split())[:300])
        return 1
    finally:
        if publisher is not None:
            publisher.stop()


if __name__ == "__main__":
    raise SystemExit(main())
