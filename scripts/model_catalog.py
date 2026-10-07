#!/usr/bin/env python3
"""Fetches, caches, and displays the live Antigravity model catalog."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib")
)
import catalog
import tokenomics_config as tc
import usage


def main() -> int:
  parser = argparse.ArgumentParser(
      description=(
          "Fetches, caches, and displays the live Antigravity model catalog and"
          " conversation usage snapshot."
      )
  )
  parser.add_argument(
      "--json",
      action="store_true",
      help="Print the raw catalog JSON instead of the picker table.",
  )
  parser.add_argument(
      "--no-write",
      action="store_true",
      help="Skip writing the fetched catalog and usage to the disk cache.",
  )
  parser.add_argument(
      "--no-usage",
      action="store_true",
      help="Skip fetching conversation usage and recording a usage snapshot.",
  )
  parser.add_argument(
      "--conversation",
      action="append",
      default=[],
      metavar="ID",
      help="Additional conversation ID to track in the usage snapshot (repeatable).",
  )
  args = parser.parse_args()

  try:
    cat = catalog.fetch_live_catalog()
  except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    return 1

  cache_path: str | None = None
  usage_summary: str | None = None
  if not args.no_write:
    conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
    app_dir = None
    if conv_id:
      try:
        app_dir = tc.find_app_dir_for_conversation(conv_id)
      except (tc.ConfigError, OSError):
        app_dir = None
    if not app_dir:
      try:
        app_dir = tc.app_data_dir()
      except (tc.ConfigError, OSError) as e:
        print(f"Error: cannot determine app directory: {e}", file=sys.stderr)
        return 1
    try:
      cache_path = catalog.write_catalog(cat, app_dir)
    except (tc.ConfigError, OSError, ValueError) as e:
      print(f"Error: failed writing catalog cache: {e}", file=sys.stderr)
      return 1

    if not args.no_usage and (conv_id or args.conversation):
      try:
        snap_info = usage.take_usage_snapshot(
            app_dir=app_dir,
            catalog_data=cat,
            root_conversation_id=conv_id,
            extra_ids=args.conversation,
            env=os.environ,
            min_recheck_seconds=60.0,
        )
        n_convs = snap_info["tracked_conversations"]
        delta_calls = snap_info["calls_since_previous"]
        usage_summary = (
            f"Usage snapshot: {n_convs} tracked conversation(s),"
            f" +{delta_calls} call(s) since previous snapshot"
            " (usage.json, usage_snapshots.jsonl)"
        )
      except (tc.ConfigError, OSError, ValueError) as e:
        usage_summary = f"Usage snapshot skipped: {e}"

  if args.json:
    print(json.dumps(cat, indent=2))
  else:
    current: dict[str, str] = {}
    try:
      active = tc.load_active()
      current["orchestrator"] = active["roles"]["orchestrator"]["model_id"]
      for r in tc.SUBAGENT_ROLES:
        current[r] = active["roles"][r]["model_id"]
        fb = active["roles"][r].get("fallback")
        if fb:
          current[f"{r}_fallback"] = fb["model_id"]
    except (tc.ConfigError, OSError, ValueError):
      pass

    print(catalog.picker_text(cat, current=current))
    if cache_path:
      print(f"Cache written to {cache_path}")
    if usage_summary:
      print(usage_summary)

  return 0


if __name__ == "__main__":
  sys.exit(main())
