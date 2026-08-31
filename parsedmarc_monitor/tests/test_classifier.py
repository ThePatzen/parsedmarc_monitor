from ipaddress import ip_network

from dmarc_monitor.classifier import Classification, classify, match_known_source
from dmarc_monitor.models import KnownSourceRule


def rule(name: str, cidr: str | None = None, suffix: str | None = None) -> KnownSourceRule:
    return KnownSourceRule(
        name=name,
        network=ip_network(cidr) if cidr else None,
        rdns_suffix=suffix,
    )


def test_cidr_matches_source_ipv4_and_ipv6() -> None:
    assert match_known_source(
        "203.0.113.25",
        None,
        [rule("Relay", "203.0.113.0/24")],
    ) == "Relay"
    assert match_known_source(
        "2001:db8::25",
        None,
        [rule("Relay6", "2001:db8::/32")],
    ) == "Relay6"


def test_rdns_suffix_obeys_dns_label_boundary() -> None:
    rules = [rule("Provider", suffix="example.at")]
    assert match_known_source("192.0.2.1", "mx1.example.at", rules) == "Provider"
    assert match_known_source("192.0.2.1", "Example.AT.", rules) == "Provider"
    assert match_known_source("192.0.2.1", "notexample.at", rules) is None


def test_rule_matchers_use_or_semantics_and_first_match_wins() -> None:
    rules = [
        rule("Both", "203.0.113.0/24", "example.at"),
        rule("Later", "203.0.113.0/24"),
    ]
    assert match_known_source("203.0.113.9", "other.net", rules) == "Both"
    assert match_known_source("192.0.2.9", "mx.example.at", rules) == "Both"


def test_invalid_source_ip_can_still_match_rdns() -> None:
    rules = [rule("Provider", "203.0.113.0/24", "example.at")]
    assert match_known_source("not-an-ip", "mx.example.at", rules) == "Provider"
    assert match_known_source("not-an-ip", "other.example.net", rules) is None


def test_classification_matrix() -> None:
    assert classify(True, "Relay") is Classification.KNOWN_PASS
    assert classify(False, "Relay") is Classification.KNOWN_FAIL
    assert classify(True, None) is Classification.UNKNOWN_PASS
    assert classify(False, None) is Classification.UNKNOWN_FAIL
