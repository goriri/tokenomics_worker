#!/usr/bin/env python3
"""Stdio Model Context Protocol (MCP) server for tokenomics-worker.

Every tool reads tokenomics.config.json at call time, so a model change made
with set_models (or by hand) is reflected without restarting the server.

Tools:
- get_model_config: Active models (plugin defaults plus this conversation's
  session pins), the model actually serving the conversation, and the verdict.
- list_models: Model ids the plugin can pin, with billing.
- set_models: Chooses orchestrator/subagent models from chat. Installs
  session pins that work immediately, or persists new plugin defaults.
- learn_model: Records a model's enum by reading it from a conversation that
  runs on that model (pick it in the IDE's model picker first).
- get_quota_status: Quota buckets, limits and cost rules for the configured
  models.
- get_cache_status: Prompt cache state, TTL expiry and cost for a conversation.
- detect_test_tampering: Audits test diffs for deleted assertions or
  suppressions.
- record_orchestration_event: Appends a usage event to the local ledger.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
from typing import Any, Callable, TextIO

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib")
)
import render
import tokenomics_config as tc

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [tokenomics_mcp] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)

SERVER_NAME = "tokenomics-worker-tools"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

TELEMETRY_FILE: str | None = None
TELEMETRY_FILE_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 300
_LEDGER_LOCK = threading.Lock()


def get_telemetry_file() -> str:
  if TELEMETRY_FILE:
    return TELEMETRY_FILE
  try:
    return os.path.join(tc.app_data_dir(), "telemetry", "tokenomics_events.jsonl")
  except tc.ConfigError:
    return os.path.expanduser("~/.gemini/antigravity/telemetry/tokenomics_events.jsonl")

SUPPRESSION_PATTERNS = (
    r"@Suppress\b",
    r"//\s*NOLINT",
    r"#\s*pylint:\s*disable",
    r"//\s*eslint-disable",
    r"//\s*@ts-ignore",
    r"//\s*@ts-nocheck",
    r"//\s*go:build\s*ignore",
    r"@unittest\.skip\b",
    r"@pytest\.mark\.skip\b",
    r"#\s*type:\s*ignore\b",
    r"//\s*@ts-expect-error\b",
)

ASSERTION_PATTERNS = (
    r"\bself\.assert(?:In|NotIn|IsNone|IsNotNone|Raises|AlmostEqual)\b",
    r"\bassert\.NoError\b",
    r"\brequire\.(?:Equal|NoError|True|False)\b",
    r"\bpytest\.raises\b",
    r"\bassert\b",
    r"\bassertTrue\b",
    r"\bassertFalse\b",
    r"\bassertEqual\b",
    r"\bassertEquals\b",
    r"\bassertNotEqual\b",
    r"\bassert_called\b",
    r"\bexpect\(",
    r"\bt\.(?:Error|Errorf|Fatal|Fatalf)\b",
)

TEST_FILE_RE = re.compile(
    r"(_test\.(py|go|ts|js|cc|cpp)|Test\.(java|kt)|test_.*\.py)$"
)


def sanitize_ident(value: Any, max_len: int = 64) -> str:
  """Keeps only [A-Za-z0-9_.-]."""
  if value is None:
    return ""
  return re.sub(r"[^a-zA-Z0-9_\-.]", "", str(value).strip())[:max_len]


def sanitize_float(value: Any, default: float = 0.0) -> float:
  """Converts to a finite non-negative float, else default."""
  try:
    val = float(value)
  except (ValueError, TypeError):
    return default
  if math.isnan(val) or math.isinf(val) or val < 0.0:
    return default
  return val


def sanitize_int(value: Any, minimum: int = 0) -> int:
  try:
    return max(minimum, int(value))
  except (ValueError, TypeError):
    return minimum


def get_current_time() -> datetime.datetime:
  return datetime.datetime.now(datetime.timezone.utc)


def _active() -> dict[str, Any]:
  return tc.load_active()


# ---------------------------------------------------------------- model config


def _enum_to_ids(registry: dict[str, Any], enum: str | None) -> list[str]:
  return sorted(m for m, info in registry.items() if enum and info.get("enum") == enum)


def _role_view(active: dict[str, Any], role: str) -> dict[str, Any]:
  info = active["roles"][role]
  return {
      "type_name": active["agents"][role],
      "model_id": info["model_id"],
      "label": info["label"],
      "pinned": bool(info.get("enum")),
      "enum": info["enum"],
      "enum_source": info.get("enum_source"),
      "billing": info["billing"],
      "alias": info.get("alias", False),
      "notes": info.get("notes", ""),
  }


def get_model_config(conversation_id: str | None = None) -> dict[str, Any]:
  """Returns plugin defaults, session pins and the detected live model."""
  active = _active()
  result: dict[str, Any] = {
      "plugin_defaults": {
          "orchestrator": _role_view(active, "orchestrator"),
          "subagents": {r: _role_view(active, r) for r in tc.SUBAGENT_ROLES},
          "mode": active["economics"]["mode"],
          "pairing_verdict": active["economics"]["mode"],
          "findings": active["economics"]["findings"],
          "orchestrator_cost_rules": active["economics"]["orchestrator"]["summary"],
          "orchestrator_cost_rules_summary": active["economics"]["orchestrator"]["summary"],
      },
      "how_to_change": (
          "Call set_models (session pins, immediate) or set_models with"
          " persist=true (new plugin defaults, after restart). The"
          " orchestrator's model is whatever is selected in the IDE's model"
          " picker."
      ),
  }
  if conversation_id:
    registry = tc.load_registry(active["config"])
    try:
      enum = tc.read_conversation_model_enum(conversation_id)
    except tc.ConfigError as e:
      enum, result["live_model_error"] = None, str(e)
    result["live_model"] = {
        "enum": enum,
        "model_ids": _enum_to_ids(registry, enum),
        "matches_orchestrator_default": enum is not None
        and enum == active["roles"]["orchestrator"].get("enum"),
    }
    if enum and not result["live_model"]["model_ids"]:
      result["live_model"]["hint"] = (
          "Unknown enum. Call learn_model with the model id you selected in"
          " the picker and this conversation id to record it."
      )
    session = render.load_session(conversation_id)
    if session:
      result["session_pins"] = session
  return result


# ---------------------------------------------------------------- quota status


def get_quota_status(role: str | None = None) -> dict[str, Any]:
  """Returns quota buckets and cost rules for the configured models."""
  active = _active()
  which = (role or "all").strip().lower()
  if which not in ("all", "orchestrator", "subagents"):
    raise ValueError("role must be 'orchestrator', 'subagents' or 'all'")

  result: dict[str, Any] = {
      "note": "Quota limits and cost rules are estimates based on configured model definitions."
  }
  if which in ("all", "orchestrator"):
    view = _role_view(active, "orchestrator")
    view["cost_rules"] = active["economics"]["orchestrator"]["summary"]
    result["orchestrator"] = view
  if which in ("all", "subagents"):
    # Collapse roles that share a model so the answer stays short.
    by_model: dict[str, dict[str, Any]] = {}
    for r in tc.SUBAGENT_ROLES:
      view = _role_view(active, r)
      entry = by_model.setdefault(
          view["model_id"],
          {k: v for k, v in view.items() if k != "type_name"},
      )
      entry.setdefault("type_names", []).append(view["type_name"])
    result["subagents"] = list(by_model.values())
  if which == "all":
    result["pairing_verdict"] = active["economics"]["mode"]
    result["findings"] = active["economics"]["findings"]
  return result


# ------------------------------------------------------- choosing models


def list_models(name_filter: str | None = None) -> dict[str, Any]:
  """Returns pinnable model ids with enum and billing."""
  config = tc.load_config()
  registry = tc.load_registry(config)
  models = []
  for model_id in sorted(registry):
    info = registry[model_id]
    if name_filter and name_filter.lower() not in model_id.lower():
      continue
    models.append({
        "model_id": model_id,
        "label": info.get("label"),
        "enum": info.get("enum"),
        "pinnable": bool(info.get("enum")),
        "enum_source": info.get("enum_source"),
        "billing": render.billing_text(info.get("billing") or {}),
        "notes": info.get("notes", ""),
    })
  return {
      "models": models,
      "missing_a_model": (
          "Select it in the IDE's model picker for any conversation, send one"
          " message, then call learn_model with its id and that"
          " conversation's id."
      ),
  }


_CLEAR = ("", "none", "null", "default")


def _apply_choices(config: dict[str, Any], args: dict[str, Any]) -> list[str]:
  """Applies orchestrator/subagent/role choices to config; returns changes."""
  changes = []
  for key in ("orchestrator", "subagent"):
    value = args.get(key)
    if isinstance(value, str) and value.strip() and value.strip() != config[key]:
      changes.append(f"{key}: {config[key]} -> {value.strip()}")
      config[key] = value.strip()
  for role in tc.SUBAGENT_ROLES:
    value = args.get(role)
    if value is None:
      continue
    new = None if str(value).strip().lower() in _CLEAR else str(value).strip()
    if config["role_overrides"].get(role) != new:
      changes.append(f"{role}: {config['role_overrides'].get(role)} -> {new}")
      config["role_overrides"][role] = new
  return changes


def _summary(active: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
  roles = active["roles"]
  return {
      "orchestrator": {
          "model_id": roles["orchestrator"]["model_id"],
          "billing": render.billing_text(roles["orchestrator"]["billing"]),
          "note": "Runs on the model selected in the IDE's model picker.",
      },
      "subagents": {
          r: {
              "type_name": names[r],
              "model_id": roles[r]["model_id"],
              "enum": roles[r]["enum"],
              "billing": render.billing_text(roles[r]["billing"]),
          }
          for r in tc.SUBAGENT_ROLES
      },
      "pairing_verdict": active["economics"]["mode"],
      "findings": active["economics"]["findings"],
      "orchestrator_cost_rules": active["economics"]["orchestrator"]["summary"],
  }


def set_models(args: dict[str, Any]) -> dict[str, Any]:
  """Chooses models from chat: session pins now, or persisted defaults."""
  conversation_id = (args.get("conversation_id") or "").strip()
  persist = bool(args.get("persist", False))
  if not persist and not conversation_id:
    raise ValueError(
        "conversation_id is required for session pins (or pass persist=true)"
    )
  config = tc.load_config()
  registry = tc.load_registry(config)
  changes = _apply_choices(config, args)
  active = tc.build_active(config, registry)
  mode = active["economics"]["mode"]

  if args.get("strict") and mode == "counterproductive":
    return {
        "status": "REFUSED",
        "reason": "pairing verdict is counterproductive and strict=true",
        **_summary(active, tc.AGENT_NAMES),
    }
  if args.get("dry_run"):
    return {"status": "DRY_RUN", "changes": changes, **_summary(active, tc.AGENT_NAMES)}

  result: dict[str, Any] = {"changes": changes}
  if persist:
    writes = render.planned_writes(active)
    tc.save_config(config)
    for path, content in writes.items():
      render.write_file(path, content)
    result["status"] = "PERSISTED"
    result["written"] = [tc.display_path(p) for p in writes]
    result["note"] = (
        "Plugin defaults updated; tw_scout/tw_worker/tw_redteam pick them up"
        " after an IDE restart."
    )
  if conversation_id:
    manifest = render.install_session(active, conversation_id)
    names = dict(tc.AGENT_NAMES)
    names.update({r: e["type_name"] for r, e in manifest["subagents"].items()})
    result["status"] = "PERSISTED_AND_SESSION" if persist else "SESSION"
    result["use_these_type_names"] = {r: names[r] for r in tc.SUBAGENT_ROLES}
    result.update(_summary(active, names))
    result["session_note"] = (
        "Session pins are written to this conversation's .agents directory."
        " Invoke them by the TypeNames above and omit the Model argument."
        " Installing agents changes the system prompt, so expect one cold"
        " prompt-cache turn."
    )
  else:
    result.update(_summary(active, tc.AGENT_NAMES))
  return result


def learn_model_tool(args: dict[str, Any]) -> dict[str, Any]:
  """Records model_id -> enum from a conversation running that model."""
  learned = tc.learn_model(
      model_id=args.get("model_id", ""),
      conversation_id=(args.get("conversation_id") or "").strip(),
      label=args.get("label"),
      billing_like=args.get("billing_like"),
      billing=args.get("billing"),
  )
  learned["next"] = (
      f"Now pinnable: call set_models with subagent={learned['model_id']} (or"
      " a role) to use it."
  )
  if learned["also_known_as"]:
    learned["warning"] = (
        f"Enum {learned['enum']} is also registered as"
        f" {learned['also_known_as']}; the picker may have been on that model."
    )
  return learned


# ---------------------------------------------------------------- cache status


def get_last_turn_timestamp(conversation_id: str) -> datetime.datetime | None:
  """Returns the newest turn timestamp from the transcript or DB mtime."""
  clean_id = sanitize_ident(conversation_id)
  if not clean_id:
    return None
  try:
    app_dir = tc.app_data_dir()
  except tc.ConfigError:
    return None
  transcript = os.path.join(
      app_dir, "brain", clean_id, ".system_generated", "logs", "transcript.jsonl"
  )
  try:
    with open(transcript, "r", encoding="utf-8") as f:
      lines = f.readlines()
    for line in reversed(lines):
      line = line.strip()
      if not line:
        continue
      try:
        created = json.loads(line).get("created_at")
      except (ValueError, AttributeError):
        continue
      if created:
        try:
          return datetime.datetime.fromisoformat(
              str(created).replace("Z", "+00:00")
          )
        except ValueError:
          continue
  except OSError:
    pass
  db_path = os.path.join(app_dir, "conversations", f"{clean_id}.db")
  try:
    return datetime.datetime.fromtimestamp(
        os.path.getmtime(db_path), tz=datetime.timezone.utc
    )
  except OSError:
    return None


def get_cache_status(conversation_id: str) -> dict[str, Any]:
  """Inspects prompt cache state relative to the orchestrator's TTL."""
  orch = _active()["roles"]["orchestrator"]
  billing = orch["billing"]
  ttl = int(billing.get("cache_ttl_seconds") or DEFAULT_CACHE_TTL_SECONDS)
  quota_relevant = billing.get("unit") != "calls"
  clean_id = sanitize_ident(conversation_id)
  base = {
      "orchestrator_model": orch["model_id"],
      "cache_ttl_seconds": ttl,
      "affects_quota": quota_relevant,
      "estimate": True,
  }
  if not clean_id:
    return {
        **base,
        "cache_state": "UNKNOWN",
        "recommendation": "Missing conversation_id.",
    }

  hit_pct: float | None = None
  ledger_path = get_telemetry_file()
  if os.path.exists(ledger_path):
    try:
      with open(ledger_path, "r", encoding="utf-8") as f:
        for line in reversed(f.readlines()):
          line = line.strip()
          if not line:
            continue
          try:
            ev = json.loads(line)
            if ev.get("session_id") == clean_id:
              if "cache_hit_rate" in ev:
                hit_pct = float(ev["cache_hit_rate"])
              break
          except (ValueError, TypeError):
            continue
    except OSError:
      pass

  last = get_last_turn_timestamp(clean_id)
  if last is None:
    return {
        **base,
        "cache_state": "UNKNOWN",
        "seconds_since_last_turn": None,
        "seconds_until_expiry": None,
        "cache_hit_rate_pct": hit_pct,
        "recommendation": "Unable to determine last turn timestamp; status is estimated.",
    }

  elapsed = max(0, int((get_current_time() - last).total_seconds()))
  safe = max(0, ttl - 60)
  if elapsed < safe:
    state, rec = "WARM", "Continue in this conversation."
  elif elapsed < ttl:
    state, rec = "EXPIRING", "Act now or accept a cold resume."
  else:
    state, rec = "COLD", (
        "Cache already lost. This is the cheapest moment to compact or start"
        " a fresh conversation."
    )
  if not quota_relevant:
    rec = (
        f"{orch['model_id']} is call-metered, so cache state does not affect"
        " quota. Do not spend a turn keeping it warm."
    )
  return {
      **base,
      "cache_state": state,
      "seconds_since_last_turn": elapsed,
      "seconds_until_expiry": max(0, ttl - elapsed),
      "cache_hit_rate_pct": hit_pct,
      "recommendation": rec,
  }


