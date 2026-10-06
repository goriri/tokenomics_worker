"""Shared configuration, model resolution and economics for tokenomics-worker.

One module is the single source of truth for:
  * where the plugin lives and what its agents are called,
  * reading tokenomics.config.json (which models fill which role),
  * resolving a model id to the enum an agent.json pin needs,
  * judging whether a given orchestrator/subagent pairing actually saves quota.

It is imported by lib/render.py, mcp/tokenomics_tools_server.py and
scripts/verify_and_record.py, and has no dependencies outside the stdlib.
"""

from __future__ import annotations

import datetime
import glob
import io
import json
import os
import re
import sqlite3
from typing import Any

PLUGIN_NAME = "tokenomics-worker"
# realpath, so invoking through the install symlink renders the same paths as
# invoking from the source checkout and --check stays stable.
PLUGIN_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
)
# The user's own choices (gitignored). Falls back to the shipped defaults.
CONFIG_PATH = os.path.join(PLUGIN_ROOT, "tokenomics.config.json")
DEFAULT_CONFIG_PATH = os.path.join(PLUGIN_ROOT, "tokenomics.default.json")
REGISTRY_PATH = os.path.join(PLUGIN_ROOT, "models.json")

# Billing units understood by the economics check.
UNIT_TOKENS = "tokens"  # Token-metered: every token held, read or written costs.
UNIT_CALLS = "calls"  # Call-metered: one unit per turn whatever its size.
UNIT_UNKNOWN = "unknown"
KNOWN_UNITS = (UNIT_TOKENS, UNIT_CALLS, UNIT_UNKNOWN)

APP_DIR_ENV = "TOKENOMICS_APP_DIR"

# Where agent prompts tell the model to find scripts/ and skills/. A constant
# (not this checkout's path) so rendered agents are portable and reproducible;
# override when the plugin is installed somewhere else.
PLUGIN_DIR_ENV = "TOKENOMICS_PLUGIN_DIR"
DEFAULT_PLUGIN_DIR = "~/.gemini/config/plugins/tokenomics_worker"

SUBAGENT_ROLES = ("scout", "worker", "redteam")
ALL_ROLES = ("orchestrator",) + SUBAGENT_ROLES

AGENT_NAMES = {
    "orchestrator": "tw_orchestrator",
    "scout": "tw_scout",
    "worker": "tw_worker",
    "redteam": "tw_redteam",
}

DEFAULT_FALLBACKS: dict[str, str | None] = {
    "orchestrator": None,
    "subagent": None,
    "scout": None,
    "worker": None,
    "redteam": None,
}
DEFAULT_FALLBACK_THRESHOLD = 0.05

_ENUM_RE = re.compile(r"^MODEL_[A-Za-z0-9_]+$")
_CATALOG_SENTINEL = object()

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_RISK = "risk"


class ConfigError(Exception):
  """Raised when the configuration cannot be resolved into pinned agents."""


def plugin_dir_for_prompts() -> str:
  """Returns the plugin path written into rendered agent prompts."""
  return os.environ.get(PLUGIN_DIR_ENV) or DEFAULT_PLUGIN_DIR


def find_app_dir_for_conversation(conversation_id: str) -> str | None:
  """Finds the app directory under ~/.gemini holding a conversation db or brain."""
  override = os.environ.get(APP_DIR_ENV)
  if override:
    return os.path.expanduser(override)
  clean_id = (conversation_id or "").strip()
  if not clean_id:
    return None
  pattern = os.path.expanduser("~/.gemini/*/conversations")
  for conv_dir in glob.glob(pattern):
    base = os.path.dirname(conv_dir)
    brain_dir = os.path.join(base, "brain")
    if os.path.isdir(conv_dir) and os.path.isdir(brain_dir):
      db_path = os.path.join(conv_dir, f"{clean_id}.db")
      brain_path = os.path.join(brain_dir, clean_id)
      if os.path.exists(db_path) or os.path.isdir(brain_path):
        return base
  return None


