"""Static contracts of the prod side of the monitoring stack (docs/MONITORING.md).

No Docker daemon: Prometheus rules are run by promtool in scripts/validate_monitoring.sh; the tests
here read the YAML files of deploy/monitoring and tests/monitoring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "deploy" / "monitoring"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_every_alert_has_a_case_that_checks_its_environment() -> None:
    rules = _load(MONITORING / "prometheus" / "rules" / "alerts.yml")
    alerts = {
        rule["alert"] for group in rules["groups"] for rule in group["rules"] if "alert" in rule
    }
    cases = _load(ROOT / "tests" / "monitoring" / "alerts.test.yml")
    with_environment = {
        check["alertname"]
        for case in cases["tests"]
        for check in case.get("alert_rule_test", [])
        for expected in check.get("exp_alerts") or []
        if "environment" in expected.get("exp_labels", {})
    }
    assert alerts - with_environment == set()