# ------------------------------------------------------------- test tampering


def scan_diff_for_tampering(diff_output: str) -> list[dict[str, str]]:
  """Returns deleted assertions and added suppressions in test files."""
  violations: list[dict[str, str]] = []
  current_file = ""
  is_test_file = False
  for line in diff_output.splitlines():
    if line.startswith(("diff ", "--- ", "+++ ")):
      match = re.search(r"[ab]/(\S+)", line)
      if match:
        current_file = match.group(1)
        is_test_file = bool(TEST_FILE_RE.search(current_file))
      continue
    if not is_test_file:
      continue
    if line.startswith("-"):
      content = line[1:].strip()
      for pattern in ASSERTION_PATTERNS:
        if re.search(pattern, content):
          violations.append({
              "file": current_file,
              "type": "DELETED_ASSERTION",
              "pattern": pattern,
              "snippet": content,
          })
          break
    elif line.startswith("+"):
      content = line[1:].strip()
      for pattern in SUPPRESSION_PATTERNS:
        if re.search(pattern, content):
          violations.append({
              "file": current_file,
              "type": "ADDED_SUPPRESSION",
              "pattern": pattern,
              "snippet": content,
          })
          break
  return violations


def detect_test_tampering(
    workspace_dir: str, base_revision: str | None = None
) -> dict[str, Any]:
  """Audits test-file diffs for deleted assertions or added suppressions."""
  if not workspace_dir or not os.path.isdir(workspace_dir):
    return {
        "tampering_detected": False,
        "error": f"Directory not found: {workspace_dir}",
    }
  if os.path.exists(os.path.join(workspace_dir, ".hg")):
    cmd = ["hg", "diff", "-r", base_revision or "min(roots(branch(.)))"]
  else:
    cmd = ["git", "diff", base_revision or "HEAD"]
  try:
    result = subprocess.run(
        cmd,
        cwd=workspace_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
  except (OSError, subprocess.SubprocessError) as e:
    return {"tampering_detected": False, "error": f"diff failed: {e}"}
  if result.returncode not in (0, 1):
    return {
        "tampering_detected": False,
        "error": (
            f"{' '.join(cmd)} exited {result.returncode}:"
            f" {result.stderr.strip()[:500]}"
        ),
    }
  violations = scan_diff_for_tampering(result.stdout)
  return {
      "tampering_detected": bool(violations),
      "violations_count": len(violations),
      "violations": violations,
      "summary": (
          f"Found {len(violations)} test assertion or suppression violations."
          if violations
          else "All test assertions intact. Zero suppression annotations added."
      ),
  }


# ------------------------------------------------------------------ telemetry


def rotate_ledger(path: str) -> None:
  try:
    if os.path.getsize(path) > TELEMETRY_FILE_MAX_BYTES:
      os.replace(path, f"{path}.1")
  except OSError:
    pass


def record_orchestration_event(args: dict[str, Any]) -> dict[str, Any]:
  """Appends one orchestration event to the local JSONL ledger."""
  active = _active()
  roles = active["roles"]
  tier = args.get("task_tier", "tier_1_surgical")
  clean_tier = (
      f"tier_{tier}" if isinstance(tier, int) else sanitize_ident(tier, 32)
  )
  turns = sanitize_int(args.get("conductor_turns", 1), minimum=1)
  offloaded = sanitize_int(args.get("subagent_calls_offloaded", 0))
  weighted_tokens_consumed = sanitize_float(
      args.get("weighted_tokens_consumed", 0)
  )
  est_unoffloaded = sanitize_float(
      args.get("estimated_unoffloaded_tokens", 0)
  )
  if args.get("token_leverage") is not None:
    token_leverage = round(sanitize_float(args["token_leverage"]), 2)
  elif weighted_tokens_consumed == 0.0:
    token_leverage = 0.0
  else:
    token_leverage = round(
        est_unoffloaded / max(1.0, weighted_tokens_consumed), 2
    )

  event = {
      "timestamp": get_current_time().strftime("%Y-%m-%dT%H:%M:%SZ"),
      "session_id": sanitize_ident(
          args.get("session_id")
          or os.environ.get("CONVERSATION_ID", "standalone")
      ),
      "plugin_name": tc.PLUGIN_NAME,
      "orchestrator_model": sanitize_ident(
          args.get("orchestrator_model") or roles["orchestrator"]["model_id"]
      ),
      "subagent_models": {
          r: roles[r]["model_id"] for r in tc.SUBAGENT_ROLES
      },
      "pairing_verdict": active["economics"]["mode"],
      "task_tier": clean_tier,
      "task_category": sanitize_ident(args.get("task_category", "feature"), 32),
      "conductor_turns": turns,
      "subagents_spawned": sanitize_int(args.get("subagents_spawned", 1)),
      "subagent_calls_offloaded": offloaded,
      "cached_input_tokens": sanitize_int(args.get("cached_input_tokens", 0)),
      "uncached_input_tokens": sanitize_int(args.get("uncached_input_tokens", 0)),
      "cache_write_tokens": sanitize_int(args.get("cache_write_tokens", 0)),
      "output_tokens": sanitize_int(args.get("output_tokens", 0)),
      "thinking_tokens": sanitize_int(args.get("thinking_tokens", 0)),
      "weighted_tokens_consumed": weighted_tokens_consumed,
      "cache_hit_rate": round(sanitize_float(args.get("cache_hit_rate", 0)), 4),
      "token_leverage": token_leverage,
      "estimated_unoffloaded_tokens": est_unoffloaded,
      "leverage_ratio": round(offloaded / turns, 2),
      "verification_outcome": sanitize_ident(
          args.get("verification_outcome", "PASSED"), 32
      ),
      "redteam_findings_count": sanitize_int(
          args.get("redteam_findings_count", 0)
      ),
      "test_violations_count": sanitize_int(args.get("test_violations_count", 0)),
      "duration_seconds": round(
          sanitize_float(args.get("duration_seconds", 0)), 2
      ),
      "change_id": sanitize_ident(args.get("change_id") or "", 32),
      "details": str(args.get("details") or "")[:1024],
  }
  target_file = get_telemetry_file()
  with _LEDGER_LOCK:
    try:
      os.makedirs(os.path.dirname(target_file), mode=0o700, exist_ok=True)
      rotate_ledger(target_file)
      with open(target_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError as e:
      logging.warning("Failed to write ledger: %s", e)
      return {"status": "FAILED", "error": str(e)}
  return {"status": "RECORDED", "local_ledger": target_file, "event": event}


# ------------------------------------------------------------------- protocol

_INT = {"type": "integer"}
_NUM = {"type": "number"}
_STR = {"type": "string"}


def list_tools() -> list[dict[str, Any]]:
  """Returns the MCP tool list."""
  return [
      {
          "name": "get_model_config",
          "description": (
              "Returns the tokenomics-worker model setup: plugin-default models"
              " for the orchestrator and the pinned subagents (tw_scout,"
              " tw_worker, tw_redteam), this conversation's session pins, the"
              " model actually serving this conversation, and whether the"
              " pairing saves quota."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "conversation_id": {
                      "type": "string",
                      "description": "Your current conversation id (recommended).",
                  },
              },
          },
      },
      {
          "name": "list_models",
          "description": (
              "Lists model ids tokenomics-worker can pin subagents to, with"
              " enum and billing."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {"filter": {"type": "string"}},
          },
      },
      {
          "name": "set_models",
          "description": (
              "Chooses tokenomics-worker models from chat. Without persist,"
              " installs subagent pins into this conversation that are usable"
              " immediately and returns their TypeNames. With persist=true,"
              " also saves them as plugin defaults (effective after restart)."
              " The orchestrator value only sets the model used for cost math;"
              " the orchestrator itself runs on the IDE's model picker choice."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "conversation_id": {
                      "type": "string",
                      "description": "Your current conversation id.",
                  },
                  "orchestrator": {
                      "type": "string",
                      "description": "Model id the orchestrator runs on.",
                  },
                  "subagent": {
                      "type": "string",
                      "description": "Model id for every subagent role.",
                  },
                  "scout": {"type": "string", "description": "Scout-only model id; 'none' clears."},
                  "worker": {"type": "string", "description": "Worker-only model id; 'none' clears."},
                  "redteam": {"type": "string", "description": "Red-team-only model id; 'none' clears."},
                  "persist": {"type": "boolean"},
                  "strict": {
                      "type": "boolean",
                      "description": "Refuse a counterproductive pairing.",
                  },
                  "dry_run": {"type": "boolean"},
              },
          },
      },
      {
          "name": "learn_model",
          "description": (
              "Makes a model pinnable when its enum is unknown: reads the enum"
              " from a conversation that is running on that model (selected in"
              " the IDE's model picker) and records it in the plugin config."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "model_id": {
                      "type": "string",
                      "description": "Id to record, e.g. gemini-3.8-flash-high.",
                  },
                  "conversation_id": {
                      "type": "string",
                      "description": "A conversation running on that model.",
                  },
                  "label": {"type": "string"},
                  "billing_like": {
                      "type": "string",
                      "description": (
                          "Known model id whose billing to assume, e.g."
                          " opus-5-high."
                      ),
                  },
                  "billing": {
                      "type": "object",
                      "description": (
                          "Optional explicit billing specification: unit"
                          " (tokens|calls|unknown), bucket, daily_limit,"
                          " token_weights, cache_ttl_seconds."
                      ),
                      "properties": {
                          "unit": {
                              "type": "string",
                              "enum": ["tokens", "calls", "unknown"],
                          },
                          "bucket": {"type": "string"},
                          "daily_limit": {"type": "number"},
                          "token_weights": {"type": "object"},
                          "cache_ttl_seconds": {"type": "integer"},
                      },
                  },
              },
              "required": ["model_id", "conversation_id"],
          },
      },
      {
          "name": "get_quota_status",
          "description": (
              "Returns quota buckets, daily limits and cost rules for the"
              " configured orchestrator and subagent models."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "role": {
                      "type": "string",
                      "description": "'orchestrator', 'subagents' or 'all'.",
                  },
              },
          },
      },
      {
          "name": "get_cache_status",
          "description": (
              "Inspects prompt cache state for a conversation against the"
              " configured orchestrator's cache TTL, and says whether cache"
              " state affects quota for that model."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "conversation_id": {
                      "type": "string",
                      "description": "Conversation UUID to inspect.",
                  },
              },
              "required": ["conversation_id"],
          },
      },
      {
          "name": "detect_test_tampering",
          "description": (
              "Audits git diffs of test files for deleted assertions or"
              " added suppression annotations (@Suppress, // NOLINT, #"
              " pylint: disable, skip decorators)."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "workspace_dir": {
                      "type": "string",
                      "description": "Absolute path to the workspace root.",
                  },
                  "base_revision": {
                      "type": "string",
                      "description": "Optional base revision to diff against.",
                  },
              },
              "required": ["workspace_dir"],
          },
      },
      {
          "name": "record_orchestration_event",
          "description": (
              "Appends an orchestration milestone to the local ledger"
              " (telemetry/tokenomics_events.jsonl). Models default to the"
              " active configuration."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "task_tier": {
                      "type": "string",
                      "description": (
                          "'tier_1_surgical', 'tier_2_pipeline' or"
                          " 'tier_3_competing'."
                      ),
                  },
                  "task_category": _STR,
                  "verification_outcome": {
                      "type": "string",
                      "description": (
                          "'PASSED', 'FAILED', 'CIRCUIT_BREAKER_HALTED' or"
                          " 'TAMPERING_DETECTED'."
                      ),
                  },
                  "orchestrator_model": _STR,
                  "conductor_turns": _INT,
                  "subagents_spawned": _INT,
                  "subagent_calls_offloaded": _INT,
                  "cached_input_tokens": _INT,
                  "uncached_input_tokens": _INT,
                  "cache_write_tokens": _INT,
                  "output_tokens": _INT,
                  "thinking_tokens": _INT,
                  "weighted_tokens_consumed": _NUM,
                  "token_leverage": _NUM,
                  "cache_hit_rate": _NUM,
                  "estimated_unoffloaded_tokens": _NUM,
                  "redteam_findings_count": _INT,
                  "test_violations_count": _INT,
                  "duration_seconds": _NUM,
                  "change_id": _STR,
                  "details": {
                      "type": "string",
                      "description": "Brief summary; no secrets or PII.",
                  },
                  "session_id": _STR,
              },
              "required": ["task_tier", "verification_outcome"],
          },
      },
  ]


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "get_model_config": lambda a: get_model_config(a.get("conversation_id")),
    "list_models": lambda a: list_models(a.get("filter")),
    "set_models": set_models,
    "learn_model": learn_model_tool,
    "get_quota_status": lambda a: get_quota_status(a.get("role")),
    "get_cache_status": lambda a: get_cache_status(a.get("conversation_id", "")),
    "detect_test_tampering": lambda a: detect_test_tampering(
        a.get("workspace_dir", ""), a.get("base_revision")
    ),
    "record_orchestration_event": record_orchestration_event,
}