def app_data_dir() -> str:
  """Returns the IDE's app data directory (holds conversations/ and brain/).

  Order: $TOKENOMICS_APP_DIR, then the directory under ~/.gemini whose
  conversations/ was modified most recently.

  Raises:
    ConfigError: when no candidate is found.
  """
  override = os.environ.get(APP_DIR_ENV)
  if override:
    return os.path.expanduser(override)
  candidates = [
      os.path.dirname(c)
      for c in glob.glob(os.path.expanduser("~/.gemini/*/conversations"))
      if os.path.isdir(c) and os.path.isdir(os.path.join(os.path.dirname(c), "brain"))
  ]
  if not candidates:
    raise ConfigError(
        "cannot find the IDE app data directory (a folder under ~/.gemini"
        f" holding conversations/ and brain/); set {APP_DIR_ENV}"
    )
  return max(
      candidates,
      key=lambda d: os.path.getmtime(os.path.join(d, "conversations")),
  )


def display_path(path: str) -> str:
  """Returns path with the home directory collapsed to '~' when possible."""
  home = os.path.expanduser("~")
  real = os.path.abspath(path)
  if real == home or real.startswith(home + os.sep):
    return "~" + real[len(home):]
  return real


def _read_json(path: str) -> dict[str, Any]:
  try:
    with io.open(path, encoding="utf-8") as f:
      data = json.load(f)
  except OSError as e:
    raise ConfigError(f"cannot read {path}: {e}") from e
  except ValueError as e:
    raise ConfigError(f"{path} is not valid JSON: {e}") from e
  if not isinstance(data, dict):
    raise ConfigError(f"{path} must contain a JSON object")
  return data


def load_config(path: str | None = None) -> dict[str, Any]:
  """Loads and validates the user's config, or the shipped defaults."""
  cfg_path = path or (
      CONFIG_PATH if os.path.exists(CONFIG_PATH) else DEFAULT_CONFIG_PATH
  )
  data = _read_json(cfg_path)
  for key in ("orchestrator", "subagent"):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
      raise ConfigError(f"{cfg_path}: '{key}' must be a non-empty model id")
    data[key] = value.strip()

  overrides = data.get("role_overrides") or {}
  if not isinstance(overrides, dict):
    raise ConfigError(f"{cfg_path}: 'role_overrides' must be an object")
  unknown_roles = sorted(set(overrides) - set(SUBAGENT_ROLES))
  if unknown_roles:
    raise ConfigError(
        f"{cfg_path}: unknown role_overrides {unknown_roles}; valid roles are"
        f" {list(SUBAGENT_ROLES)}"
    )
  clean_overrides: dict[str, str | None] = {}
  for role in SUBAGENT_ROLES:
    value = overrides.get(role)
    if value is not None and (not isinstance(value, str) or not value.strip()):
      raise ConfigError(
          f"{cfg_path}: role_overrides.{role} must be a model id or null"
      )
    clean_overrides[role] = value.strip() if value else None
  data["role_overrides"] = clean_overrides

  fallbacks = data.get("fallbacks")
  if fallbacks is None:
    clean_fallbacks = dict(DEFAULT_FALLBACKS)
  elif not isinstance(fallbacks, dict):
    raise ConfigError(f"{cfg_path}: 'fallbacks' must be an object")
  else:
    valid_fb_keys = set(DEFAULT_FALLBACKS.keys())
    unknown_fb = sorted(set(fallbacks) - valid_fb_keys)
    if unknown_fb:
      raise ConfigError(
          f"{cfg_path}: unknown fallbacks {unknown_fb}; valid keys are"
          f" {sorted(valid_fb_keys)}"
      )
    clean_fallbacks = dict(DEFAULT_FALLBACKS)
    for k in valid_fb_keys:
      v = fallbacks.get(k)
      if v is not None and (not isinstance(v, str) or not v.strip()):
        raise ConfigError(
            f"{cfg_path}: fallbacks.{k} must be a model id or null"
        )
      clean_fallbacks[k] = v.strip() if v else None
  data["fallbacks"] = clean_fallbacks

  threshold = data.get("fallback_threshold")
  if threshold is None:
    data["fallback_threshold"] = DEFAULT_FALLBACK_THRESHOLD
  else:
    try:
      val = float(threshold)
    except (ValueError, TypeError):
      raise ConfigError(
          f"{cfg_path}: 'fallback_threshold' must be a number between 0 and 1,"
          f" got {threshold!r}"
      )
    if not (0.0 <= val <= 1.0):
      raise ConfigError(
          f"{cfg_path}: 'fallback_threshold' must be between 0 and 1, got {val}"
      )
    data["fallback_threshold"] = val

  custom = data.get("custom_models") or {}
  if not isinstance(custom, dict):
    raise ConfigError(f"{cfg_path}: 'custom_models' must be an object")
  for model_id, entry in custom.items():
    if not isinstance(entry, dict):
      raise ConfigError(f"{cfg_path}: custom_models.{model_id} must be an object")
    enum = entry.get("enum")
    if enum is not None and not (isinstance(enum, str) and _ENUM_RE.match(enum)):
      raise ConfigError(
          f"{cfg_path}: custom_models.{model_id}.enum must look like"
          f" MODEL_..., got {enum!r}"
      )
    unit = (entry.get("billing") or {}).get("unit")
    if unit is not None and unit not in KNOWN_UNITS:
      raise ConfigError(
          f"{cfg_path}: custom_models.{model_id}.billing.unit must be one of"
          f" {list(KNOWN_UNITS)}, got {unit!r}"
      )
  data["custom_models"] = custom
  return data


