"""AgentCore Platform v1.0"""

# Renewal-risk policy and cross-sell catalog — the single source of truth for
# the values that decide how an account's renewal risk is graded and what the
# briefing quotes as the indicative annual premium of a missing cover.
#
# GOVERNANCE NOTE:
#   These thresholds are an AGENCY-CONFIGURED commercial policy input, not a
#   statutory requirement. No regulation fixes them; they belong to the agency
#   and its carriers. The generated briefing is sales-preparation
#   decision-support, not a regulatory record and not an offer of cover.
#
#   The policy lives in config/config.yaml — the runtime-parameter file the
#   platform registry loads and passes to the graph constructor — under
#   `renewal_risk_policy` and `cross_sell_catalog`, each carrying a
#   policy_version and an effective_date, so the rule is governed as versioned
#   configuration instead of being duplicated as literals across nodes.
#
#   This module is the only loader. It reads that file best-effort and falls
#   back to the documented defaults below, recording provenance (`source`) and
#   an effectiveness flag (`policy_effective`) so a configuration that never
#   arrived is visible on the briefing rather than silently absent.
#
#   Every value read from configuration goes through the same finite + bounded
#   parser the caller-supplied numbers use. An operator typo is not hostile,
#   but a non-finite threshold fails open in exactly the same way: NaN compares
#   False against every account and the agency is told nothing is at risk.

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from src.schemas.state import finite_in_range, finite_int_in_range, inert_identifier

logger = logging.getLogger(__name__)

# Documented fallback policy — applied per key, only where configuration is
# absent or does not survive its bounds check.
_DEFAULT_POLICY: Dict[str, Any] = {
    "policy_name": "Renewal Risk & Cross-Sell Policy",
    "policy_version": "0.0.0-default",
    "effective_date": "1970-01-01",
    "lapse_days_threshold": 30,
    "premium_increase_pct_threshold": 10.0,
    "claims_count_threshold": 2,
}

# Indicative annual premiums used to size the cross-sell opportunity, keyed by
# cover category. Figures are indicative planning values for briefing purposes,
# not quotes.
_DEFAULT_CATALOG: Dict[str, int] = {
    "life": 84_000,
    "medical": 48_000,
    "accident": 24_000,
    "fire": 36_000,
    "auto": 72_000,
}

# Accepted ranges for each configured value. A declared value outside its range
# — or a non-finite one — is not applied: the documented default for that key
# is kept and the policy is reported as not effective.
_INT_BOUNDS: Dict[str, tuple[int, int]] = {
    "lapse_days_threshold": (0, 3_650),
    "claims_count_threshold": (0, 1_000),
}
_FLOAT_BOUNDS: Dict[str, tuple[float, float]] = {
    "premium_increase_pct_threshold": (0.0, 1_000.0),
}

# Bounds for the indicative premium of a single cover category.
_CATALOG_PREMIUM_MIN = 0
_CATALOG_PREMIUM_MAX = 100_000_000

# Runtime tuning values with their accepted ranges. `timeout_s` is the key
# name in config/config.yaml; `timeout_seconds` is the name the graph layer
# validates, so the mapping is applied here rather than being assumed.
_TIMEOUT_BOUNDS = (1, 3_600)
_MAX_RETRY_BOUNDS = (0, 10)
_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_MAX_RETRY = 3


def _config_path() -> str:
    """Absolute path to config/config.yaml, anchored on this module.

    src/services/renewal_policy.py -> parents[2] is the repository root.
    config/agent.yaml holds registration identity only; every runtime
    parameter lives in config/config.yaml.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "config", "config.yaml"))


def load_runtime_config() -> Dict[str, Any]:
    """Return the runtime parameters declared in config/config.yaml.

    This is the same file the platform registry loads and passes as
    Graph(config=...). Best-effort: an empty dict on any failure (missing file,
    parse error, non-mapping document). Never raises — loading configuration
    must not turn into a graph-construction failure.
    """
    try:
        import yaml

        with open(_config_path(), encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except Exception:  # noqa: BLE001 - configuration is advisory, never fatal
        logger.warning("renewal_policy: runtime configuration is unreadable; template defaults apply")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _resolve_catalog(raw: Any) -> tuple[Dict[str, int], bool]:
    """Validate a declared cross-sell catalog. Returns (catalog, fully_applied)."""
    if not isinstance(raw, dict) or not raw:
        return dict(_DEFAULT_CATALOG), False

    catalog: Dict[str, int] = {}
    complete = True
    for key, value in raw.items():
        category = inert_identifier(key)
        premium = finite_int_in_range(value, _CATALOG_PREMIUM_MIN, _CATALOG_PREMIUM_MAX)
        if category is None or premium is None:
            # Name the key, never the value: an unusable figure is reported by
            # position so a bad configuration is diagnosable without copying it
            # into the log.
            logger.warning("renewal_policy: cross_sell_catalog entry rejected (key=%r)", str(key)[:32])
            complete = False
            continue
        catalog[category] = premium
    if not catalog:
        return dict(_DEFAULT_CATALOG), False
    return catalog, complete


def resolve_policy(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve the effective renewal policy, catalog and runtime tuning values.

    *config* is the runtime mapping the graph was constructed with. None means
    "no mapping was handed to me" and the file is read directly, which keeps
    the loader usable from a node that was constructed without configuration.
    An EMPTY mapping is not the same thing: it means the configuration was
    forwarded and carried nothing, so the defaults apply and the policy is
    reported as not effective rather than quietly re-reading the file.

    The returned mapping always carries every key. `policy_effective` is True
    only when the declared configuration was found AND every value in it
    survived its bounds check, so a partially applied policy is never reported
    as the agency's policy.
    """
    source_config = config if isinstance(config, dict) else load_runtime_config()
    declared = source_config.get("renewal_risk_policy")
    declared = declared if isinstance(declared, dict) else {}

    resolved: Dict[str, Any] = dict(_DEFAULT_POLICY)
    complete = bool(declared)

    for key, (lo_i, hi_i) in _INT_BOUNDS.items():
        parsed_int = finite_int_in_range(declared.get(key), lo_i, hi_i)
        if parsed_int is None:
            if key in declared:
                logger.warning("renewal_policy: declared %s is not a finite in-range value; default kept", key)
            complete = False
            continue
        resolved[key] = parsed_int

    for key, (lo_f, hi_f) in _FLOAT_BOUNDS.items():
        parsed_float = finite_in_range(declared.get(key), lo_f, hi_f)
        if parsed_float is None:
            if key in declared:
                logger.warning("renewal_policy: declared %s is not a finite in-range value; default kept", key)
            complete = False
            continue
        resolved[key] = parsed_float

    for text_key in ("policy_name", "policy_version", "effective_date"):
        value = declared.get(text_key)
        if isinstance(value, str) and value.strip():
            resolved[text_key] = value.strip()[:64]

    catalog, catalog_complete = _resolve_catalog(source_config.get("cross_sell_catalog"))
    resolved["cross_sell_catalog"] = catalog
    complete = complete and catalog_complete

    timeout_seconds = finite_int_in_range(source_config.get("timeout_s"), *_TIMEOUT_BOUNDS)
    max_retry = finite_int_in_range(source_config.get("max_retry"), *_MAX_RETRY_BOUNDS)
    resolved["timeout_seconds"] = _DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    resolved["max_retry"] = _DEFAULT_MAX_RETRY if max_retry is None else max_retry

    resolved["policy_effective"] = complete
    resolved["source"] = "configuration" if complete else "template default"
    return resolved
