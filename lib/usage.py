"""Measured token usage collection, caching, snapshot ledger, and billing inference.

Queries GetCascadeTrajectory on the local Antigravity Language Server from the
CLI agent environment, strips all conversation content to retain only per-model
token counters and conversation IDs, caches results in usage.json, appends
quota+usage snapshots to usage_snapshots.jsonl, and empirically infers whether a
quota pool is call-metered or token-metered.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import datetime
import io
import json
import math
import os
import re
from typing import Any

import catalog
import tokenomics_config as tc

_CONV_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_ENUM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
MAX_SNAPSHOT_LINES = 500
_COUNTER_KEYS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
_UNTRACKED_NOISE_CAVEAT = (
    "Usage from conversations not tracked (other windows, other clients) on the"
    " same quota pool adds noise to quota drops."
)


def _now_iso() -> str:
  """Returns current UTC timestamp in ISO 8601 Z format."""
  return datetime.datetime.now(datetime.timezone.utc).strftime(
      "%Y-%m-%dT%H:%M:%SZ"
  )


def _empty_counters() -> dict[str, int]:
  """Returns a fresh zeroed counter dictionary."""
  return {
      "calls": 0,
      "input_tokens": 0,
      "output_tokens": 0,
      "cache_read_tokens": 0,
      "cache_write_tokens": 0,
  }


def _parse_nonneg_int(val: Any) -> int:
  """Parses a protobuf int64 string or int into a non-negative integer."""
  if val is None or isinstance(val, bool):
    return 0
  try:
    return max(0, int(val))
  except (ValueError, TypeError):
    return 0


def _extract_counters(
    usage_obj: dict[str, Any],
) -> tuple[str | None, dict[str, int]]:
  """Extracts (enum, counters) from a trajectory usage dictionary."""
  model_raw = usage_obj.get("model")
  if not isinstance(model_raw, str) or not model_raw.strip():
    return None, _empty_counters()
  enum = model_raw.strip()
  if not _ENUM_TOKEN_RE.match(enum):
    return None, _empty_counters()
  input_tok = _parse_nonneg_int(
      usage_obj.get("inputTokens")
      if "inputTokens" in usage_obj
      else usage_obj.get("input_tokens")
  )
  if "outputTokens" in usage_obj:
    output_tok = _parse_nonneg_int(usage_obj.get("outputTokens"))
  elif "output_tokens" in usage_obj:
    output_tok = _parse_nonneg_int(usage_obj.get("output_tokens"))
  else:
    output_tok = _parse_nonneg_int(
        usage_obj.get("responseOutputTokens")
    ) + _parse_nonneg_int(usage_obj.get("thinkingOutputTokens"))

  cache_read_tok = _parse_nonneg_int(
      usage_obj.get("cacheReadTokens")
      if "cacheReadTokens" in usage_obj
      else usage_obj.get("cache_read_tokens")
  )
  cache_write_raw = (
      usage_obj.get("cacheWriteTokens")
      if "cacheWriteTokens" in usage_obj
      else (
          usage_obj.get("cacheCreationTokens")
          if "cacheCreationTokens" in usage_obj
          else (
              usage_obj.get("cacheCreationInputTokens")
              if "cacheCreationInputTokens" in usage_obj
              else usage_obj.get("cache_write_tokens")
          )
      )
  )
  cache_write_tok = _parse_nonneg_int(cache_write_raw)

  return enum, {
      "calls": 1,
      "input_tokens": input_tok,
      "output_tokens": output_tok,
      "cache_read_tokens": cache_read_tok,
      "cache_write_tokens": cache_write_tok,
  }


def _has_any_tokens(counters: dict[str, int]) -> bool:
  """Returns True if any token counter is positive."""
  return (
      counters["input_tokens"] > 0
      or counters["output_tokens"] > 0
      or counters["cache_read_tokens"] > 0
      or counters["cache_write_tokens"] > 0
  )


def _add_counters(
    target: dict[str, dict[str, int]], enum: str, counters: dict[str, int]
) -> None:
  """Adds counters for enum into target per_model map in place."""
  bucket = target.setdefault(enum, _empty_counters())
  for k in _COUNTER_KEYS:
    bucket[k] = bucket.get(k, 0) + _parse_nonneg_int(counters.get(k, 0))


def _same_usage_call(a: dict[str, Any], b: dict[str, Any]) -> bool:
  """Returns True if two raw usage objects represent the same model call."""
  if a == b:
    return True
  msg_a = a.get("messageId")
  msg_b = b.get("messageId")
  if isinstance(msg_a, str) and msg_a and msg_a == msg_b:
    return True
  resp_a = a.get("responseId")
  resp_b = b.get("responseId")
  if isinstance(resp_a, str) and resp_a and resp_a == resp_b:
    return True
  return False


def _collect_child_ids_from_node(
    node: Any, parent_id: str, out: list[str], seen: set[str]
) -> None:
  """Collects valid conversationId strings from subagent step nodes."""
  if isinstance(node, dict):
    for k, v in node.items():
      if k in ("conversationId", "conversation_id", "subagentConversationId"):
        if isinstance(v, str):
          cid = v.strip()
          if (
              cid
              and cid != parent_id
              and _CONV_ID_RE.match(cid)
              and cid not in seen
          ):
            seen.add(cid)
            out.append(cid)
      else:
        _collect_child_ids_from_node(v, parent_id, out, seen)
  elif isinstance(node, list):
    for item in node:
      _collect_child_ids_from_node(item, parent_id, out, seen)


def parse_trajectory_usage(
    data: dict[str, Any], conversation_id: str, fetched_at: str | None = None
) -> dict[str, Any]:
  """Extracts per-model token usage and child IDs from a GetCascadeTrajectory dict."""
  traj = data.get("trajectory") if isinstance(data, dict) else None
  if not isinstance(traj, dict):
    traj = data if isinstance(data, dict) else {}

  per_model: dict[str, dict[str, int]] = {}
  seen_call_ids: set[str] = set()
  gm_step_indices: set[int] = set()
  gm_usage_objs: list[dict[str, Any]] = []

  gen_meta = traj.get("generatorMetadata")
  if isinstance(gen_meta, list):
    for gm in gen_meta:
      if not isinstance(gm, dict):
        continue
      step_indices = gm.get("stepIndices")
      if isinstance(step_indices, list):
        for s_idx in step_indices:
          if isinstance(s_idx, int):
            gm_step_indices.add(s_idx)

      cm = gm.get("chatModel")
      if not isinstance(cm, dict):
        continue
      main_u = cm.get("usage") if isinstance(cm.get("usage"), dict) else None
      retry_infos = (
          cm.get("retryInfos") if isinstance(cm.get("retryInfos"), list) else []
      )

      for idx, ri in enumerate(retry_infos):
        if not isinstance(ri, dict):
          continue
        ri_u = ri.get("usage")
        if not isinstance(ri_u, dict):
          continue
        enum, counters = _extract_counters(ri_u)
        if not enum:
          continue
        if main_u is not None:
          if _same_usage_call(ri_u, main_u):
            continue
          if idx == len(retry_infos) - 1 and not _has_any_tokens(counters):
            continue
        if (
            (ri.get("error") or ri.get("retryReason"))
            and not _has_any_tokens(counters)
        ):
          continue
        if (
            not _has_any_tokens(counters)
            and not ri_u.get("messageId")
            and not ri_u.get("responseId")
        ):
          continue
        call_id = ri_u.get("messageId") or ri_u.get("responseId")
        if isinstance(call_id, str) and call_id:
          if call_id in seen_call_ids:
            continue
          seen_call_ids.add(call_id)
        gm_usage_objs.append(ri_u)
        _add_counters(per_model, enum, counters)

      if main_u is not None:
        enum, counters = _extract_counters(main_u)
        if not enum:
          continue
        if (
            gm.get("error")
            and not _has_any_tokens(counters)
            and not main_u.get("messageId")
            and not main_u.get("responseId")
        ):
          continue
        call_id = main_u.get("messageId") or main_u.get("responseId")
        if isinstance(call_id, str) and call_id:
          if call_id in seen_call_ids:
            continue
          seen_call_ids.add(call_id)
        gm_usage_objs.append(main_u)
        _add_counters(per_model, enum, counters)

  child_ids: list[str] = []
  seen_children: set[str] = set()
  matched_gm_indices: set[int] = set()
  steps = traj.get("steps")
  if isinstance(steps, list):
    for s_idx, step in enumerate(steps):
      if not isinstance(step, dict):
        continue
      for sub_key in ("invokeSubagent", "invoke_subagent", "subagentResult"):
        if sub_key in step:
          _collect_child_ids_from_node(
              step[sub_key], conversation_id, child_ids, seen_children
          )
      cp = step.get("checkpoint")
      if isinstance(cp, dict) and "subagentSnapshots" in cp:
        _collect_child_ids_from_node(
            cp["subagentSnapshots"], conversation_id, child_ids, seen_children
        )

      md = step.get("metadata")
      if isinstance(md, dict) and isinstance(md.get("modelUsage"), dict):
        if s_idx in gm_step_indices:
          continue
        mu = md["modelUsage"]
        call_id = mu.get("messageId") or mu.get("responseId")
        if isinstance(call_id, str) and call_id and call_id in seen_call_ids:
          continue
        matched_gm = False
        for g_idx, prev_u in enumerate(gm_usage_objs):
          if g_idx not in matched_gm_indices and _same_usage_call(mu, prev_u):
            matched_gm_indices.add(g_idx)
            matched_gm = True
            break
        if matched_gm:
          continue
        enum, counters = _extract_counters(mu)
        if enum:
          if isinstance(call_id, str) and call_id:
            seen_call_ids.add(call_id)
          _add_counters(per_model, enum, counters)

  return {
      "conversation_id": conversation_id,
      "fetched_at": fetched_at or _now_iso(),
      "per_model": per_model,
      "child_conversation_ids": child_ids,
  }


def fetch_conversation_usage(
    conversation_id: str,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
  """Fetches trajectory usage for a single conversation from the local LS."""
  clean_id = (conversation_id or "").strip()
  if not clean_id or not _CONV_ID_RE.match(clean_id):
    raise tc.ConfigError(f"invalid conversation id {conversation_id!r}")

  effective_env = os.environ if env is None else env
  ls_addr = effective_env.get("ANTIGRAVITY_LS_ADDRESS")
  token = effective_env.get("ANTIGRAVITY_CSRF_TOKEN")
  if not ls_addr or not token:
    raise tc.ConfigError(
        "ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN are required to"
        " fetch conversation usage"
    )

  raw_data = catalog.call_language_server(
      ls_addr,
      token,
      "GetCascadeTrajectory",
      {"cascadeId": clean_id},
      timeout=timeout,
  )
  return parse_trajectory_usage(raw_data, clean_id, fetched_at=_now_iso())


def _sanitize_conv_record(rec: dict[str, Any]) -> dict[str, Any]:
  """Ensures a conversation usage record contains only ids, timestamps, enums, and numbers."""
  raw_cid = str(rec.get("conversation_id") or "").strip()
  cid = raw_cid if _CONV_ID_RE.match(raw_cid) else ""
  raw_ts = str(rec.get("fetched_at") or "").strip()
  fetched_at = (
      raw_ts if catalog._parse_iso_utc(raw_ts) is not None else _now_iso()
  )
  raw_pm = rec.get("per_model")
  clean_pm: dict[str, dict[str, int]] = {}
  if isinstance(raw_pm, dict):
    for enum, counts in raw_pm.items():
      if (
          isinstance(enum, str)
          and _ENUM_TOKEN_RE.match(enum.strip())
          and isinstance(counts, dict)
      ):
        clean_pm[enum.strip()] = {
            k: _parse_nonneg_int(counts.get(k, 0)) for k in _COUNTER_KEYS
        }
  raw_children = rec.get("child_conversation_ids")
  clean_children: list[str] = []
  if isinstance(raw_children, list):
    for ch in raw_children:
      if isinstance(ch, str) and _CONV_ID_RE.match(ch.strip()):
        if ch.strip() not in clean_children and ch.strip() != cid:
          clean_children.append(ch.strip())
  return {
      "conversation_id": cid,
      "fetched_at": fetched_at,
      "per_model": clean_pm,
      "child_conversation_ids": clean_children,
  }


def sum_conversations_usage(
    conversations: Mapping[str, dict[str, Any]],
    conversation_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, int]]:
  """Sums per_model counters across selected (or all) conversations."""
  totals: dict[str, dict[str, int]] = {}
  ids = (
      list(conversation_ids)
      if conversation_ids is not None
      else list(conversations.keys())
  )
  for cid in ids:
    conv = conversations.get(cid)
    if not isinstance(conv, dict):
      continue
    pm = conv.get("per_model")
    if not isinstance(pm, dict):
      continue
    for enum, counts in pm.items():
      if isinstance(enum, str) and isinstance(counts, dict):
        _add_counters(totals, enum, counts)
  return totals


def _is_conv_cache_fresh(
    conv_rec: dict[str, Any] | None, max_age_seconds: float
) -> bool:
  """Returns True if conv_rec has a fetched_at timestamp younger than max_age_seconds."""
  if not isinstance(conv_rec, dict) or max_age_seconds <= 0:
    return False
  ts = catalog._parse_iso_utc(conv_rec.get("fetched_at", ""))
  if ts is None:
    return False
  now = datetime.datetime.now(datetime.timezone.utc)
  age = (now - ts).total_seconds()
  return 0.0 <= age < max_age_seconds


def collect_usage(
    root_conversation_id: str | None = None,
    extra_ids: Iterable[str] = (),
    max_children: int = 50,
    recursive: bool = True,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    cached_conversations: Mapping[str, dict[str, Any]] | None = None,
    min_recheck_seconds: float = 60.0,
) -> dict[str, Any]:
  """Walks root + extra conversation IDs and their children, aggregating usage."""
  effective_env = os.environ if env is None else env
  root_id = (
      root_conversation_id
      or effective_env.get("ANTIGRAVITY_CONVERSATION_ID")
      or ""
  ).strip()

  seed_ids: list[str] = []
  if root_id:
    seed_ids.append(root_id)
  for extra in extra_ids or ():
    clean_extra = (extra or "").strip()
    if clean_extra and clean_extra not in seed_ids:
      seed_ids.append(clean_extra)

  if not seed_ids:
    raise tc.ConfigError("At least one conversation ID is required to collect usage")

  conversations: dict[str, dict[str, Any]] = {}
  errors: dict[str, str] = {}
  queue: list[tuple[str, int]] = [(cid, 0) for cid in seed_ids]
  visited: set[str] = set(seed_ids)
  children_collected = 0

  while queue:
    cid, depth = queue.pop(0)
    is_root = cid == root_id
    cached_rec = (
        cached_conversations.get(cid)
        if isinstance(cached_conversations, Mapping)
        else None
    )
    if (
        not is_root
        and cached_rec is not None
        and _is_conv_cache_fresh(cached_rec, min_recheck_seconds)
    ):
      rec = _sanitize_conv_record(cached_rec)
      conversations[cid] = rec
    else:
      try:
        rec = fetch_conversation_usage(cid, env=effective_env, timeout=timeout)
        conversations[cid] = _sanitize_conv_record(rec)
      except Exception as e:
        err_msg = str(e)
        tok = (effective_env.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
        if tok and tok in err_msg:
          err_msg = err_msg.replace(tok, "[REDACTED]")
        errors[cid] = err_msg
        if cached_rec is not None:
          conversations[cid] = _sanitize_conv_record(cached_rec)
          rec = conversations[cid]
        else:
          continue

    if depth == 0 or recursive:
      for child_id in rec.get("child_conversation_ids") or []:
        if child_id not in visited and children_collected < max_children:
          visited.add(child_id)
          children_collected += 1
          queue.append((child_id, depth + 1))

  if not conversations and errors:
    first_err = next(iter(errors.values()))
    raise tc.ConfigError(f"Failed to collect conversation usage: {first_err}")

  now_iso = _now_iso()
  totals = sum_conversations_usage(conversations)
  return {
      "root_conversation_id": root_id or seed_ids[0],
      "fetched_at": now_iso,
      "conversations": conversations,
      "totals": totals,
      "errors": errors,
  }


def usage_cache_path(app_dir: str) -> str:
  """Returns the usage.json cache path for an app data directory."""
  return os.path.join(tc.plugin_data_dir(app_dir), "usage.json")


def snapshots_path(app_dir: str) -> str:
  """Returns the usage_snapshots.jsonl ledger path for an app data directory."""
  return os.path.join(tc.plugin_data_dir(app_dir), "usage_snapshots.jsonl")


def _read_usage_file(path: str) -> dict[str, Any] | None:
  """Safely reads and validates a usage.json file."""
  try:
    with io.open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("conversations"), dict):
      return data
  except (OSError, ValueError):
    pass
  return None


def read_usage(app_dir: str | None = None) -> dict[str, Any] | None:
  """Reads usage.json from app_dir, or the freshest across all app dirs."""
  if app_dir is not None:
    return _read_usage_file(usage_cache_path(app_dir))

  env_override = os.environ.get(tc.APP_DIR_ENV)
  if env_override:
    return _read_usage_file(usage_cache_path(os.path.expanduser(env_override)))

  candidates: list[str] = []
  pattern = os.path.expanduser("~/.gemini/*")
  for base in sorted(tc.glob.glob(pattern)):
    if os.path.isdir(base) and base not in candidates:
      candidates.append(base)

  newest_usage: dict[str, Any] | None = None
  newest_time: datetime.datetime | None = None

  for candidate in candidates:
    target = usage_cache_path(candidate)
    u_data = _read_usage_file(target)
    if not u_data:
      continue
    ts_str = u_data.get("updated_at") or u_data.get("fetched_at") or ""
    ts = catalog._parse_iso_utc(ts_str)
    if ts is None:
      try:
        mtime = os.path.getmtime(target)
        ts = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
      except OSError:
        continue
    if newest_time is None or ts > newest_time:
      newest_time = ts
      newest_usage = u_data

  return newest_usage


def write_usage(
    usage_data: dict[str, Any], app_dir: str, merge: bool = True
) -> str:
  """Atomically writes sanitized per-conversation usage to usage.json."""
  target = usage_cache_path(app_dir)
  os.makedirs(os.path.dirname(target), exist_ok=True)

  merged_convs: dict[str, dict[str, Any]] = {}
  if merge:
    existing = _read_usage_file(target)
    if existing and isinstance(existing.get("conversations"), dict):
      for cid, rec in existing["conversations"].items():
        if isinstance(cid, str) and isinstance(rec, dict):
          merged_convs[cid] = _sanitize_conv_record(rec)

  new_convs = usage_data.get("conversations")
  if isinstance(new_convs, dict):
    for cid, rec in new_convs.items():
      if isinstance(cid, str) and isinstance(rec, dict):
        merged_convs[cid] = _sanitize_conv_record(rec)
  elif (
      isinstance(usage_data.get("conversation_id"), str)
      and isinstance(usage_data.get("per_model"), dict)
  ):
    san = _sanitize_conv_record(usage_data)
    merged_convs[san["conversation_id"]] = san

  updated_at = str(
      usage_data.get("updated_at")
      or usage_data.get("fetched_at")
      or _now_iso()
  )
  payload = {
      "updated_at": updated_at,
      "conversations": merged_convs,
      "totals": sum_conversations_usage(merged_convs),
  }

  tmp = target + ".tmp"
  with io.open(tmp, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
  os.replace(tmp, target)
  return target


def usage_age_seconds(
    usage_data: dict[str, Any] | None, conversation_id: str | None = None
) -> float | None:
  """Returns the age in seconds of usage_data (or a specific conversation in it)."""
  if not usage_data or not isinstance(usage_data, dict):
    return None
  ts_str = ""
  if conversation_id:
    convs = usage_data.get("conversations") or {}
    conv_rec = convs.get(conversation_id)
    if isinstance(conv_rec, dict):
      ts_str = str(conv_rec.get("fetched_at") or "")
  if not ts_str:
    ts_str = str(
        usage_data.get("updated_at") or usage_data.get("fetched_at") or ""
    )
  ts = catalog._parse_iso_utc(ts_str)
  if ts is None:
    return None
  now = datetime.datetime.now(datetime.timezone.utc)
  return max(0.0, (now - ts).total_seconds())


def build_snapshot(
    collected: dict[str, Any],
    catalog_data: dict[str, Any] | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
  """Builds a snapshot entry combining quota from catalog_data and usage totals."""
  quota_map: dict[str, dict[str, Any]] = {}
  for m in (catalog_data or {}).get("models") or []:
    if not isinstance(m, dict):
      continue
    mid = m.get("model_id")
    if not isinstance(mid, str) or not mid.strip():
      continue
    quota_map[mid.strip()] = {
        "enum": m.get("enum"),
        "remaining_fraction": m.get("remaining_fraction"),
        "reset_time": m.get("reset_time"),
        "quota_group": m.get("quota_group"),
    }

  convs = collected.get("conversations") or {}
  by_conv: dict[str, dict[str, dict[str, int]]] = {}
  conv_ids: list[str] = []
  if isinstance(convs, dict):
    for cid, rec in convs.items():
      if isinstance(cid, str) and isinstance(rec, dict):
        san = _sanitize_conv_record(rec)
        conv_ids.append(cid)
        by_conv[cid] = san["per_model"]

  if by_conv:
    totals = sum_conversations_usage(convs, conv_ids)
  else:
    totals = {}
    raw_totals = collected.get("totals")
    if isinstance(raw_totals, dict):
      for enum, counts in raw_totals.items():
        if (
            isinstance(enum, str)
            and _ENUM_TOKEN_RE.match(enum.strip())
            and isinstance(counts, dict)
        ):
          _add_counters(totals, enum.strip(), counts)
  return {
      "ts": ts or str(collected.get("fetched_at") or _now_iso()),
      "quota": quota_map,
      "totals": totals,
      "conversations": conv_ids,
      "by_conversation": by_conv,
  }


def append_snapshot(
    snapshot: dict[str, Any],
    app_dir: str,
    max_lines: int = MAX_SNAPSHOT_LINES,
) -> str:
  """Appends one snapshot JSON line to usage_snapshots.jsonl, capped at max_lines."""
  target = snapshots_path(app_dir)
  os.makedirs(os.path.dirname(target), exist_ok=True)
  new_line = json.dumps(snapshot, separators=(",", ":"))
  existing_lines: list[str] = []
  if max_lines > 0 and os.path.exists(target):
    try:
      with io.open(target, "r", encoding="utf-8") as f:
        existing_lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
      existing_lines = []
  if max_lines > 0 and len(existing_lines) + 1 > max_lines:
    kept = (existing_lines + [new_line])[-max_lines:]
    tmp = target + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
      for ln in kept:
        f.write(ln + "\n")
    os.replace(tmp, target)
  else:
    with io.open(target, "a", encoding="utf-8") as f:
      f.write(new_line + "\n")
  return target


def _read_snapshots_file(path: str) -> list[dict[str, Any]]:
  """Reads all valid JSON objects from a usage_snapshots.jsonl file."""
  out: list[dict[str, Any]] = []
  if not os.path.exists(path):
    return out
  try:
    with io.open(path, "r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          obj = json.loads(line)
          if isinstance(obj, dict):
            out.append(obj)
        except ValueError:
          continue
  except OSError:
    pass
  return out


def read_snapshots(app_dir: str | None = None) -> list[dict[str, Any]]:
  """Reads the snapshot ledger from app_dir, or the freshest across app dirs."""
  if app_dir is not None:
    return _read_snapshots_file(snapshots_path(app_dir))

  env_override = os.environ.get(tc.APP_DIR_ENV)
  if env_override:
    return _read_snapshots_file(
        snapshots_path(os.path.expanduser(env_override))
    )

  candidates: list[str] = []
  pattern = os.path.expanduser("~/.gemini/*")
  for base in sorted(tc.glob.glob(pattern)):
    if os.path.isdir(base) and base not in candidates:
      candidates.append(base)

  newest_snaps: list[dict[str, Any]] = []
  newest_mtime: float | None = None

  for candidate in candidates:
    target = snapshots_path(candidate)
    if not os.path.exists(target):
      continue
    snaps = _read_snapshots_file(target)
    if not snaps:
      continue
    try:
      mtime = os.path.getmtime(target)
    except OSError:
      continue
    if newest_mtime is None or mtime > newest_mtime:
      newest_mtime = mtime
      newest_snaps = snaps

  return newest_snaps


def _extract_snapshot_by_conv(
    snap: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, dict[str, int]]] | None]:
  """Returns (conversation_ids, by_conversation_map_or_None) for a snapshot."""
  by_conv = snap.get("by_conversation") or snap.get("per_conversation")
  convs = snap.get("conversations")
  if isinstance(convs, dict) and not isinstance(by_conv, dict):
    by_conv = convs
    conv_ids = [str(k) for k in convs.keys()]
  elif isinstance(convs, list):
    conv_ids = [str(x) for x in convs if isinstance(x, str)]
  elif isinstance(by_conv, dict):
    conv_ids = [str(k) for k in by_conv.keys()]
  else:
    conv_ids = []
  return conv_ids, (by_conv if isinstance(by_conv, dict) else None)


def snapshot_delta(
    prev_snapshot: dict[str, Any], curr_snapshot: dict[str, Any]
) -> dict[str, Any]:
  """Computes usage deltas strictly over conversations present in both snapshots."""
  prev_ids, prev_by_conv = _extract_snapshot_by_conv(prev_snapshot)
  curr_ids, curr_by_conv = _extract_snapshot_by_conv(curr_snapshot)

  prev_id_set = set(prev_ids)
  common_ids = [cid for cid in curr_ids if cid in prev_id_set]

  if prev_by_conv is not None and curr_by_conv is not None:
    if not common_ids:
      return {"common_conversations": [], "per_model": {}}
    prev_totals: dict[str, dict[str, int]] = {}
    curr_totals: dict[str, dict[str, int]] = {}
    for cid in common_ids:
      for enum, counts in (prev_by_conv.get(cid) or {}).items():
        if isinstance(enum, str) and isinstance(counts, dict):
          _add_counters(prev_totals, enum, counts)
      for enum, counts in (curr_by_conv.get(cid) or {}).items():
        if isinstance(enum, str) and isinstance(counts, dict):
          _add_counters(curr_totals, enum, counts)
  else:
    if (prev_ids or curr_ids) and not common_ids:
      return {"common_conversations": [], "per_model": {}}
    if prev_by_conv is not None and common_ids:
      prev_totals = {}
      for cid in common_ids:
        for enum, counts in (prev_by_conv.get(cid) or {}).items():
          if isinstance(enum, str) and isinstance(counts, dict):
            _add_counters(prev_totals, enum, counts)
    elif not prev_ids or set(prev_ids) == set(common_ids):
      prev_totals = prev_snapshot.get("totals") or {}
    else:
      return {"common_conversations": common_ids, "per_model": {}}

    if curr_by_conv is not None and common_ids:
      curr_totals = {}
      for cid in common_ids:
        for enum, counts in (curr_by_conv.get(cid) or {}).items():
          if isinstance(enum, str) and isinstance(counts, dict):
            _add_counters(curr_totals, enum, counts)
    elif not curr_ids or set(curr_ids) == set(common_ids):
      curr_totals = curr_snapshot.get("totals") or {}
    else:
      return {"common_conversations": common_ids, "per_model": {}}

  delta_per_model: dict[str, dict[str, int]] = {}
  all_enums = set(prev_totals.keys()) | set(curr_totals.keys())
  for enum in sorted(all_enums):
    p_counts = prev_totals.get(enum) or {}
    c_counts = curr_totals.get(enum) or {}
    diff = {
        k: _parse_nonneg_int(c_counts.get(k, 0))
        - _parse_nonneg_int(p_counts.get(k, 0))
        for k in _COUNTER_KEYS
    }
    if any(v != 0 for v in diff.values()):
      delta_per_model[enum] = diff

  return {
      "common_conversations": common_ids,
      "per_model": delta_per_model,
  }


def take_usage_snapshot(
    app_dir: str,
    catalog_data: dict[str, Any] | None = None,
    root_conversation_id: str | None = None,
    extra_ids: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    min_recheck_seconds: float = 60.0,
    max_children: int = 50,
    timeout: float = 30.0,
) -> dict[str, Any]:
  """Collects usage, updates usage.json, and appends a snapshot to the ledger."""
  existing_usage = read_usage(app_dir)
  cached_convs = (
      existing_usage.get("conversations")
      if isinstance(existing_usage, dict)
      else None
  )
  prev_snapshots = read_snapshots(app_dir)
  prev_snap = prev_snapshots[-1] if prev_snapshots else None

  collected = collect_usage(
      root_conversation_id=root_conversation_id,
      extra_ids=extra_ids,
      max_children=max_children,
      env=env,
      timeout=timeout,
      cached_conversations=cached_convs,
      min_recheck_seconds=min_recheck_seconds,
  )
  u_path = write_usage(collected, app_dir, merge=True)
  merged_usage = read_usage(app_dir) or collected
  snapshot = build_snapshot(
      merged_usage, catalog_data=catalog_data, ts=collected["fetched_at"]
  )
  s_path = append_snapshot(snapshot, app_dir)

  if prev_snap is not None:
    delta = snapshot_delta(prev_snap, snapshot)
    calls_since_prev = sum(
        max(0, m.get("calls", 0)) for m in delta["per_model"].values()
    )
  else:
    calls_since_prev = sum(
        max(0, m.get("calls", 0)) for m in snapshot["totals"].values()
    )

  return {
      "snapshot": snapshot,
      "usage_path": u_path,
      "snapshots_path": s_path,
      "tracked_conversations": len(snapshot["conversations"]),
      "calls_since_previous": calls_since_prev,
      "has_previous": prev_snap is not None,
  }


def _resolve_group_in_snapshot(
    snap: dict[str, Any], target: str
) -> tuple[str | None, float | None, str | None, set[str], set[str]]:
  """Resolves (quota_group, remaining_fraction, reset_time, model_ids, enums) in snap."""
  quota_map = snap.get("quota")
  if not isinstance(quota_map, dict) or not quota_map:
    return None, None, None, set(), set()

  anchor_id: str | None = None
  anchor_info: dict[str, Any] | None = None

  if target in quota_map and isinstance(quota_map[target], dict):
    anchor_id = target
    anchor_info = quota_map[target]
  else:
    for mid, m_info in quota_map.items():
      if not isinstance(m_info, dict):
        continue
      if m_info.get("quota_group") == target or m_info.get("enum") == target:
        anchor_id = mid
        anchor_info = m_info
        break

  if anchor_info is None:
    return None, None, None, set(), set()

  qg = anchor_info.get("quota_group")
  rf_raw = anchor_info.get("remaining_fraction")
  rf: float | None = None
  if rf_raw is not None and not isinstance(rf_raw, bool):
    try:
      val = float(rf_raw)
      if not math.isnan(val) and not math.isinf(val):
        rf = val
    except (ValueError, TypeError):
      rf = None
  rt = anchor_info.get("reset_time")

  mids: set[str] = set()
  enums: set[str] = set()
  for mid, m_info in quota_map.items():
    if not isinstance(m_info, dict):
      continue
    same_group = False
    if mid == anchor_id:
      same_group = True
    elif qg is not None and m_info.get("quota_group") == qg:
      same_group = True
    elif (
        qg is None
        and rf is not None
        and rt is not None
        and m_info.get("remaining_fraction") == rf_raw
        and m_info.get("reset_time") == rt
    ):
      same_group = True
    if same_group:
      mids.add(mid)
      enum = m_info.get("enum")
      if isinstance(enum, str) and enum.strip():
        enums.add(enum.strip())

  if target.startswith("MODEL_"):
    enums.add(target)

  return qg, rf, (str(rt) if rt is not None else None), mids, enums


def _compute_measure_fit(
    pairs: list[dict[str, float]], key: str
) -> dict[str, float | None]:
  """Computes units_per_full_quota and coefficient of variation (cv) for a measure."""
  ratios = [
      p["quota_drop"] / p[key]
      for p in pairs
      if p.get(key, 0.0) > 0.0 and p.get("quota_drop", 0.0) > 0.0
  ]
  n = len(ratios)
  if n == 0:
    return {"units_per_full_quota": None, "cv": None}
  mean_r = sum(ratios) / n
  if mean_r <= 0.0:
    return {"units_per_full_quota": None, "cv": None}
  units_full = round(1.0 / mean_r, 2)
  if n < 2:
    return {"units_per_full_quota": units_full, "cv": None}
  variance = sum((r - mean_r) ** 2 for r in ratios) / n
  std_r = math.sqrt(max(0.0, variance))
  cv = round(std_r / mean_r, 6)
  return {"units_per_full_quota": units_full, "cv": cv}


def infer_billing(
    snapshots: list[dict[str, Any]],
    target: str,
    min_pairs: int = 3,
    min_drop: float = 0.002,
) -> dict[str, Any]:
  """Empirically infers billing unit ('calls' vs 'tokens') from usage snapshots.

  Walks snapshots in order and accumulates usage deltas (over conversations
  common to each consecutive pair) into an open segment until the accumulated
  quota drop reaches at least `min_drop` (default 0.002), then emits one sample
  `(accumulated_usage, accumulated_drop)` and starts a new segment. Trailing
  usage with no closing drop is reported in `pending_usage`.

  A quota reset is detected when `remaining_fraction` rises (> 1e-9) or when
  the previous snapshot's `reset_time` is <= the later snapshot's `ts`. A reset
  discards the open segment. Rolling/drifting `reset_time` values that have not
  yet passed do not reset the segment.

  Confidence levels:
    * 'insufficient': fewer than min_pairs (default 3) completed segments.
    * 'high': >= max(5, min_pairs) samples, best CV < 0.25, and > 2x separation
      against the competing billing unit.
    * 'medium': >= min_pairs samples and best CV < 0.5.
    * 'low': >= min_pairs samples and best CV >= 0.5.
  """
  clean_target = (target or "").strip()
  effective_min = max(1, int(min_pairs))
  effective_min_drop = max(1e-12, float(min_drop))

  pairs: list[dict[str, float]] = []
  zero_drop_samples = 0
  zero_drop_calls = 0
  zero_drop_tokens = 0
  skipped_reset_pairs = 0
  resolved_qg: str | None = None
  all_mids: set[str] = set()
  all_enums: set[str] = set()

  seg_calls = 0
  seg_tokens_total = 0
  seg_tokens_uncached = 0
  seg_drop = 0.0

  for s1, s2 in zip(snapshots or [], (snapshots or [])[1:]):
    if not isinstance(s1, dict) or not isinstance(s2, dict):
      continue
    qg1, rf1, rt1, mids1, enums1 = _resolve_group_in_snapshot(s1, clean_target)
    qg2, rf2, rt2, mids2, enums2 = _resolve_group_in_snapshot(s2, clean_target)
    if rf1 is None or rf2 is None:
      seg_calls = 0
      seg_tokens_total = 0
      seg_tokens_uncached = 0
      seg_drop = 0.0
      continue
    if qg2 is not None:
      resolved_qg = qg2
    elif qg1 is not None and resolved_qg is None:
      resolved_qg = qg1
    all_mids.update(mids1 | mids2)
    group_enums = enums1 | enums2
    all_enums.update(group_enums)

    is_reset = False
    if rf2 - rf1 > 1e-9:
      is_reset = True
    elif rt1 is not None:
      rt1_dt = catalog._parse_iso_utc(rt1)
      ts2_dt = catalog._parse_iso_utc(str(s2.get("ts") or ""))
      if rt1_dt is not None and ts2_dt is not None and rt1_dt <= ts2_dt:
        is_reset = True

    if is_reset:
      skipped_reset_pairs += 1
      seg_calls = 0
      seg_tokens_total = 0
      seg_tokens_uncached = 0
      seg_drop = 0.0
      continue

    step_drop = max(0.0, rf1 - rf2)

    delta = snapshot_delta(s1, s2)
    pm = delta["per_model"]

    d_calls = sum(pm.get(e, {}).get("calls", 0) for e in group_enums)
    d_input = sum(pm.get(e, {}).get("input_tokens", 0) for e in group_enums)
    d_output = sum(pm.get(e, {}).get("output_tokens", 0) for e in group_enums)
    d_cache_read = sum(
        pm.get(e, {}).get("cache_read_tokens", 0) for e in group_enums
    )
    d_cache_write = sum(
        pm.get(e, {}).get("cache_write_tokens", 0) for e in group_enums
    )
    if any(
        x < 0 for x in (d_calls, d_input, d_output, d_cache_read, d_cache_write)
    ):
      seg_calls = 0
      seg_tokens_total = 0
      seg_tokens_uncached = 0
      seg_drop = 0.0
      continue

    d_tokens_total = d_input + d_output + d_cache_read + d_cache_write
    d_tokens_uncached = d_input + d_output + d_cache_write

    if (d_calls > 0 or d_tokens_total > 0) and step_drop <= 1e-12:
      zero_drop_samples += 1
      zero_drop_calls += d_calls
      zero_drop_tokens += d_tokens_total

    seg_calls += d_calls
    seg_tokens_total += d_tokens_total
    seg_tokens_uncached += d_tokens_uncached

    if seg_calls > 0 or seg_tokens_total > 0:
      seg_drop += step_drop
      if seg_drop >= effective_min_drop:
        pairs.append({
            "quota_drop": seg_drop,
            "calls": float(seg_calls),
            "tokens_total": float(seg_tokens_total),
            "tokens_uncached": float(seg_tokens_uncached),
        })
        seg_calls = 0
        seg_tokens_total = 0
        seg_tokens_uncached = 0
        seg_drop = 0.0
    else:
      seg_drop = 0.0

  pending_usage = {
      "calls": int(seg_calls),
      "tokens_total": int(seg_tokens_total),
      "tokens_uncached": int(seg_tokens_uncached),
  }

  fit = {
      "calls": _compute_measure_fit(pairs, "calls"),
      "tokens_total": _compute_measure_fit(pairs, "tokens_total"),
      "tokens_uncached": _compute_measure_fit(pairs, "tokens_uncached"),
  }

  samples = len(pairs)
  caveats: list[str] = [_UNTRACKED_NOISE_CAVEAT]
  if skipped_reset_pairs > 0:
    caveats.append(
        f"Skipped {skipped_reset_pairs} snapshot pair(s) across a quota reset"
        " or quota rise."
    )
  if zero_drop_samples > 0:
    caveats.append(
        f"Observed {zero_drop_samples} pair(s) with positive usage"
        f" ({zero_drop_calls} calls, {zero_drop_tokens} tokens) but zero"
        " measured quota drop (accumulated into lagged segments or"
        " pending_usage)."
    )

  calls_full = fit["calls"]["units_per_full_quota"]
  calls_per_pct = (
      round(float(calls_full) / 100.0, 2) if calls_full is not None else None
  )

  tok_total_cv = fit["tokens_total"]["cv"]
  tok_unc_cv = fit["tokens_uncached"]["cv"]
  if (
      tok_unc_cv is not None
      and (tok_total_cv is None or tok_unc_cv < tok_total_cv)
  ):
    best_tok_key = "tokens_uncached"
  else:
    best_tok_key = "tokens_total"
  tok_full = fit[best_tok_key]["units_per_full_quota"]
  tokens_per_pct = (
      round(float(tok_full) / 100.0, 2) if tok_full is not None else None
  )

  if samples < effective_min:
    caveats.append(
        f"Insufficient positive-drop sample pairs: have {samples}, need at"
        f" least {effective_min}."
    )
    return {
        "target": clean_target,
        "quota_group": resolved_qg,
        "models": sorted(all_mids),
        "enums": sorted(all_enums),
        "unit": None,
        "method": None,
        "confidence": "insufficient",
        "samples": samples,
        "pending_usage": pending_usage,
        "zero_drop_samples": zero_drop_samples,
        "skipped_reset_pairs": skipped_reset_pairs,
        "fit": fit,
        "calls_per_percent": calls_per_pct,
        "tokens_per_percent": tokens_per_pct,
        "caveats": caveats,
    }

  candidates: list[tuple[float, str]] = []
  for m_key in ("calls", "tokens_total", "tokens_uncached"):
    cv_val = fit[m_key]["cv"]
    pos_count = sum(1 for p in pairs if p.get(m_key, 0.0) > 0.0)
    if cv_val is not None and pos_count >= effective_min and pos_count == samples:
      candidates.append((float(cv_val), m_key))

  if not candidates:
    return {
        "target": clean_target,
        "quota_group": resolved_qg,
        "models": sorted(all_mids),
        "enums": sorted(all_enums),
        "unit": None,
        "method": None,
        "confidence": "insufficient",
        "samples": samples,
        "pending_usage": pending_usage,
        "zero_drop_samples": zero_drop_samples,
        "skipped_reset_pairs": skipped_reset_pairs,
        "fit": fit,
        "calls_per_percent": calls_per_pct,
        "tokens_per_percent": tokens_per_pct,
        "caveats": caveats,
    }

  candidates.sort(key=lambda item: item[0])
  best_cv, best_method = candidates[0]
  inferred_unit = (
      tc.UNIT_CALLS if best_method == "calls" else tc.UNIT_TOKENS
  )

  cv_calls = fit["calls"]["cv"]
  cv_tokens_list = [
      float(v)
      for v in (fit["tokens_total"]["cv"], fit["tokens_uncached"]["cv"])
      if v is not None
  ]
  cv_tokens_best = min(cv_tokens_list) if cv_tokens_list else None
  competing_cv = (
      cv_tokens_best if inferred_unit == tc.UNIT_CALLS else cv_calls
  )

  if (
      samples >= max(5, effective_min)
      and best_cv < 0.25
      and (competing_cv is None or float(competing_cv) > 2.0 * best_cv)
  ):
    confidence = "high"
  elif samples >= effective_min and best_cv < 0.5:
    confidence = "medium"
  else:
    confidence = "low"

  return {
      "target": clean_target,
      "quota_group": resolved_qg,
      "models": sorted(all_mids),
      "enums": sorted(all_enums),
      "unit": inferred_unit,
      "method": best_method,
      "confidence": confidence,
      "samples": samples,
      "pending_usage": pending_usage,
      "zero_drop_samples": zero_drop_samples,
      "skipped_reset_pairs": skipped_reset_pairs,
      "fit": fit,
      "calls_per_percent": calls_per_pct,
      "tokens_per_percent": tokens_per_pct,
      "caveats": caveats,
  }