def save_config(data: dict[str, Any], path: str | None = None) -> None:
  """Writes the user's config (never the shipped defaults)."""
  cfg_path = path or CONFIG_PATH
  tmp_path = cfg_path + ".tmp"
  with io.open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
  os.replace(tmp_path, cfg_path)


def load_registry(
    config: dict[str, Any] | None = None,
    registry_path: str | None = None,
    catalog: Any = _CATALOG_SENTINEL,
) -> dict[str, dict[str, Any]]:
  """Builds model_id -> info from models.json, live catalog, and custom_models.

  Precedence: custom_models > live catalog > models.json.
  """
  bundled = _read_json(registry_path or REGISTRY_PATH).get("models", {})
  registry: dict[str, dict[str, Any]] = {}
  for model_id, entry in bundled.items():
    registry[model_id] = {
        "enum": entry.get("enum"),
        "enum_source": "models.json",
        "label": entry.get("label") or model_id,
        "billing": dict(entry.get("billing") or {"unit": "unknown"}),
        "alias": bool(entry.get("alias", False)),
        "notes": entry.get("notes", ""),
    }

  cat_data = None
  if catalog is _CATALOG_SENTINEL:
    try:
      import catalog as cat_lib
      cat_data = cat_lib.read_catalog()
    except Exception:
      cat_data = None
  elif catalog is not None and isinstance(catalog, dict):
    cat_data = catalog

  if cat_data:
    for entry in cat_data.get("models") or []:
      mid = entry.get("model_id")
      if not mid:
        continue
      info = registry.setdefault(
          mid,
          {
              "enum": None,
              "enum_source": None,
              "label": mid,
              "billing": {"unit": "unknown"},
              "alias": False,
              "notes": "",
          },
      )
      if entry.get("label"):
        info["label"] = entry["label"]
      if entry.get("enum"):
        info["enum"] = entry["enum"]
        info["enum_source"] = "live"
      elif info.get("enum_source") is None:
        info["enum_source"] = "live"

      info["remaining_fraction"] = entry.get("remaining_fraction")
      info["reset_time"] = entry.get("reset_time")
      info["quota_group"] = entry.get("quota_group")
      info["recommended"] = bool(entry.get("recommended", False))
      if entry.get("tag"):
        info["tag"] = entry["tag"]

      if not info["billing"].get("bucket") and entry.get("quota_group"):
        info["billing"]["bucket"] = entry["quota_group"]
        info["billing"]["bucket_inferred"] = True

  for model_id, entry in ((config or {}).get("custom_models") or {}).items():
    info = registry.setdefault(
        model_id,
        {
            "enum": None,
            "enum_source": None,
            "label": model_id,
            "billing": {"unit": "unknown"},
            "alias": False,
            "notes": "",
        },
    )
    if entry.get("enum"):
      info["enum"] = entry["enum"]
      info["enum_source"] = "custom_models"
    if entry.get("label"):
      info["label"] = entry["label"]
    like = entry.get("billing_like")
    if entry.get("notes"):
      info["notes"] = entry["notes"]
    if like and not entry.get("billing"):
      if like not in registry:
        raise ConfigError(
            f"custom_models.{model_id}.billing_like refers to unknown model"
            f" {like!r}"
        )
      info["billing"] = dict(registry[like]["billing"])
      info["notes"] = (
          f"{info.get('notes', '')} Billing assumed same as {like}; verify."
      ).strip()
    if entry.get("billing"):
      info["billing"] = dict(entry["billing"])
    if "alias" in entry:
      info["alias"] = bool(entry["alias"])

  return registry


