from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, Mapping, Protocol

from .metrics import build_metrics, run_daily_maintenance
from .models import Settings

LOGGER = logging.getLogger(__name__)
MAX_UNSAVED_RETRIES = 2_147_483_647
BATCH_SIZE = 10
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300
IMAP_MAX_RETRIES = 4


class StopEvent(Protocol):
    def is_set(self) -> bool: ...
    def wait(self, timeout: float) -> bool: ...


def _sanitized_exception(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


def create_imap_connection(settings: Settings, imap_connection_class: type) -> object:
    """Construct parsedmarc's IMAP connection without logging credentials."""
    return imap_connection_class(
        host=settings.imap_host,
        user=settings.imap_username,
        password=settings.imap_password,
        port=settings.imap_port,
        ssl=settings.imap_ssl,
        verify=not settings.imap_skip_certificate_verification,
        timeout=settings.check_timeout,
        max_retries=IMAP_MAX_RETRIES,
    )


def _default_connection_factory(settings: Settings) -> object:
    from parsedmarc.mail import IMAPConnection

    return create_imap_connection(settings, IMAPConnection)


def _default_parser_config() -> object:
    from parsedmarc import ParserConfig

    return ParserConfig()


def _default_get_reports() -> Callable[..., object]:
    from parsedmarc import get_dmarc_reports_from_mailbox

    return get_dmarc_reports_from_mailbox


def _default_watch_inbox() -> Callable[..., object]:
    from parsedmarc import watch_inbox

    return watch_inbox


class MailboxRunner:
    def __init__(
        self,
        settings: Settings,
        database: object,
        publisher: object,
        stop_event: StopEvent,
        connection_factory: Callable[[Settings], object] | None = None,
        get_reports: Callable[..., object] | None = None,
        watch_inbox: Callable[..., object] | None = None,
        parser_config_factory: Callable[[], object] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.publisher = publisher
        self.stop_event = stop_event
        self.connection_factory = connection_factory or _default_connection_factory
        self.get_reports = get_reports
        self.watch_inbox = watch_inbox
        self.parser_config_factory = parser_config_factory or _default_parser_config

        if settings.imap_skip_certificate_verification:
            LOGGER.warning("IMAP certificate verification is disabled")
        if not settings.imap_ssl:
            LOGGER.warning("IMAP SSL/TLS is disabled; IMAPS is strongly recommended")

    def save_batch(self, batch: Mapping[str, Any]) -> bool:
        """Accept a parsedmarc batch only after SQLite durably accepted it."""
        try:
            self.database.persist_batch(batch, self.settings.known_sources)
        except (sqlite3.Error, OSError) as exc:
            detail = _sanitized_exception(exc)
            LOGGER.error("Unable to persist DMARC batch (%s): %s", type(exc).__name__, detail)
            try:
                self.publisher.set_storage_health(False, detail)
            except Exception:
                LOGGER.exception("Unable to publish storage failure health")
            return False
        except Exception as exc:
            detail = _sanitized_exception(exc)
            LOGGER.error("Unexpected DMARC persistence failure (%s): %s", type(exc).__name__, detail)
            try:
                self.publisher.set_storage_health(False, detail)
            except Exception:
                LOGGER.exception("Unable to publish storage failure health")
            return False

        # From here onward the report is already in SQLite. Auxiliary failures
        # must not make parsedmarc keep/retry an otherwise durable message.
        try:
            run_daily_maintenance(self.database, self.settings.retention_days)
            snapshot = build_metrics(self.database)
            self.publisher.set_storage_health(True, None)
            self.publisher.publish_snapshot(snapshot)
        except Exception as exc:
            LOGGER.error(
                "Post-persistence DMARC update failed (%s): %s",
                type(exc).__name__,
                _sanitized_exception(exc),
            )
        return True

    def _mailbox_kwargs(self, parser_config: object) -> dict[str, object]:
        return {
            "reports_folder": self.settings.reports_folder,
            "archive_folder": self.settings.archive_folder,
            "delete": False,
            "batch_size": BATCH_SIZE,
            "max_unsaved_retries": MAX_UNSAVED_RETRIES,
            "config": parser_config,
        }

    def run_forever(self) -> None:
        get_reports = self.get_reports or _default_get_reports()
        watch_inbox = self.watch_inbox or _default_watch_inbox()
        delay = INITIAL_BACKOFF_SECONDS

        while not self.stop_event.is_set():
            self.publisher.ensure_started()
            try:
                connection = self.connection_factory(self.settings)
                self.publisher.set_imap_ok(True)
                delay = INITIAL_BACKOFF_SECONDS
                parser_config = self.parser_config_factory()
                mailbox_kwargs = self._mailbox_kwargs(parser_config)

                get_reports(connection, save_callback=self.save_batch, **mailbox_kwargs)
                if self.stop_event.is_set():
                    break

                watch_inbox(
                    connection,
                    self.save_batch,
                    check_timeout=self.settings.check_timeout,
                    config_reloading=self.stop_event.is_set,
                    **mailbox_kwargs,
                )
                if not self.stop_event.is_set():
                    raise ConnectionError("IMAP watch ended unexpectedly")
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                try:
                    self.publisher.set_imap_ok(False)
                except Exception:
                    LOGGER.exception("Unable to publish IMAP failure health")
                LOGGER.error(
                    "IMAP processing failed (%s): %s",
                    type(exc).__name__,
                    _sanitized_exception(exc),
                )
                self.stop_event.wait(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
