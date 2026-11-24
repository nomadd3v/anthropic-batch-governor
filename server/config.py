"""Per-app YAML config loader for the batch-governor.

``config/apps.yaml`` is the single source of truth for every app's
caps, model allowlist, and daemon permission. This module loads that
file once at startup, exposes a typed :class:`AppCaps` view, and
falls back to the ``default`` entry when an app is not listed.

PyYAML expands ``<<: *defaults`` anchors natively via ``safe_load``,
so a tight test asserts that behaviour — widening caps by deleting
the anchor would silently under-apply defaults.

No credentials live in this file. API keys, tokens, and DB URLs are
all environment variables — see `the README` non-negotiable rule #2.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, Field

from server.gate import AppCaps as GateAppCaps
from server.gate import GovernorMode


# ─── Constants ───────────────────────────────────────────────────────────────
DEFAULT_APP_KEY: Final[str] = "default"
APPS_YAML_REL_PATH: Final[str] = "config/apps.yaml"
GOVERNOR_MODE_ENV: Final[str] = "GOVERNOR_MODE"


# ─── Schemas ────────────────────────────────────────────────────────────────
class AppCaps(BaseModel):
    """Pydantic view of one app's YAML entry.

    Matches the YAML fields one-for-one. The gate consumes a frozen
    dataclass (``server.gate.AppCaps``) for immutability guarantees on
    the hot path; :meth:`to_gate_caps` converts between the two.
    """

    max_batch_size: int = Field(..., ge=0)
    per_submit_cap_usd: float = Field(..., ge=0.0)
    daily_ceiling_usd: float = Field(..., ge=0.0)
    monthly_ceiling_usd: float = Field(..., ge=0.0)
    allowed_models: list[str] = Field(default_factory=list)
    allow_daemon: bool = False
    allow_large_batch: bool = False

    def to_gate_caps(self, *, app: str) -> GateAppCaps:
        """Return the frozen dataclass the gate consumes."""
        return GateAppCaps(
            app=app,
            max_batch_size=self.max_batch_size,
            per_submit_cap_usd=self.per_submit_cap_usd,
            daily_ceiling_usd=self.daily_ceiling_usd,
            monthly_ceiling_usd=self.monthly_ceiling_usd,
            allowed_models=tuple(self.allowed_models),
            allow_daemon=self.allow_daemon,
            allow_large_batch=self.allow_large_batch,
        )


class AppsConfig(BaseModel):
    """Parsed root of ``config/apps.yaml``."""

    apps: dict[str, AppCaps]
    # Optional file-level governor mode. The ``GOVERNOR_MODE`` env var takes
    # precedence (see :func:`resolve_governor_mode`); this is the config-file
    # equivalent. ``None`` means "not set here — fall through to the default".
    governor_mode: str | None = None

    def get_caps_for_app(self, app: str) -> AppCaps:
        """Return the caps for ``app``, falling back to ``default`` on miss.

        # BUSINESS RULE: Unknown apps must use the conservative ``default``
        # entry (see apps.yaml header) — NEVER the first or last entry.
        """
        if app in self.apps:
            return self.apps[app]
        if DEFAULT_APP_KEY not in self.apps:
            raise KeyError(
                f"apps.yaml missing required '{DEFAULT_APP_KEY}' entry"
            )
        return self.apps[DEFAULT_APP_KEY]


# ─── Loader ─────────────────────────────────────────────────────────────────
def _resolveYamlPath(path: str | Path | None) -> Path:
    """Resolve a provided or default YAML path.

    Defaults to ``<repo-root>/config/apps.yaml`` — ``server/config.py`` →
    ``server/`` → repo root.
    """
    if path is not None:
        return Path(path)
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / APPS_YAML_REL_PATH


def load_apps_config(path: str | Path | None = None) -> AppsConfig:
    """Load and parse ``config/apps.yaml`` into a typed :class:`AppsConfig`.

    YAML anchor expansion (``<<: *defaults``) is handled by PyYAML's
    ``safe_load``; the result is a plain dict we hand to pydantic.

    Raises:
        FileNotFoundError: when the YAML file is missing.
        ValueError: when the file is malformed or missing ``apps:``.
    """
    yaml_path = _resolveYamlPath(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"apps.yaml not found at {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as handle:
        raw: Any = yaml.safe_load(handle)

    if not isinstance(raw, dict) or "apps" not in raw:
        raise ValueError(
            f"apps.yaml malformed: expected top-level 'apps:' key, got "
            f"{type(raw).__name__}"
        )

    apps_dict: dict[str, AppCaps] = {}
    for app_name, entry in raw["apps"].items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"apps.yaml entry for '{app_name}' must be a mapping, got "
                f"{type(entry).__name__}"
            )
        apps_dict[str(app_name)] = AppCaps.model_validate(entry)

    if DEFAULT_APP_KEY not in apps_dict:
        raise ValueError(
            f"apps.yaml must define '{DEFAULT_APP_KEY}' — it is the fallback"
            " for unknown app names"
        )
    raw_mode = raw.get("governor_mode")
    governor_mode = str(raw_mode) if raw_mode is not None else None
    return AppsConfig(apps=apps_dict, governor_mode=governor_mode)


# ─── Cached singleton ───────────────────────────────────────────────────────
_CACHED_CONFIG: AppsConfig | None = None


def get_apps_config(path: str | Path | None = None) -> AppsConfig:
    """Return the process-wide :class:`AppsConfig` singleton.

    The service loads the YAML once at startup (see ``server/app.py``
    lifespan) via :func:`reload_apps_config`. Callers from FastAPI
    dependencies go through this accessor so tests can swap the cache.
    """
    global _CACHED_CONFIG
    if _CACHED_CONFIG is None:
        _CACHED_CONFIG = load_apps_config(path)
    return _CACHED_CONFIG


def reload_apps_config(path: str | Path | None = None) -> AppsConfig:
    """Force re-read the YAML and replace the cached singleton."""
    global _CACHED_CONFIG
    _CACHED_CONFIG = load_apps_config(path)
    return _CACHED_CONFIG


def get_caps_for_app(
    app: str, *, path: str | Path | None = None
) -> AppCaps:
    """Top-level convenience: load config and return one app's caps.

    Used by the CLI's ``budget`` command and any out-of-process script
    that does not want to wire up the FastAPI dependency injector.
    """
    config = get_apps_config(path)
    return config.get_caps_for_app(app)


# ─── Governor mode resolution ───────────────────────────────────────────────
def resolve_governor_mode(config: AppsConfig | None = None) -> GovernorMode:
    """Resolve the active governor mode: env var first, then config, then default.

    Precedence (highest first):

    1. ``GOVERNOR_MODE`` env var (``permissive`` | ``enforcing``).
    2. ``governor_mode`` in ``config/apps.yaml``.
    3. Default ``enforcing`` — unchanged historical behavior.

    Garbage values fall back to ``enforcing`` (see
    :meth:`GovernorMode.from_str`) so a typo never silently disables the gate.
    """
    raw_env = os.environ.get(GOVERNOR_MODE_ENV)
    if raw_env:
        return GovernorMode.from_str(raw_env)
    if config is not None and config.governor_mode:
        return GovernorMode.from_str(config.governor_mode)
    return GovernorMode.ENFORCING


# ─── Environment helpers ────────────────────────────────────────────────────
def getEnvOrDefault(name: str, default: str) -> str:
    """Return ``os.environ[name]`` with a documented default. Used by app.py."""
    return os.environ.get(name, default)