def suggest_models(model_id: str, registry: dict[str, Any]) -> list[str]:
  """Returns registry ids that share a family prefix with model_id."""
  family = model_id.split("-")[0]
  return [m for m in sorted(registry) if family and family in m][:12]


def resolve_model(
    model_id: str, registry: dict[str, Any], require_enum: bool = True
) -> dict[str, Any]:
  """Returns the registry entry for model_id plus its id, or raises."""
  target_id = model_id
  if target_id not in registry:
    try:
      import catalog as cat_lib
      target_id = cat_lib.resolve_query(model_id, registry)
    except ConfigError:
      pass

  info = registry.get(target_id)
  if not require_enum and (info is None or not info.get("enum")):
    resolved = dict(info or {
        "label": target_id,
        "billing": {"unit": "unknown"},
        "alias": False,
        "notes": "",
    })
    resolved.update({"model_id": target_id, "enum": resolved.get("enum")})
    resolved.setdefault("enum_source", None)
    resolved["billing"] = dict(resolved.get("billing") or {"unit": "unknown"})
    resolved["billing"].setdefault("unit", "unknown")
    return resolved
  if info is None or not info.get("enum"):
    close = suggest_models(target_id, registry)
    hint = ", ".join(close) if close else "none similar (see list_models)"
    raise ConfigError(
        f"no enum known for model id {target_id!r}. Known: {hint}. To add it,"
        " select that model in the model picker for a new conversation, send"
        " one message, then call the learn_model tool with that"
        " conversation's id."
    )
  resolved = dict(info)
  resolved["model_id"] = target_id
  resolved["billing"] = dict(info.get("billing") or {"unit": "unknown"})
  resolved["billing"].setdefault("unit", "unknown")
  return resolved


def role_model_ids(config: dict[str, Any]) -> dict[str, str]:
  """Returns role -> model id after applying role_overrides."""
  ids = {"orchestrator": config["orchestrator"]}
  for role in SUBAGENT_ROLES:
    ids[role] = config["role_overrides"].get(role) or config["subagent"]
  return ids


def role_fallback_ids(config: dict[str, Any]) -> dict[str, str | None]:
  """Returns role -> fallback model id after applying subagent fallback default."""
  fb = config.get("fallbacks") or {}
  ids = {"orchestrator": fb.get("orchestrator")}
  sub_fb = fb.get("subagent")
  for role in SUBAGENT_ROLES:
    ids[role] = fb.get(role) or sub_fb
  return ids


def resolve_roles(
    config: dict[str, Any],
    registry: dict[str, Any],
    allow_unpinned: bool = False,
) -> dict[str, dict[str, Any]]:
  """Resolves every role's primary and fallback model."""
  resolved: dict[str, dict[str, Any]] = {}
  errors: list[str] = []
  primaries = role_model_ids(config)
  fallbacks = role_fallback_ids(config)

  for role in ALL_ROLES:
    pri_id = primaries[role]
    fb_id = fallbacks.get(role)
    if fb_id and fb_id == pri_id:
      errors.append(f"{role}: fallback model cannot be the same as primary ({pri_id})")

    try:
      resolved_primary = resolve_model(
          pri_id,
          registry,
          require_enum=(role != "orchestrator" and not allow_unpinned),
      )
    except ConfigError as e:
      errors.append(f"{role}: {e}")
      resolved_primary = {
          "model_id": pri_id,
          "label": pri_id,
          "enum": None,
          "billing": {"unit": "unknown"},
      }

    resolved_fb = None
    if fb_id:
      try:
        resolved_fb = resolve_model(
            fb_id,
            registry,
            require_enum=(role != "orchestrator" and not allow_unpinned),
        )
      except ConfigError as e:
        errors.append(f"{role} (fallback): {e}")
        resolved_fb = {
            "model_id": fb_id,
            "label": fb_id,
            "enum": None,
            "billing": {"unit": "unknown"},
        }

    resolved_primary["fallback"] = resolved_fb
    resolved[role] = resolved_primary

  if errors:
    raise ConfigError("\n".join(errors))
  return resolved


