from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from ipaddress import ip_address

from .models import KnownSourceRule


class Classification(StrEnum):
    KNOWN_PASS = "known_pass"
    KNOWN_FAIL = "known_fail"
    UNKNOWN_PASS = "unknown_pass"
    UNKNOWN_FAIL = "unknown_fail"


def match_known_source(
    source_ip: str,
    reverse_dns: str | None,
    rules: Sequence[KnownSourceRule],
) -> str | None:
    try:
        address = ip_address(source_ip)
    except ValueError:
        address = None
    host = reverse_dns.strip().lower().rstrip(".") if reverse_dns else None

    for rule in rules:
        network_match = address is not None and rule.network is not None and address in rule.network
        suffix_match = False
        if host and rule.rdns_suffix:
            suffix = rule.rdns_suffix.lower().strip(".")
            suffix_match = host == suffix or host.endswith("." + suffix)
        if network_match or suffix_match:
            return rule.name
    return None


def classify(dmarc_pass: bool, known_source_name: str | None) -> Classification:
    if known_source_name is not None:
        return Classification.KNOWN_PASS if dmarc_pass else Classification.KNOWN_FAIL
    return Classification.UNKNOWN_PASS if dmarc_pass else Classification.UNKNOWN_FAIL