def _tool_result(req_id: Any, text: str, is_error: bool) -> dict[str, Any]:
  return {
      "jsonrpc": "2.0",
      "id": req_id,
      "result": {
          "content": [{"type": "text", "text": text}],
          "isError": is_error,
      },
  }


def handle_json_rpc_request(req: dict[str, Any]) -> dict[str, Any] | None:
  """Dispatches one JSON-RPC request."""
  method = req.get("method")
  req_id = req.get("id")

  if method == "initialize":
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    }
  if method == "notifications/initialized" or (
      req_id is None and str(method).startswith("notifications/")
  ):
    return None
  if method == "ping":
    return {"jsonrpc": "2.0", "id": req_id, "result": {}}
  if method == "tools/list":
    return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": list_tools()}}
  if method == "tools/call":
    params = req.get("params") or {}
    name = params.get("name")
    tool_args = params.get("arguments") or {}
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
      return _tool_result(req_id, f"Unknown tool: {name}", True)
    try:
      return _tool_result(req_id, json.dumps(handler(tool_args), indent=2), False)
    except (
        tc.ConfigError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as e:
      return _tool_result(req_id, f"Error executing tool '{name}': {e}", True)
  return {
      "jsonrpc": "2.0",
      "id": req_id,
      "error": {"code": -32601, "message": f"Method '{method}' not found"},
  }


def process_message(line: str) -> str | None:
  stripped = line.strip()
  if not stripped:
    return None
  try:
    req = json.loads(stripped)
  except json.JSONDecodeError as e:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": f"Parse error: {e}"},
    })
  if not isinstance(req, dict):
    return json.dumps({
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request: expected object"},
    })
  resp = handle_json_rpc_request(req)
  return json.dumps(resp) if resp is not None else None


def run_stdio_server(
    input_stream: TextIO | None = None, output_stream: TextIO | None = None
) -> int:
  in_stream = input_stream if input_stream is not None else sys.stdin
  out_stream = output_stream if output_stream is not None else sys.stdout
  for line in in_stream:
    resp = process_message(line)
    if resp is not None:
      out_stream.write(resp + "\n")
      out_stream.flush()
  return 0


if __name__ == "__main__":
  sys.exit(run_stdio_server())