def _fmt_limit(billing: dict[str, Any]) -> str:
  limit = billing.get("daily_limit")
  unit = billing.get("unit")
  if not limit:
    return "unknown daily limit"
  return f"{int(limit):,} {unit}/day"


def _assess_pair(
    role: str, orch: dict[str, Any], sub: dict[str, Any]
) -> tuple[str, str]:
  """Returns (level, message) for one subagent role against the orchestrator."""
  ob, sb = orch["billing"], sub["billing"]
  ou, su = ob.get("unit", "unknown"), sb.get("unit", "unknown")
  obucket, sbucket = ob.get("bucket"), sb.get("bucket")
  sid = sub["model_id"]

  if sid == orch["model_id"]:
    return LEVEL_RISK, (
        f"{role} runs on the orchestrator's own model ({sid}). That is"
        " Model:'inherit' with extra steps: everything it reads and writes"
        " bills to the orchestrator's budget. Delegation only buys context"
        " isolation."
    )
  if ou == UNIT_UNKNOWN or su == UNIT_UNKNOWN:
    which = sid if su == UNIT_UNKNOWN else orch["model_id"]
    return LEVEL_WARN, (
        f"Billing for {which} is unknown. Treat it as token-metered until you"
        " know its quota, then record it with learn_model (billing argument)."
    )
  if obucket and obucket == sbucket:
    return LEVEL_WARN, (
        f"{role} ({sid}) shares quota bucket {sbucket} with the orchestrator."
        " Delegation spreads no load across budgets; the only gain is keeping"
        " long reads out of the orchestrator's context."
    )
  if ou == UNIT_TOKENS and su == UNIT_CALLS:
    return LEVEL_OK, (
        f"Offload: {role} moves tokens from the token-metered {obucket} bucket"
        f" to the per-call {sbucket} bucket ({_fmt_limit(sb)}). A {role} that"
        " reads 500k tokens costs one call."
    )
  if ou == UNIT_TOKENS and su == UNIT_TOKENS:
    return LEVEL_WARN, (
        f"Both token-metered on different buckets ({obucket} -> {sbucket},"
        f" {_fmt_limit(sb)}). Delegation moves tokens to a second budget, but"
        " they are still metered: keep {role} reads bounded and never treat"
        " its tokens as free."
    )
  if ou == UNIT_CALLS and su == UNIT_CALLS:
    return LEVEL_OK, (
        f"Both per-call on different buckets. Each {role} turn costs one"
        f" {sbucket} call instead of one {obucket} call; hand multi-turn loops"
        f" (builds, crawls, review loops) to the {role} to spare {obucket}."
    )
  if ou == UNIT_CALLS and su == UNIT_TOKENS:
    return LEVEL_RISK, (
        f"Inverted economics: the orchestrator is per-call but {role} ({sid})"
        " is token-metered. Long reads and build logs become the expensive"
        f" part. Pick a per-call model for {role}, or keep token-heavy work on"
        " the orchestrator."
    )
  return LEVEL_WARN, f"Unrecognised billing units {ou!r} / {su!r} for {role}."


def orchestrator_doctrine(orch: dict[str, Any]) -> dict[str, Any]:
  """Returns the cost rules the orchestrator should follow for its billing."""
  billing = orch["billing"]
  unit = billing.get("unit", "unknown")
  if unit == UNIT_CALLS:
    return {
        "unit": UNIT_CALLS,
        "summary": (
            f"Every orchestrator turn costs one call on {billing.get('bucket')}"
            f" ({_fmt_limit(billing)}) whatever its size. Turn count IS the"
            " bill: batch decisions into fewer, larger turns, never bridge a"
            " wait with a keepalive turn, and ignore prompt-cache warmth (it"
            " does not touch quota)."
        ),
        "cache_discipline": False,
    }
  weights = billing.get("token_weights") or {}
  ttl = billing.get("cache_ttl_seconds")
  if unit == UNIT_TOKENS:
    parts = [
        f"Tokens are the bill on {billing.get('bucket')}"
        f" ({_fmt_limit(billing)}); turn count is not."
    ]
    if weights:
      parts.append(
          "Weights per token: cached read"
          f" {weights.get('cached_input_read')}, uncached"
          f" {weights.get('uncached_input')}, cache write"
          f" {weights.get('cache_write')}, output+thinking"
          f" {weights.get('output_and_thinking')}."
      )
    else:
      parts.append(
          "Token weights for this bucket are not recorded; assume output is"
          " the most expensive dimension."
      )
    if ttl:
      parts.append(
          f"Prompt cache TTL is {ttl}s (safe margin {max(0, ttl - 60)}s): a"
          " cold resume re-bills all history at cache-write rates."
      )
    parts.append("Keep context small, write little, delegate prose.")
    return {"unit": UNIT_TOKENS, "summary": " ".join(parts), "cache_discipline": True}
  return {
      "unit": UNIT_UNKNOWN,
      "summary": (
          "Orchestrator billing is unknown. Assume it is token-metered and"
          " follow the token rules (small context, short output, warm cache)."
      ),
      "cache_discipline": True,
  }


