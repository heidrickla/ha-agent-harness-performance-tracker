"""Fixtures for the Home Assistant layer tests.

These run against Home Assistant, in CI and on a Windows workstation alike.
`tests/winposix.py` supplies what Windows does not have; it is loaded by
`-p tests.winposix` from `pyproject.toml`, and `install_ha_layer_shims()`
below installs the rest.

This conftest lives in its own directory on purpose: its autouse fixture pulls
in Home Assistant machinery, and a conftest in `tests/` would attach that to
the pure-module tests one level up and error them at setup.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from tests.winposix import install_ha_layer_shims

install_ha_layer_shims()

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agent_harness_performance_tracker.const import (
    CONF_AGENT,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

AGENT = "Claude Code on a workstation"
WEBHOOK_ID = "test-webhook-id-0123456789abcdef"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Required for Home Assistant to load a custom component in tests."""
    return


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=AGENT,
        unique_id="claude_code_on_a_workstation",
        data={CONF_AGENT: AGENT, CONF_WEBHOOK_ID: WEBHOOK_ID},
    )


def run(harness: str = "v1", outcome: str = "pass", **extra: Any) -> dict[str, Any]:
    """A run payload with sensible defaults, for the action and the webhook."""
    return {"harness_version": harness, "outcome": outcome, **extra}
