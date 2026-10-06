"""Model catalog discovery, caching, resolution, and picker display.

Interacts with the Antigravity language server or the CLI models command
to fetch available models and live quotas, caches them per application directory,
resolves queries (exact, label, or fuzzy), and renders picker tables.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from typing import Any

import tokenomics_config as tc


def _assign_quota_groups(models: list[dict[str, Any]]) -> None:
  """Assigns stable quota_group ids for pairs of identical (quota, reset_time)."""
  groups: dict[tuple[float, str], str] = {}
  counter = 1
  for m in models:
    rf = m.get("remaining_fraction")
    rt = m.get("reset_time")
    if rf is not None and rt is not None:
      key = (rf, rt)
      if key not in groups:
        groups[key] = f"quota-group-{counter}"
        counter += 1
      m["quota_group"] = groups[key]
    else:
      m["quota_group"] = None


def _parse_iso_utc(ts: str) -> datetime.datetime | None:
  """Parses an ISO 8601 UTC timestamp string."""
  if not ts or not isinstance(ts, str):
    return None
  try:
    clean = ts.replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(clean)
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)
  except (ValueError, TypeError):
    return None


def fetch_from_language_server(
    address: str, token: str, timeout: float = 8.0
) -> list[dict[str, Any]]:
  """Fetches clientModelConfigs from the local Antigravity Language Server."""
  clean_addr = address.strip()
  parsed = urllib.parse.urlsplit(f"http://{clean_addr}")
  host = parsed.hostname
  if host not in ("localhost", "127.0.0.1", "::1"):
    raise tc.ConfigError(
        f"Refusing to send credentials to non-local host {host!r} (address:"
        f" {clean_addr!r})"
    )

  url = (
      f"http://{clean_addr}/exa.language_server_pb.LanguageServerService/GetCascadeModelConfigData"
  )
  headers = {
      "Content-Type": "application/json",
      "Connect-Protocol-Version": "1",
      "x-codeium-csrf-token": token.strip(),
  }
  req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      raw = resp.read().decode("utf-8")
  except Exception as e:
    raise tc.ConfigError(f"Language server request failed: {e}") from e

  try:
    data = json.loads(raw)
  except ValueError as e:
    raise tc.ConfigError(f"Language server returned invalid JSON: {e}") from e

  client_configs = data.get("clientModelConfigs") or []
  models: list[dict[str, Any]] = []
  for item in client_configs:
    if not isinstance(item, dict):
      continue
    mid = item.get("modelId")
    if not mid or not isinstance(mid, str):
      continue
    mid = mid.strip()
    lbl = item.get("label") or mid
    moa = item.get("modelOrAlias") or {}
    enum = moa.get("model") if isinstance(moa, dict) else None
    recommended = bool(item.get("isRecommended", False))
    tag = item.get("tagTitle")
    quota_info = item.get("quotaInfo")

    if quota_info is not None and isinstance(quota_info, dict):
      rf_raw = quota_info.get("remainingFraction")
      rf = float(rf_raw) if rf_raw is not None else 0.0
      rt = quota_info.get("resetTime")
      if rt is not None and not isinstance(rt, str):
        rt = str(rt)
    else:
      rf = None
      rt = None

    models.append({
        "model_id": mid,
        "label": str(lbl).strip(),
        "enum": str(enum).strip() if enum else None,
        "remaining_fraction": rf,
        "reset_time": rt,
        "recommended": recommended,
        "tag": str(tag).strip() if tag else None,
    })

  _assign_quota_groups(models)
  return models


def fetch_from_cli_models(
    exe_path: str | None = None, timeout: float = 30.0
) -> list[dict[str, Any]]:
  """Fetches model ids and labels via `<cli> models` fallback."""
  cli = os.path.realpath(exe_path) if exe_path else "agy"
  try:
    proc = subprocess.run(
        [cli, "models"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
  except Exception as e:
    raise tc.ConfigError(f"Failed to execute CLI models command: {e}") from e

  if proc.returncode != 0:
    err_tail = proc.stderr.strip() or f"exit code {proc.returncode}"
    raise tc.ConfigError(f"CLI models command failed: {err_tail}")

  models: list[dict[str, Any]] = []
  for line in proc.stdout.splitlines():
    line = line.strip()
    if not line or line.startswith("Fetching available models"):
      continue
    if "\t" in line:
      parts = line.split("\t", 1)
      mid = parts[0].strip()
      lbl = parts[1].strip() if len(parts) > 1 and parts[1].strip() else mid
      if mid:
        models.append({
            "model_id": mid,
            "label": lbl,
            "enum": None,
            "remaining_fraction": None,
            "reset_time": None,
            "recommended": False,
            "tag": None,
            "quota_group": None,
        })

  if not models:
    raise tc.ConfigError("CLI models command returned no model entries")

  return models


def fetch_live_catalog(
    env: dict[str, str] | None = None, timeout: float = 8.0
) -> dict[str, Any]:
  """Fetches the live catalog from the Language Server or the CLI fallback."""
  effective_env = os.environ if env is None else env
  ls_addr = effective_env.get("ANTIGRAVITY_LS_ADDRESS")
  token = effective_env.get("ANTIGRAVITY_CSRF_TOKEN")
  cli_exe = effective_env.get("ANTIGRAVITY_AGENTAPI_EXE")

  models: list[dict[str, Any]] | None = None
  source: str | None = None
  ls_error: str | None = None

  if ls_addr and token:
    try:
      models = fetch_from_language_server(ls_addr, token, timeout=timeout)
      source = "language_server"
    except Exception as e:
      ls_error = str(e)

  if models is None:
    try:
      models = fetch_from_cli_models(cli_exe, timeout=30.0)
      source = "cli_models"
    except Exception as e:
      cli_error = str(e)
      msg = "cannot fetch live model catalog from language server or CLI models fallback."
      if ls_error:
        msg += f" (LS error: {ls_error})"
      msg += f" (CLI error: {cli_error})"
      raise tc.ConfigError(msg) from e

  now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
      "%Y-%m-%dT%H:%M:%SZ"
  )
  return {
      "fetched_at": now_iso,
      "source": source,
      "models": models,
  }


def catalog_cache_path(app_dir: str) -> str:
  """Returns the target cache file path for an app data directory."""
  return os.path.join(
      app_dir, "plugin_data", tc.PLUGIN_NAME, "model_catalog.json"
  )


def write_catalog(catalog: dict[str, Any], app_dir: str) -> str:
  """Atomically writes catalog data to the app directory cache."""
  target = catalog_cache_path(app_dir)
  os.makedirs(os.path.dirname(target), exist_ok=True)
  tmp = target + ".tmp"
  with io.open(tmp, "w", encoding="utf-8") as f:
    json.dump(catalog, f, indent=2)
    f.write("\n")
  os.replace(tmp, target)
  return target


def _read_catalog_file(path: str) -> dict[str, Any] | None:
  """Safely reads and validates a single catalog JSON file."""
  try:
    with io.open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("models"), list):
      return data
  except (OSError, ValueError):
    pass
  return None


def read_catalog(app_dir: str | None = None) -> dict[str, Any] | None:
  """Reads the catalog cache from app_dir, or the freshest across all app dirs."""
  if app_dir is not None:
    return _read_catalog_file(catalog_cache_path(app_dir))

  candidates: list[str] = []
  env_override = os.environ.get(tc.APP_DIR_ENV)
  if env_override:
    candidates.append(os.path.expanduser(env_override))

  pattern = os.path.expanduser("~/.gemini/*")
  for base in sorted(tc.glob.glob(pattern)):
    if os.path.isdir(base) and base not in candidates:
      candidates.append(base)

  newest_catalog: dict[str, Any] | None = None
  newest_time: datetime.datetime | None = None

  for candidate in candidates:
    target = catalog_cache_path(candidate)
    cat = _read_catalog_file(target)
    if not cat:
      continue
    ts = _parse_iso_utc(cat.get("fetched_at", ""))
    if ts is None:
      try:
        mtime = os.path.getmtime(target)
        ts = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
      except OSError:
        continue
    if newest_time is None or ts > newest_time:
      newest_time = ts
      newest_catalog = cat

  return newest_catalog


def catalog_age_seconds(catalog: dict[str, Any] | None) -> float | None:
  """Returns the age in seconds since fetched_at, or None."""
  if not catalog or not isinstance(catalog, dict):
    return None
  ts = _parse_iso_utc(catalog.get("fetched_at", ""))
  if ts is None:
    return None
  now = datetime.datetime.now(datetime.timezone.utc)
  diff = (now - ts).total_seconds()
  return max(0.0, diff)


def is_catalog_stale(
    catalog: dict[str, Any] | None, max_age_seconds: float = 900.0
) -> bool:
  """Returns True if catalog is missing, has no timestamp, or is older than max_age."""
  age = catalog_age_seconds(catalog)
  return age is None or age > max_age_seconds


def _normalize_models_list(
    models: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
  """Normalizes list or registry dict into a list of model dicts."""
  if isinstance(models, dict):
    model_list: list[dict[str, Any]] = []
    for k, v in models.items():
      lbl = (v.get("label") if isinstance(v, dict) else None) or k
      model_list.append({"model_id": k, "label": lbl})
    return model_list
  return list(models)


def resolve_query(
    query: str, models: list[dict[str, Any]] | dict[str, Any]
) -> str:
  """Resolves a model query to a model_id: exact id, exact label, or fuzzy match."""
  model_list = _normalize_models_list(models)
  q = (query or "").strip()
  if not q:
    raise tc.ConfigError("model query cannot be empty")

  # 1. Exact model_id match
  for m in model_list:
    if m.get("model_id") == q:
      return m["model_id"]

  # 2. Exact label match (case-insensitive)
  exact_labels = [
      m
      for m in model_list
      if (m.get("label") or "").strip().lower() == q.lower()
  ]
  if len(exact_labels) == 1:
    return exact_labels[0]["model_id"]
  if len(exact_labels) > 1:
    candidates = [
        f"{m.get('label') or m['model_id']} ({m['model_id']})"
        for m in exact_labels[:8]
    ]
    raise tc.ConfigError(
        f"ambiguous model label {query!r}; matches: {', '.join(candidates)}"
    )

  # 3. Fuzzy match: every token of query appears in normalized label or id
  tokens = re.findall(r"[A-Za-z0-9]+", q.lower())
  if not tokens:
    raise tc.ConfigError(f"model query {query!r} has no searchable tokens")

  fuzzy_matches: list[dict[str, Any]] = []
  for m in model_list:
    mid = m.get("model_id") or ""
    lbl = m.get("label") or mid
    target = f"{lbl} {mid}".lower()
    if all(tok in target for tok in tokens):
      fuzzy_matches.append(m)

  if len(fuzzy_matches) == 1:
    return fuzzy_matches[0]["model_id"]
  if len(fuzzy_matches) > 1:
    candidates = [
        f"{m.get('label') or m['model_id']} ({m['model_id']})"
        for m in fuzzy_matches[:8]
    ]
    raise tc.ConfigError(
        f"ambiguous model query {query!r}; matches {len(fuzzy_matches)} models:"
        f" {', '.join(candidates)}"
    )

  # 4. No match: list close candidates (matching any token)
  close_candidates: list[str] = []
  for m in model_list:
    mid = m.get("model_id") or ""
    lbl = m.get("label") or mid
    target = f"{lbl} {mid}".lower()
    if any(tok in target for tok in tokens):
      close_candidates.append(f"{lbl} ({mid})")

  if not close_candidates:
    close_candidates = [
        f"{m.get('label') or m['model_id']} ({m['model_id']})"
        for m in model_list[:8]
    ]

  hint = f": {', '.join(close_candidates[:8])}" if close_candidates else ""
  raise tc.ConfigError(f"no model matches query {query!r}. Did you mean{hint}?")


def _extract_current_markers(current: Any) -> dict[str, list[str]]:
  """Maps model_id to list of role markers."""
  result: dict[str, list[str]] = {}
  if not current:
    return result
  if isinstance(current, str):
    result.setdefault(current, []).append("current")
  elif isinstance(current, (list, tuple, set)):
    for item in current:
      if isinstance(item, str):
        result.setdefault(item, []).append("current")
  elif isinstance(current, dict):
    for role, val in current.items():
      mid = val.get("model_id") if isinstance(val, dict) else val
      if isinstance(mid, str) and mid:
        result.setdefault(mid, []).append(str(role))
  return result


def _format_local_reset_time(rt: str | None) -> str:
  """Formats an ISO reset time as HH:MM local time."""
  if not rt:
    return "-"
  parsed = _parse_iso_utc(rt)
  if parsed is None:
    return "-"
  local_dt = parsed.astimezone()
  return local_dt.strftime("%H:%M")


def picker_text(catalog: dict[str, Any] | None, current: Any = None) -> str:
  """Renders an aligned TUI model picker table."""
  models = (catalog or {}).get("models") or []
  if not models:
    return "No models available in catalog."

  current_map = _extract_current_markers(current)
  recommended = [m for m in models if m.get("recommended")]
  others = [m for m in models if not m.get("recommended")]
  ordered = recommended + others

  rows: list[tuple[str, str, str, str, str, str]] = []
  for idx, m in enumerate(ordered, 1):
    mid = m.get("model_id") or ""
    lbl = m.get("label") or mid
    tag = m.get("tag")
    if tag:
      lbl = f"{lbl} [{tag}]"
    rf = m.get("remaining_fraction")
    quota_str = f"{int(round(rf * 100))}%" if rf is not None else "-"
    reset_str = _format_local_reset_time(m.get("reset_time"))
    marker_parts = current_map.get(mid, [])
    marker_str = (
        f"* ({', '.join(marker_parts)})" if marker_parts else ""
    )
    rows.append((str(idx), lbl, mid, quota_str, reset_str, marker_str))

  headers = ("#", "Label", "Model ID", "Quota", "Reset", "Current")
  col_widths = [len(h) for h in headers]
  for row in rows:
    for i in range(len(headers)):
      col_widths[i] = max(col_widths[i], len(row[i]))

  header_line = (
      f"{headers[0]:>{col_widths[0]}}  "
      f"{headers[1]:<{col_widths[1]}}  "
      f"{headers[2]:<{col_widths[2]}}  "
      f"{headers[3]:>{col_widths[3]}}  "
      f"{headers[4]:<{col_widths[4]}}  "
      f"{headers[5]:<{col_widths[5]}}"
  ).rstrip()

  divider = (
      f"{'-'*col_widths[0]}  "
      f"{'-'*col_widths[1]}  "
      f"{'-'*col_widths[2]}  "
      f"{'-'*col_widths[3]}  "
      f"{'-'*col_widths[4]}  "
      f"{'-'*col_widths[5]}"
  ).rstrip()

  lines = [header_line, divider]
  for r in rows:
    line = (
        f"{r[0]:>{col_widths[0]}}  "
        f"{r[1]:<{col_widths[1]}}  "
        f"{r[2]:<{col_widths[2]}}  "
        f"{r[3]:>{col_widths[3]}}  "
        f"{r[4]:<{col_widths[4]}}  "
        f"{r[5]:<{col_widths[5]}}"
    ).rstrip()
    lines.append(line)

  return "\n".join(lines)