def assess_economics(resolved: dict[str, dict[str, Any]]) -> dict[str, Any]:
  """Judges whether the configured pairings actually save quota."""
  orch = resolved["orchestrator"]
  orch_fb = orch.get("fallback")
  primary_findings: list[dict[str, str]] = []
  fallback_findings: list[dict[str, str]] = []

  for role in ALL_ROLES:
    info = resolved[role]
    if info.get("alias"):
      primary_findings.append({
          "level": LEVEL_WARN,
          "role": role,
          "message": (
              f"{info['model_id']} is a server-side alias; pinning it pins a"
              " moving target."
          ),
      })
    fb = info.get("fallback")
    if fb and fb.get("alias"):
      fallback_findings.append({
          "level": LEVEL_WARN,
          "role": f"{role} (fallback)",
          "message": (
              f"{fb['model_id']} is a server-side alias; pinning it pins a"
              " moving target."
          ),
      })

  for role in SUBAGENT_ROLES:
    info = resolved[role]
    if not info.get("enum"):
      primary_findings.append({
          "level": LEVEL_RISK,
          "role": role,
          "message": (
              f"{role} is not pinned: no enum is known for"
              f" {info['model_id']}, so it runs on whatever model the IDE picks"
              " (usually the orchestrator's own). Teach the model with"
              " learn_model, then call set_models."
          ),
      })
    else:
      level, message = _assess_pair(role, orch, info)
      primary_findings.append({"level": level, "role": role, "message": message})

    fb = info.get("fallback")
    if fb:
      if not fb.get("enum"):
        fallback_findings.append({
            "level": LEVEL_RISK,
            "role": f"{role} (fallback)",
            "message": (
                f"{role} fallback is not pinned: no enum is known for"
                f" {fb['model_id']}. Teach the model with learn_model."
            ),
        })
      else:
        level, message = _assess_pair(f"{role} (fallback)", orch, fb)
        fallback_findings.append({
            "level": level,
            "role": f"{role} (fallback)",
            "message": message,
        })
        if orch_fb:
          level_fb, msg_fb = _assess_pair(f"{role} (fallback)", orch_fb, fb)
          fallback_findings.append({
              "level": level_fb,
              "role": f"{role} (fallback vs orch fallback)",
              "message": msg_fb,
          })

  primary_levels = {f["level"] for f in primary_findings}
  fallback_levels = {f["level"] for f in fallback_findings}

  if LEVEL_RISK in primary_levels:
    mode = "counterproductive"
  elif (
      LEVEL_WARN in primary_levels
      or LEVEL_RISK in fallback_levels
      or LEVEL_WARN in fallback_levels
  ):
    mode = "partial"
  else:
    mode = "offload"

  all_findings = primary_findings + fallback_findings
  return {
      "mode": mode,
      "findings": all_findings,
      "orchestrator": orchestrator_doctrine(orch),
  }


def build_active(
    config: dict[str, Any],
    registry: dict[str, Any],
    allow_unpinned: bool = False,
) -> dict[str, Any]:
  """Resolves every role of an in-memory config and assesses economics."""
  resolved = resolve_roles(config, registry, allow_unpinned=allow_unpinned)
  return {
      "config": config,
      "roles": resolved,
      "agents": dict(AGENT_NAMES),
      "economics": assess_economics(resolved),
  }


