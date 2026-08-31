from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network


@dataclass(frozen=True, slots=True)
class KnownSourceRule:
    name: str
    network: IPv4Network | IPv6Network | None
    rdns_suffix: str | None


@dataclass(frozen=True, slots=True)
class Settings:
    imap_host: str
    imap_port: int
    imap_ssl: bool
    imap_skip_certificate_verification: bool
    imap_username: str
    imap_password: str
    reports_folder: str
    archive_folder: str
    check_timeout: int
    retention_days: int
    known_sources: tuple[KnownSourceRule, ...]
    log_level: str


@dataclass(frozen=True, slots=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class PersistResult:
    reports_inserted: int
    rows_inserted: int


@dataclass(frozen=True, slots=True)
class CountSummary:
    messages: int
    passed: int
    failed: int
    known_fail: int
    unknown_pass: int
    unknown_fail: int


@dataclass(frozen=True, slots=True)
class ProblemSource:
    source_ip: str
    source_reverse_dns: str | None
    known_source_name: str | None
    classification: str
    message_count: int


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    latest_report_date: str | None
    messages_latest_period: int
    pass_latest_period: int
    fail_latest_period: int
    pass_rate_latest_period: float | None
    messages_30d: int
    pass_rate_30d: float | None
    known_fail_30d: int
    unknown_pass_30d: int
    unknown_fail_30d: int
    last_report: str | None
    last_reporting_org: str | None
    problem: bool
    problem_sources: tuple[ProblemSource, ...]
