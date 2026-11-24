"""Tests for :mod:`server.config`.

Covers:

* Happy-path YAML load with anchor expansion.
* Fallback to the ``default`` entry when an app is not listed.
* ``FileNotFoundError`` for missing paths and ``ValueError`` for malformed
  YAML or missing ``default`` entry.
* :meth:`AppCaps.to_gate_caps` produces a frozen dataclass the gate
  consumes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os  # noqa: E402

from server.config import (  # noqa: E402
    AppCaps,
    AppsConfig,
    get_caps_for_app,
    load_apps_config,
    reload_apps_config,
    resolve_governor_mode,
)
from server.gate import AppCaps as GateAppCaps  # noqa: E402
from server.gate import GovernorMode  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────
DEFAULTS_YAML = """
defaults: &defaults
  max_batch_size: 40
  per_submit_cap_usd: 5.00
  daily_ceiling_usd: 25.00
  monthly_ceiling_usd: 300.00
  allowed_models:
    - claude-haiku-4-5-20251001
  allow_daemon: false
  allow_large_batch: false

apps:
  default:
    <<: *defaults
  example-rank-batch:
    <<: *defaults
    per_submit_cap_usd: 3.00
  example-deploy-agent:
    <<: *defaults
    daily_ceiling_usd: 15.00
  example-extractor:
    <<: *defaults
    max_batch_size: 0
