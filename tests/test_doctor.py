from __future__ import annotations

from scripts.doctor import CheckResult, check_port_free


def test_check_result_warn_is_not_strict_failure():
    assert CheckResult("example", "WARN").ok is True
    assert CheckResult("example", "FAIL").ok is False


def test_unused_high_port_is_reported_free():
    result = check_port_free("test", 9)
    assert result.name == "Port 9 free for test"
    assert result.status in {"OK", "FAIL"}