def load_active(
    config_path: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
  """Loads config defaults, resolves roles and assesses economics."""
  config = load_config(config_path)
  registry = load_registry(config, registry_path=registry_path)
  return build_active(config, registry, allow_unpinned=True)


def route_roles(
    active: dict[str, Any],
    catalog: dict[str, Any] | None = None,
    names: dict[str, str] | None = None,
    threshold: float | None = None,
    fallback_names: dict[str, str] | None = None,
) -> dict[str, Any]:
  """Routes each role to primary or fallback based on catalog quotas."""
  effective_names = dict(AGENT_NAMES)
  if names:
    effective_names.update(names)

  effective_threshold = (
      threshold
      if threshold is not None
      else float(active.get("config", {}).get("fallback_threshold", DEFAULT_FALLBACK_THRESHOLD))
  )

  if catalog is None:
    routes: dict[str, Any] = {}
    for role in ALL_ROLES:
      pri_info = active["roles"][role]
      use_name = (
          None if role == "orchestrator" else effective_names.get(role)
      )
      routes[role] = {
          "use_type_name": use_name,
          "model_id": pri_info["model_id"],
          "using_fallback": False,
          "reason": "no live quota data; refresh with scripts/model_catalog.py",
          "primary_quota": None,
          "fallback_quota": None,
      }
    return routes

  import catalog as cat_lib
  is_stale = cat_lib.is_catalog_stale(catalog)
  cat_models = {
      m["model_id"]: m
      for m in catalog.get("models", [])
      if isinstance(m, dict) and "model_id" in m
  }

  routes = {}
  for role in ALL_ROLES:
    pri_info = active["roles"][role]
    pri_id = pri_info["model_id"]
    pri_cat = cat_models.get(pri_id)
    pri_quota = (
        {
            "remaining_fraction": pri_cat.get("remaining_fraction"),
            "reset_time": pri_cat.get("reset_time"),
        }
        if pri_cat
        else None
    )

    fb_info = pri_info.get("fallback")
    fb_id = fb_info["model_id"] if fb_info else None
    fb_cat = cat_models.get(fb_id) if fb_id else None
    fb_quota = (
        {
            "remaining_fraction": fb_cat.get("remaining_fraction"),
            "reset_time": fb_cat.get("reset_time"),
        }
        if fb_cat
        else None
    )

    pri_name = None if role == "orchestrator" else effective_names.get(role)
    fb_name = None
    if role != "orchestrator":
      fb_name = (
          (fallback_names or {}).get(role)
          or f"{effective_names.get(role)}_fallback"
      )

    pri_rf = pri_cat.get("remaining_fraction") if pri_cat else None
    absent = pri_cat is None
    exhausted = pri_rf is not None and pri_rf <= effective_threshold

    using_fb = False
    chosen_id = pri_id
    use_name = pri_name
    action = None

    if absent or exhausted:
      if fb_info is not None:
        fb_rf = fb_cat.get("remaining_fraction") if fb_cat else None
        if fb_rf is None or (pri_rf is None or fb_rf > pri_rf):
          using_fb = True
          chosen_id = fb_id
          use_name = fb_name
          if absent:
            reason = (
                f"primary model {pri_id} is absent from live catalog; routed to"
                f" fallback {fb_id}"
            )
          else:
            reason = (
                f"primary quota ({pri_rf*100:.1f}%) <= threshold"
                f" ({effective_threshold*100:.1f}%); routed to fallback {fb_id}"
            )
          if role == "orchestrator":
            action = f"Ask the user to run /model and pick '{fb_info['label']}'"
        else:
          reason = (
              f"fallback quota ({fb_rf*100:.1f}%) is not better than primary"
              f" ({pri_rf*100:.1f}%); staying on primary"
          )
      else:
        if absent:
          reason = (
              f"primary model {pri_id} is absent from catalog and no fallback is"
              " configured"
          )
        else:
          reason = (
              f"primary quota ({pri_rf*100:.1f}%) <= threshold"
              f" ({effective_threshold*100:.1f}%) but no fallback is configured"
          )
    else:
      reason = "primary quota is healthy"

    role_dict: dict[str, Any] = {
        "use_type_name": use_name,
        "model_id": chosen_id,
        "using_fallback": using_fb,
        "reason": reason,
        "primary_quota": pri_quota,
        "fallback_quota": fb_quota,
    }
    if action is not None:
      role_dict["action"] = action
    if is_stale:
      role_dict["stale_catalog"] = True
    routes[role] = role_dict

  return routes


# --------------------------------------------- learning enums from the IDE

_MODEL_ENUM_FIELD_RE = re.compile(rb"\n\nmodel_enum\x12(.)", re.S)
_CONV_ID_RE = re.compile(r"[A-Za-z0-9_\-]+")


def read_conversation_model_enum(
    conversation_id: str, conversations_dir: str | None = None
) -> str | None:
  """Returns the model enum that served a conversation's latest turn."""
  if not conversation_id or not _CONV_ID_RE.fullmatch(conversation_id):
    raise ConfigError(f"invalid conversation id {conversation_id!r}")
  if conversations_dir is None:
    app_dir = find_app_dir_for_conversation(conversation_id) or app_data_dir()
    conversations_dir = os.path.join(app_dir, "conversations")
  db_path = os.path.join(conversations_dir, f"{conversation_id}.db")
  if not os.path.exists(db_path):
    raise ConfigError(f"no conversation database at {db_path}")
  try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
      rows = conn.execute(
          "SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 5"
      ).fetchall()
    finally:
      conn.close()
  except sqlite3.Error as e:
    raise ConfigError(f"cannot read {db_path}: {e}") from e
  for (data,) in rows:
    blob = data if isinstance(data, bytes) else str(data or "").encode()
    match = _MODEL_ENUM_FIELD_RE.search(blob)
    if match:
      length = match.group(1)[0]
      value = blob[match.end():match.end() + length].decode("ascii", "replace")
      if _ENUM_RE.match(value):
        return value
  return None


def learn_model(
    model_id: str,
    conversation_id: str,
    label: str | None = None,
    billing_like: str | None = None,
    config_path: str | None = None,
    conversations_dir: str | None = None,
    billing: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Records model_id -> enum from a conversation that ran on that model."""
  model_id = (model_id or "").strip()
  if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", model_id):
    raise ConfigError(f"invalid model id {model_id!r}")
  enum = read_conversation_model_enum(conversation_id, conversations_dir)
  if not enum:
    raise ConfigError(
        f"conversation {conversation_id} has no recorded model_enum yet; send"
        " it one message first"
    )
  if billing is not None:
    if not isinstance(billing, dict) or billing.get("unit") not in KNOWN_UNITS:
      raise ConfigError(
          f"billing.unit must be one of {list(KNOWN_UNITS)}, got {billing!r}"
      )
  config = load_config(config_path)
  registry = load_registry(config)
  clashes = sorted(
      m for m, info in registry.items()
      if info.get("enum") == enum and m != model_id
  )
  entry = dict(config["custom_models"].get(model_id) or {})
  entry["enum"] = enum
  if label:
    entry["label"] = label
  if billing_like:
    if billing_like not in registry:
      raise ConfigError(f"billing_like refers to unknown model {billing_like!r}")
    entry["billing_like"] = billing_like
  if billing is not None:
    entry["billing"] = dict(billing)
  entry["notes"] = f"Enum learned from conversation {conversation_id}."
  config["custom_models"][model_id] = entry
  save_config(config, config_path)
  return {
      "model_id": model_id,
      "enum": enum,
      "learned_from": conversation_id,
      "also_known_as": clashes,
      "entry": entry,
  }


def set_model_info(
    model_id: str,
    label: str | None = None,
    billing_like: str | None = None,
    billing: dict[str, Any] | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
  """Records label and billing in custom_models without requiring an enum."""
  model_id = (model_id or "").strip()
  if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", model_id):
    raise ConfigError(f"invalid model id {model_id!r}")
  if billing is not None:
    if not isinstance(billing, dict) or billing.get("unit") not in KNOWN_UNITS:
      raise ConfigError(
          f"billing.unit must be one of {list(KNOWN_UNITS)}, got {billing!r}"
      )
  config = load_config(config_path)
  registry = load_registry(config)
  entry = dict(config["custom_models"].get(model_id) or {})
  if label:
    entry["label"] = label
  if billing_like:
    if billing_like not in registry:
      raise ConfigError(f"billing_like refers to unknown model {billing_like!r}")
    entry["billing_like"] = billing_like
  if billing is not None:
    entry["billing"] = dict(billing)
  config["custom_models"][model_id] = entry
  save_config(config, config_path)
  return {
      "model_id": model_id,
      "entry": entry,
  }