"""


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    dest = tmp_path / "apps.yaml"
    dest.write_text(DEFAULTS_YAML, encoding="utf-8")
    return dest


# ─── Anchor expansion ───────────────────────────────────────────────────────
def test_anchors_expand_defaults_into_every_entry(yaml_path: Path) -> None:
    config = load_apps_config(yaml_path)

    assert set(config.apps.keys()) == {
        "default",
        "example-rank-batch",
        "example-deploy-agent",
        "example-extractor",
    }
    # example-rank-batch overrides per_submit_cap_usd; all other fields
    # should still come through the anchor.
    pt_rank = config.apps["example-rank-batch"]
    assert pt_rank.per_submit_cap_usd == 3.00
    assert pt_rank.daily_ceiling_usd == 25.00
    assert pt_rank.monthly_ceiling_usd == 300.00
    assert pt_rank.allowed_models == ["claude-haiku-4-5-20251001"]
    assert pt_rank.allow_daemon is False


def test_fields_from_default_when_not_overridden(yaml_path: Path) -> None:
    config = load_apps_config(yaml_path)
    default = config.apps["default"]
    assert default.max_batch_size == 40
    assert default.per_submit_cap_usd == 5.00


# ─── Fallback to default ────────────────────────────────────────────────────
def test_unknown_app_falls_back_to_default(yaml_path: Path) -> None:
    config = load_apps_config(yaml_path)
    caps = config.get_caps_for_app("never-heard-of-this-app")
    assert caps == config.apps["default"]


def test_known_app_returns_its_own_entry(yaml_path: Path) -> None:
    config = load_apps_config(yaml_path)
    caps = config.get_caps_for_app("example-rank-batch")
    assert caps.per_submit_cap_usd == 3.00


# ─── Error cases ────────────────────────────────────────────────────────────
def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_apps_config(tmp_path / "does-not-exist.yaml")


def test_missing_default_raises(tmp_path: Path) -> None:
    bad = tmp_path / "apps.yaml"
    bad.write_text(
        "apps:\n"
        "  example-rank-batch:\n"
        "    max_batch_size: 10\n"
        "    per_submit_cap_usd: 1.0\n"
        "    daily_ceiling_usd: 5.0\n"
        "    monthly_ceiling_usd: 50.0\n"
        "    allowed_models: [claude-haiku-4-5-20251001]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="default"):
        load_apps_config(bad)


def test_malformed_root_raises(tmp_path: Path) -> None:
    bad = tmp_path / "apps.yaml"
    bad.write_text("# no apps key here\njustastring\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_apps_config(bad)


def test_non_mapping_entry_raises(tmp_path: Path) -> None:
    bad = tmp_path / "apps.yaml"
    bad.write_text(
        "apps:\n"
        "  default:\n"
        "    max_batch_size: 1\n"
        "    per_submit_cap_usd: 1.0\n"
        "    daily_ceiling_usd: 1.0\n"
        "    monthly_ceiling_usd: 1.0\n"
        "    allowed_models: []\n"
        "  bad-entry: just a string\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bad-entry"):
        load_apps_config(bad)


# ─── to_gate_caps conversion ────────────────────────────────────────────────
def test_to_gate_caps_returns_frozen_dataclass(yaml_path: Path) -> None:
    config = load_apps_config(yaml_path)
    pt = config.apps["example-rank-batch"]
    gate_caps = pt.to_gate_caps(app="example-rank-batch")
    assert isinstance(gate_caps, GateAppCaps)
    assert gate_caps.app == "example-rank-batch"
    assert gate_caps.per_submit_cap_usd == 3.00
    assert gate_caps.allowed_models == ("claude-haiku-4-5-20251001",)
    # Frozen — attempt to mutate should raise.
    with pytest.raises(Exception):  # FrozenInstanceError
        gate_caps.per_submit_cap_usd = 999.0  # type: ignore[misc]


# ─── Cache + convenience ────────────────────────────────────────────────────
def test_reload_replaces_cache(yaml_path: Path) -> None:
    first = reload_apps_config(yaml_path)
    assert isinstance(first, AppsConfig)
    # Write a modified version; reload should pick it up.
    modified = DEFAULTS_YAML.replace("per_submit_cap_usd: 3.00", "per_submit_cap_usd: 7.77")
    yaml_path.write_text(modified, encoding="utf-8")
    second = reload_apps_config(yaml_path)
    assert second.apps["example-rank-batch"].per_submit_cap_usd == 7.77


def test_get_caps_for_app_top_level(yaml_path: Path) -> None:
    reload_apps_config(yaml_path)
    caps = get_caps_for_app("example-rank-batch", path=yaml_path)
    assert isinstance(caps, AppCaps)
    assert caps.per_submit_cap_usd == 3.00


# ─── Governor mode resolution ───────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_governor_mode_env() -> Any:
    """Ensure GOVERNOR_MODE never leaks between mode-resolution tests."""
    saved = os.environ.pop("GOVERNOR_MODE", None)
    yield
    if saved is not None:
        os.environ["GOVERNOR_MODE"] = saved
    else:
        os.environ.pop("GOVERNOR_MODE", None)


def test_mode_defaults_to_enforcing() -> None:
    assert resolve_governor_mode(None) is GovernorMode.ENFORCING


def test_mode_env_overrides_everything() -> None:
    config = AppsConfig(apps={}, governor_mode="enforcing")
    os.environ["GOVERNOR_MODE"] = "permissive"
    assert resolve_governor_mode(config) is GovernorMode.PERMISSIVE


def test_mode_falls_back_to_config_when_env_unset() -> None:
    config = AppsConfig(apps={}, governor_mode="permissive")
    assert resolve_governor_mode(config) is GovernorMode.PERMISSIVE


def test_mode_garbage_value_falls_back_to_enforcing() -> None:
    os.environ["GOVERNOR_MODE"] = "bogus"
    assert resolve_governor_mode(None) is GovernorMode.ENFORCING


def test_config_file_governor_mode_is_parsed(tmp_path: Path) -> None:
    dest = tmp_path / "apps.yaml"
    dest.write_text("governor_mode: permissive\n" + DEFAULTS_YAML, encoding="utf-8")
    config = load_apps_config(dest)
    assert config.governor_mode == "permissive"
    assert resolve_governor_mode(config) is GovernorMode.PERMISSIVE


# ─── Repo's real config ─────────────────────────────────────────────────────
def test_real_repo_config_parses() -> None:
    """The repo's shipped ``config/apps.yaml`` must always parse. Smoke."""
    config = load_apps_config()
    assert "default" in config.apps
    assert "example-rank-batch" in config.apps
