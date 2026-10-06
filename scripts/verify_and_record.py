#!/usr/bin/env python3
"""Verification Gate and Telemetry Recorder for tokenomics-worker.

Executes deterministic end-of-task verification:
1. Audits workspace diff for test tampering (deleted assertions, added
suppressions).
2. Executes specified test target/command and validates exit code 0.
3. Computes weighted token consumption and economic leverage.
4. Atomically records structured telemetry to the local journal.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import io
import json
import logging
import math
import os
import pathlib
import re
import subprocess
import sys
import tokenize
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [verify_and_record] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib")
)
import tokenomics_config as tc

TELEMETRY_JOURNAL: str | None = None
MAX_JOURNAL_BYTES = 8 * 1024 * 1024

DEFAULT_TOKEN_WEIGHTS = {
    "cached_input_read": 0.25,
    "uncached_input": 2.5,
    "cache_write": 3.125,
    "output_and_thinking": 12.5,
}


def get_telemetry_journal() -> str:
  if TELEMETRY_JOURNAL:
    return TELEMETRY_JOURNAL
  try:
    return os.path.join(tc.app_data_dir(), "telemetry", "tokenomics_events.jsonl")
  except tc.ConfigError:
    return os.path.expanduser("~/.gemini/antigravity/telemetry/tokenomics_events.jsonl")


def load_active_or_none() -> dict[str, Any] | None:
  """Returns the active tokenomics configuration, or None if unresolvable."""
  try:
    return tc.load_active()
  except tc.ConfigError as e:
    logging.warning("tokenomics config unavailable, using flags only: %s", e)
    return None

SUPPRESSION_PATTERNS = (
    r"@Suppress\b",
    r"//\s*NOLINT",
    r"#\s*pylint:\s*disable",
    r"//\s*eslint-disable",
    r"//\s*@ts-ignore",
    r"//\s*@ts-nocheck",
    r"//\s*@ts-expect-error\b",
    r"//\s*go:build\s*ignore",
    r"@unittest\.skip\b",
    r"@pytest\.mark\.skip\b",
    r"#\s*type:\s*ignore\b",
)

ASSERTION_PATTERNS = (
    r"\bassert\b",
    r"\bassertTrue\b",
    r"\bassertFalse\b",
    r"\bassertEqual\b",
    r"\bassertEquals\b",
    r"\bassertNotEqual\b",
    r"\bassert_called\b",
    r"\bself\.assert(?:In|NotIn|IsNone|IsNotNone|Raises|AlmostEqual)\b",
    r"\bassert\.NoError\b",
    r"\brequire\.(?:Equal|NoError|True|False)\b",
    r"\bpytest\.raises\b",
    r"\bexpect\(",
    r"\bt\.(?:Error|Errorf|Fatal|Fatalf)\b",
)


def sanitize_ident(value: str | None, max_len: int = 64) -> str:
  """Sanitizes alphanumeric identifier string."""
  if not value:
    return ""
  return re.sub(r"[^a-zA-Z0-9_\-.]", "", str(value).strip())[:max_len]


def sanitize_float(value: Any, default: float = 0.0) -> float:
  """Converts value to finite non-negative float, replacing NaN/Inf with default."""
  try:
    val = float(value)
    if math.isnan(val) or math.isinf(val) or val < 0.0:
      return default
    return val
  except (ValueError, TypeError):
    return default


def audit_test_tampering(
    workspace_dir: str,
    base_revision: str | None = None,
) -> dict[str, Any]:
  """Audits test file diffs in workspace for deleted assertions or suppressions."""
  if not os.path.isdir(workspace_dir):
    return {
        "tampering_detected": False,
        "error": f"Directory not found: {workspace_dir}",
        "violations": [],
    }

  diff_cmd: list[str] = []
  if os.path.exists(os.path.join(workspace_dir, ".hg")):
    rev = base_revision or "min(roots(branch(.)))"
    diff_cmd = ["hg", "diff", "-r", rev]
  else:
    rev = base_revision or "HEAD"
    diff_cmd = ["git", "diff", rev]

  try:
    result = subprocess.run(
        diff_cmd,
        cwd=workspace_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    diff_output = result.stdout
  except (OSError, subprocess.SubprocessError) as e:
    return {
        "tampering_detected": False,
        "error": f"Failed to execute diff command: {e}",
        "violations": [],
    }

  violations: list[dict[str, Any]] = []
  current_file = ""
  is_test_file = False

  for line in diff_output.splitlines():
    if (
        line.startswith("diff ")
        or line.startswith("--- ")
        or line.startswith("+++ ")
    ):
      match = re.search(r"[ab]/([^\s]+)", line)
      if match:
        current_file = match.group(1)
        is_test_file = bool(
            re.search(
                r"(_test\.(py|go|ts|js|cc|cpp)|Test\.(java|kt)|test_.*\.py)$",
                current_file,
            )
        )
      continue

    if not is_test_file:
      continue

    if line.startswith("-") and not line.startswith("---"):
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

    if line.startswith("+") and not line.startswith("+++"):
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

  return {
      "tampering_detected": len(violations) > 0,
      "violations_count": len(violations),
      "violations": violations,
  }


def execute_test_command(test_cmd: str, cwd: str) -> tuple[int, str]:
  """Executes specified test command and returns (exit_code, stdout_snippet)."""
  try:
    proc = subprocess.run(
        test_cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    combined = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, combined.strip()
  except subprocess.TimeoutExpired:
    return 124, "ERROR: Test execution timed out after 300 seconds."
  except (OSError, subprocess.SubprocessError) as e:
    return 1, f"ERROR: Failed to run test command: {e}"


def compute_weighted_tokens(
    cached_input_tokens: int,
    uncached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    weights: dict[str, float] | None = None,
) -> float:
  """Computes weighted token consumption from token counts."""
  w = {**DEFAULT_TOKEN_WEIGHTS, **(weights or {})}
  return round(
      cached_input_tokens * w["cached_input_read"]
      + uncached_input_tokens * w["uncached_input"]
      + cache_write_tokens * w["cache_write"]
      + (output_tokens + thinking_tokens) * w["output_and_thinking"],
      2,
  )


def record_event(
    plugin_name: str,
    conductor_model: str,
    task_tier: str,
    task_category: str,
    conductor_turns: int,
    subagents_spawned: int,
    subagent_calls_offloaded: int,
    cached_input_tokens: int,
    uncached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    weighted_tokens_consumed: int | float,
    cache_hit_rate: float,
    token_leverage: float,
    verification_outcome: str,
    redteam_findings_count: int,
    test_violations_count: int,
    duration_seconds: int | float,
    change_id: str | None,
    details: str | None,
    conversation_id: str | None = None,
    dry_run: bool = False,
    estimated_unoffloaded_tokens: int | float = 0.0,
    subagent_models: dict[str, str] | None = None,
) -> None:
  """Writes event to local journal."""
  if dry_run:
    logging.info("Dry run enabled: skipping journal write.")
    return

  clean_session = sanitize_ident(
      conversation_id or os.environ.get("CONVERSATION_ID", "standalone"),
      max_len=64,
  )
  clean_change = sanitize_ident(change_id or "", max_len=32)
  clean_turns = max(1, int(conductor_turns))
  clean_spawned = max(0, int(subagents_spawned))
  clean_offloaded = max(0, int(subagent_calls_offloaded))
  clean_cached_input = max(0, int(cached_input_tokens))
  clean_uncached_input = max(0, int(uncached_input_tokens))
  clean_cache_write = max(0, int(cache_write_tokens))
  clean_output = max(0, int(output_tokens))
  clean_thinking = max(0, int(thinking_tokens))
  clean_weighted_consumed = sanitize_float(weighted_tokens_consumed)
  clean_cache_hit_rate = round(sanitize_float(cache_hit_rate), 4)
  clean_token_leverage = round(sanitize_float(token_leverage), 2)
  clean_estimated_unoffloaded = sanitize_float(estimated_unoffloaded_tokens)
  clean_redteam = max(0, int(redteam_findings_count))
  clean_violations = max(0, int(test_violations_count))
  clean_duration = round(sanitize_float(duration_seconds), 2)

  now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
      "%Y-%m-%dT%H:%M:%SZ"
  )
  event = {
      "timestamp": now_iso,
      "session_id": clean_session,
      "plugin_name": plugin_name,
      "conductor_model": conductor_model,
      "subagent_models": subagent_models or {},
      "task_tier": task_tier,
      "task_category": task_category,
      "conductor_turns": clean_turns,
      "subagents_spawned": clean_spawned,
      "subagent_calls_offloaded": clean_offloaded,
      "cached_input_tokens": clean_cached_input,
      "uncached_input_tokens": clean_uncached_input,
      "cache_write_tokens": clean_cache_write,
      "output_tokens": clean_output,
      "thinking_tokens": clean_thinking,
      "weighted_tokens_consumed": clean_weighted_consumed,
      "cache_hit_rate": clean_cache_hit_rate,
      "token_leverage": clean_token_leverage,
      "estimated_unoffloaded_tokens": clean_estimated_unoffloaded,
      "verification_outcome": verification_outcome,
      "redteam_findings_count": clean_redteam,
      "test_violations_count": clean_violations,
      "duration_seconds": clean_duration,
      "change_id": clean_change,
      "details": details or "",
  }

  journal_path = get_telemetry_journal()
  try:
    parent = os.path.dirname(journal_path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.exists(journal_path):
      if os.path.getsize(journal_path) > MAX_JOURNAL_BYTES:
        rotated = f"{journal_path}.1"
        if os.path.exists(rotated):
          os.remove(rotated)
        os.rename(journal_path, rotated)
    with open(journal_path, "a", encoding="utf-8") as f:
      f.write(json.dumps(event, separators=(",", ":")) + "\n")
  except OSError as e:
    logging.warning("Failed to record event to local journal: %s", e)


def resolve_citation_path(
    rel_path: str, workspace_root: pathlib.Path
) -> pathlib.Path | None:
  """Resolves a cited path relative to the workspace root.

  Args:
    rel_path: The path as cited in a plan or report.
    workspace_root: The workspace directory to resolve against.

  Returns:
    The first existing candidate path, or None when none exists.
  """
  candidates = [
      workspace_root / rel_path.lstrip("/"),
      workspace_root.parent / rel_path.lstrip("/"),
  ]
  for candidate in candidates:
    if candidate.is_file():
      return candidate
  return None


def verify_citations(
    report_path: pathlib.Path, workspace_root: pathlib.Path
) -> list[str]:
  """Verifies every `path:line` / `path#L10-L20` citation exists and is in bounds."""
  errors: list[str] = []
  if not report_path.is_file():
    return [f"Report file missing: {report_path}"]
  try:
    text = report_path.read_text(encoding="utf-8", errors="replace")
  except OSError as exc:
    return [f"Failed reading report {report_path}: {exc}"]
  pattern = re.compile(
      r"([a-zA-Z0-9_\-./]+\.[a-zA-Z][a-zA-Z0-9]*)(?:#L?|:L?)(\d+)(?:-L?(\d+))?"
  )
  seen: set[tuple[str, str, str | None]] = set()
  for match in pattern.finditer(text):
    rel_path, start_s, end_s = match.groups()
    if rel_path.startswith("//") or "://" in rel_path:
      continue
    key = (rel_path, start_s, end_s)
    if key in seen:
      continue
    seen.add(key)
    clean_path = rel_path.lstrip("/")
    full_path = resolve_citation_path(rel_path, workspace_root)
    if full_path is None:
      errors.append(f"GHOST_CITATION: {clean_path} does not exist")
      continue
    try:
      lines = full_path.read_text(
          encoding="utf-8", errors="replace"
      ).splitlines()
      start_line = int(start_s)
      end_line = int(end_s) if end_s else start_line
      if start_line < 1 or end_line > len(lines) or start_line > end_line:
        errors.append(
            f"OUT_OF_BOUNDS: {clean_path}:L{start_line}-L{end_line} (total"
            f" lines: {len(lines)})"
        )
    except (OSError, ValueError) as exc:
      errors.append(f"ERROR_READING {clean_path}: {exc}")
  return errors


def check_python_readability_ast(file_path: pathlib.Path) -> list[str]:
  """Flags Python patterns that commonly fail code review, via the AST."""
  violations: list[str] = []
  if not file_path.is_file():
    return [f"{file_path}: File not found"]
  try:
    source = file_path.read_text(encoding="utf-8", errors="replace")
  except OSError as exc:
    return [f"{file_path}: Read error: {exc}"]

  try:
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok_type, tok_str, start, _, _ in tokens:
      if (
          tok_type == tokenize.COMMENT
          and "pylint:" in tok_str
          and "disable" in tok_str
      ):
        violations.append(
            f"{file_path.name}:L{start[0]}: Forbidden '# pylint: disable'"
            " comment suppression"
        )
  except tokenize.TokenError:
    pass

  try:
    tree = ast.parse(source, filename=str(file_path))
  except SyntaxError as exc:
    return [f"{file_path.name}: SyntaxError: {exc}"]

  allowed_dunders = {"__init__", "__str__", "__repr__"}
  forbidden_dynamic_attrs = {"getattr", "setattr", "hasattr"}
  scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
  reported_nested: set[tuple[int, str]] = set()

  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      if node.name.startswith("__") and node.name.endswith("__"):
        if node.name not in allowed_dunders:
          violations.append(
              f"{file_path.name}:L{node.lineno}: Forbidden dunder {node.name}"
          )
      for descendant in ast.walk(node):
        if descendant is node or not isinstance(descendant, scope_types):
          continue
        key = (descendant.lineno, descendant.name)
        if key in reported_nested:
          continue
        reported_nested.add(key)
        violations.append(
            f"{file_path.name}:L{descendant.lineno}: Nested function/class"
            f" {descendant.name} violates flat module scope"
        )
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
      if node.func.id in forbidden_dynamic_attrs:
        violations.append(
            f"{file_path.name}:L{node.lineno}: Forbidden dynamic attribute access"
            f" {node.func.id}()"
        )
  return violations


def recover_subagent_transcript(subagent_id: str) -> list[str]:
  """Extracts the last 10 steps of a crashed or unresponsive subagent."""
  clean_id = sanitize_ident(subagent_id)
  if not clean_id:
    return [f"Invalid subagent id: {subagent_id}"]
  try:
    app_dir = tc.app_data_dir()
  except tc.ConfigError as e:
    return [f"Cannot determine IDE app data directory: {e}"]
  transcript_path = (
      pathlib.Path(app_dir)
      / "brain"
      / clean_id
      / ".system_generated"
      / "logs"
      / "transcript.jsonl"
  )
  if not transcript_path.is_file():
    return [f"Transcript not found for subagent {subagent_id}"]
  try:
    lines = transcript_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
  except OSError as exc:
    return [f"Error reading transcript for {subagent_id}: {exc}"]
  summary: list[str] = []
  for raw in lines[-15:]:
    try:
      obj = json.loads(raw)
      step_idx = obj.get("step_index", "?")
      step_type = obj.get("type", "UNKNOWN")
      status = obj.get("status", "UNKNOWN")
      content = str(obj.get("content", "") or "")[:160].replace("\n", " ")
      if not content.strip():
        tool_calls = obj.get("tool_calls") or []
        names = [
            str(c.get("name", "unknown"))
            for c in tool_calls
            if isinstance(c, dict)
        ]
        if names:
          content = f"tool_calls: {', '.join(names)}"
      summary.append(f"Step {step_idx} [{step_type} / {status}]: {content}")
    except (ValueError, TypeError, KeyError):
      continue
  return summary


def build_parser() -> argparse.ArgumentParser:
  """Builds the CLI parser; kept separate so tests can validate documented flags."""
  parser = argparse.ArgumentParser(
      description="Verification Gate and Telemetry Recorder"
  )
  parser.add_argument(
      "--mode",
      default="verify_and_record",
      choices=[
          "verify_and_record",
          "verify-citations",
          "lint-readability",
          "recover-transcript",
      ],
      help="Verification or recovery mode to execute",
  )
  parser.add_argument(
      "--allowed-files",
      default="",
      help=(
          "Comma-separated list of assigned file paths for"
          " --mode=lint-readability"
      ),
  )
  parser.add_argument(
      "--report-path",
      default="",
      help="Markdown report path for --mode=verify-citations",
  )
  parser.add_argument(
      "--subagent-id",
      default="",
      help="Subagent conversation UUID for --mode=recover-transcript",
  )
  parser.add_argument(
      "--workspace_dir", default=".", help="Workspace root to audit"
  )
  parser.add_argument(
      "--test_cmd", default=None, help="Shell command to run unit tests"
  )
  parser.add_argument(
      "--conductor_model",
      "--orchestrator_model",
      dest="conductor_model",
      default=None,
      help="Orchestrator model (default: from tokenomics.config.json)",
  )
  parser.add_argument(
      "--plugin_name",
      default=tc.PLUGIN_NAME,
      help="Plugin invoking this gate",
  )
  parser.add_argument(
      "--task_tier",
      default="tier_1_surgical",
      choices=["tier_1_surgical", "tier_2_pipeline", "tier_3_competing"],
      help="Task intent tier",
  )
  parser.add_argument(
      "--task_category",
      default="refactor",
      choices=[
          "refactor",
          "feature",
          "bug_hunt",
          "codebase_crawl",
          "change_shepherd",
      ],
      help="Category of task",
  )
  parser.add_argument(
      "--conductor_turns",
      type=int,
      default=1,
      help="Conductor turn count",
  )
  parser.add_argument(
      "--subagents_spawned",
      type=int,
      default=1,
      help="Number of subagents launched",
  )
  parser.add_argument(
      "--subagent_calls_offloaded",
      type=int,
      default=10,
      help="Estimated calls offloaded to subagents",
  )
  parser.add_argument(
      "--redteam_findings",
      "--redteam_findings_count",
      dest="redteam_findings",
      type=int,
      default=0,
      help="Defects exposed by adversarial red-team audit",
  )
  parser.add_argument(
      "--duration_seconds",
      type=float,
      default=0.0,
      help="Elapsed task wall-clock seconds",
  )
  parser.add_argument(
      "--change_id",
      "--change_number",
      dest="change_id",
      default=None,
      help="Created or submitted change identifier",
  )
  parser.add_argument(
      "--details", default=None, help="Additional structured notes"
  )
  parser.add_argument(
      "--strict",
      action="store_true",
      help="Exit non-zero if test tampering violations are detected",
  )
  parser.add_argument(
      "--cached_input_tokens",
      type=int,
      default=0,
      help="Cached input tokens read from prompt cache",
  )
  parser.add_argument(
      "--uncached_input_tokens",
      type=int,
      default=0,
      help="Uncached input tokens read",
  )
  parser.add_argument(
      "--cache_write_tokens",
      type=int,
      default=0,
      help="Tokens written to prompt cache",
  )
  parser.add_argument(
      "--output_tokens",
      type=int,
      default=0,
      help="Generated output tokens",
  )
  parser.add_argument(
      "--thinking_tokens",
      type=int,
      default=0,
      help="Reasoning or thinking tokens",
  )
  parser.add_argument(
      "--weighted_tokens_consumed",
      type=float,
      default=0.0,
      help="Total weighted tokens consumed",
  )
  parser.add_argument(
      "--cache_hit_rate",
      type=float,
      default=0.0,
      help="Prompt cache hit percentage",
  )
  parser.add_argument(
      "--estimated_unoffloaded_tokens",
      type=float,
      default=0.0,
      help="Estimated weighted tokens if work had not been offloaded",
  )
  parser.add_argument(
      "--token_leverage",
      type=float,
      default=0.0,
      help="Ratio of unoffloaded to consumed weighted tokens",
  )
  parser.add_argument(
      "--conversation_id",
      default=None,
      help="Conversation UUID to associate with recorded telemetry",
  )
  parser.add_argument(
      "--dry_run",
      action="store_true",
      help="Skip journal write",
  )
  return parser


def main() -> int:
  args = build_parser().parse_args()
  workspace_path = pathlib.Path(args.workspace_dir).resolve()
  allowed_list = [f.strip() for f in args.allowed_files.split(",") if f.strip()]

  if args.mode == "verify-citations":
    if not args.report_path:
      print("ERROR: --report-path is required for --mode=verify-citations")
      return 2
    errors = verify_citations(pathlib.Path(args.report_path), workspace_path)
    if errors:
      print(f"[FAIL] Found {len(errors)} citation error(s):")
      for err in errors:
        print(f"  - {err}")
      return 1
    print("[PASS] All file and line citations verified.")
    return 0

  if args.mode == "lint-readability":
    if not allowed_list:
      print("ERROR: --allowed-files is required for --mode=lint-readability")
      return 2
    all_violations: list[str] = []
    for rel_f in allowed_list:
      if rel_f.endswith(".py"):
        resolved = resolve_citation_path(rel_f, workspace_path) or (
            workspace_path / rel_f
        )
        all_violations.extend(check_python_readability_ast(resolved))
    if all_violations:
      print(
          "[FAIL] Python readability heuristic check failed"
          f" ({len(all_violations)} violation(s)):"
      )
      for v in all_violations:
        print(f"  - {v}")
      return 1
    print("[PASS] Python readability heuristic check passed.")
    return 0

  if args.mode == "recover-transcript":
    if not args.subagent_id:
      print("ERROR: --subagent-id is required for --mode=recover-transcript")
      return 2
    steps = recover_subagent_transcript(args.subagent_id)
    print(f"[RECOVERY SUMMARY for {args.subagent_id}]:")
    for s in steps:
      print(f"  {s}")
    return 0

  print("=" * 70)
  print("TOKENOMICS-WORKER VERIFICATION GATE & TELEMETRY RECEIPT")
  print("=" * 70)

  tamper_result = audit_test_tampering(args.workspace_dir)
  violations_count = tamper_result.get("violations_count", 0)
  tampering_detected = tamper_result.get("tampering_detected", False)

  if tampering_detected:
    print(
        f"[WARNING] Test tampering detected: {violations_count} violation(s)."
    )
    for v in tamper_result.get("violations", []):
      print(f"  - {v['type']}: file={v['file']} pattern='{v['pattern']}'")
  else:
    print("[PASS] Test assertion integrity verified: zero tampering detected.")

  test_exit_code = 0
  test_output = ""
  if args.test_cmd:
    print(f"\n[RUNNING TEST] {args.test_cmd}")
    test_exit_code, test_output = execute_test_command(
        args.test_cmd, args.workspace_dir
    )
    if test_exit_code == 0:
      print("[PASS] Tests completed successfully (exit code 0).")
    else:
      print(f"[FAIL] Tests failed with exit code {test_exit_code}.")
      lines = test_output.splitlines()
      snippet = "\n".join(lines[-10:]) if len(lines) > 10 else test_output
      print(f"Output tail:\n{snippet}")

  if test_exit_code != 0:
    outcome = "FAILED"
  elif tampering_detected and args.strict:
    outcome = "TAMPERING_DETECTED"
  elif tampering_detected:
    outcome = "TAMPERING_WARNING"
  else:
    outcome = "PASSED"

  active = load_active_or_none()
  weights: dict[str, float] | None = None
  orchestrator_unit = "tokens"
  subagent_models: dict[str, str] = {}
  if active is not None:
    orch = active["roles"]["orchestrator"]
    if not args.conductor_model:
      args.conductor_model = orch["model_id"]
    orchestrator_unit = orch["billing"].get("unit", "unknown")
    weights = orch["billing"].get("token_weights")
    subagent_models = {
        role: active["roles"][role]["model_id"] for role in tc.SUBAGENT_ROLES
    }
  if not args.conductor_model:
    args.conductor_model = "unknown"
  if orchestrator_unit == "calls":
    logging.info(
        "Orchestrator %s is call-metered: token figures are informational only;"
        " conductor_turns is the quota-relevant number.",
        args.conductor_model,
    )

  cached_input_tokens = args.cached_input_tokens
  uncached_input_tokens = args.uncached_input_tokens
  cache_write_tokens = args.cache_write_tokens
  output_tokens = args.output_tokens
  thinking_tokens = args.thinking_tokens
  weighted_tokens_consumed = sanitize_float(args.weighted_tokens_consumed)
  cache_hit_rate = sanitize_float(args.cache_hit_rate)
  estimated_unoffloaded_tokens = sanitize_float(
      args.estimated_unoffloaded_tokens
  )

  if weighted_tokens_consumed == 0.0:
    computed = compute_weighted_tokens(
        cached_input_tokens=cached_input_tokens,
        uncached_input_tokens=uncached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        weights=weights,
    )
    if computed > 0.0:
      weighted_tokens_consumed = computed

  if cache_hit_rate == 0.0:
    total_input = cached_input_tokens + uncached_input_tokens
    if total_input > 0:
      cache_hit_rate = round(cached_input_tokens / total_input * 100.0, 1)

  if args.token_leverage:
    token_leverage = round(sanitize_float(args.token_leverage), 2)
  elif weighted_tokens_consumed == 0.0:
    token_leverage = 0.0
  else:
    token_leverage = round(
        estimated_unoffloaded_tokens / max(1.0, weighted_tokens_consumed), 2
    )

  journal_path = get_telemetry_journal()
  print("\n" + "-" * 70)
  print("TELEMETRY SUMMARY:")
  print(f"  Plugin:           {args.plugin_name}")
  print(f"  Orchestrator:     {args.conductor_model} ({orchestrator_unit})")
  print(
      "  Subagents:        "
      + (", ".join(f"{r}={m}" for r, m in subagent_models.items()) or "n/a")
  )
  print(f"  Task Tier:        {args.task_tier}")
  print(f"  Task Category:    {args.task_category}")
  print(f"  Weighted Tokens:  {weighted_tokens_consumed}")
  print(f"  Token Leverage:   {token_leverage}x")
  print(f"  Outcome:          {outcome}")
  print(f"  Red-Team Defects: {args.redteam_findings}")
  print(f"  Change ID:        {args.change_id or 'N/A'}")
  print(f"  Journal:          {journal_path}")
  print("-" * 70)

  record_event(
      plugin_name=args.plugin_name,
      conductor_model=args.conductor_model,
      task_tier=args.task_tier,
      task_category=args.task_category,
      conductor_turns=args.conductor_turns,
      subagents_spawned=args.subagents_spawned,
      subagent_calls_offloaded=args.subagent_calls_offloaded,
      cached_input_tokens=cached_input_tokens,
      uncached_input_tokens=uncached_input_tokens,
      cache_write_tokens=cache_write_tokens,
      output_tokens=output_tokens,
      thinking_tokens=thinking_tokens,
      weighted_tokens_consumed=weighted_tokens_consumed,
      cache_hit_rate=cache_hit_rate,
      token_leverage=token_leverage,
      verification_outcome=outcome,
      redteam_findings_count=args.redteam_findings,
      test_violations_count=violations_count,
      duration_seconds=args.duration_seconds,
      change_id=args.change_id,
      details=args.details,
      conversation_id=args.conversation_id,
      dry_run=args.dry_run,
      estimated_unoffloaded_tokens=estimated_unoffloaded_tokens,
      subagent_models=subagent_models,
  )

  if outcome == "FAILED" or (outcome == "TAMPERING_DETECTED" and args.strict):
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
