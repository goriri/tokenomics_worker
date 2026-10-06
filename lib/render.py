"""Renders pinned agent.json files from templates and the active config.

Used by the MCP server's set_models tool, so model choice happens from inside
the IDE: the agent calls the tool and the pins are written for it. There is no
separate script to run.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
from typing import Any

import tokenomics_config as tc

TEMPLATES_DIR = os.path.join(tc.PLUGIN_ROOT, "templates", "agents")
AGENTS_DIR = os.path.join(tc.PLUGIN_ROOT, "agents")
SESSION_MANIFEST = "tokenomics.session.json"

ROLE_BLURBS = {
    "scout": "read-only investigator (crawls, blame mining, symbol checks)",
    "worker": "implementer (plans, builds, tests, review fixes, change shepherd)",
    "redteam": "single-turn fresh-context reviewer-fixer",
}
_CONV_ID_RE = re.compile(r"[A-Za-z0-9_\-]+")


def ephemeral_agent_name(role: str, model_id: str) -> str:
  """Returns a conversation-scoped TypeName that cannot shadow the plugin's."""
  slug = re.sub(r"[^A-Za-z0-9]+", "_", model_id).strip("_").lower()
  return f"{tc.AGENT_NAMES[role]}_{slug}"


def billing_text(billing: dict[str, Any]) -> str:
  unit = billing.get("unit", "unknown")
  if unit == "unknown":
    return "unknown"
  limit = billing.get("daily_limit")
  limit_text = f"{int(limit):,} {unit}/day" if limit else "limit unknown"
  return f"{unit}, {billing.get('bucket', '?')}, {limit_text}"


def _substitute(node: Any, values: dict[str, str]) -> Any:
  """Replaces {{KEY}} in every string of a parsed JSON tree."""
  if isinstance(node, str):
    for key, value in values.items():
      node = node.replace("{{" + key + "}}", value)
    return node
  if isinstance(node, list):
    return [_substitute(item, values) for item in node]
  if isinstance(node, dict):
    return {k: _substitute(v, values) for k, v in node.items()}
  return node


def _load_template(role: str) -> dict[str, Any]:
  with io.open(os.path.join(TEMPLATES_DIR, f"{role}.json"), encoding="utf-8") as f:
    return json.load(f)


def subagent_table(
    roles: dict[str, Any],
    names: dict[str, str],
    fallback_names: dict[str, str] | None = None,
) -> str:
  lines = []
  for r in tc.SUBAGENT_ROLES:
    line = (
        f"   - {names[r]}: {ROLE_BLURBS[r]} -> {roles[r]['label']}"
        f" ({roles[r]['model_id']})"
    )
    fb = roles[r].get("fallback")
    if fb:
      fb_name = (fallback_names or {}).get(r) or f"{names[r]}_fallback"
      line += f" | fallback: {fb_name} -> {fb['label']} ({fb['model_id']})"
    lines.append(line)
  return "\n".join(lines)


def findings_text(economics: dict[str, Any], indent: str = "   ") -> str:
  return "\n".join(
      f"{indent}- [{f['level']}] {f['role']}: {f['message']}"
      for f in economics["findings"]
  )


def render_agent(
    role: str,
    active: dict[str, Any],
    names: dict[str, str],
    is_fallback: bool = False,
    fallback_names: dict[str, str] | None = None,
) -> dict[str, Any]:
  """Renders one agent.json from its template and the active config."""
  info = (
      active["roles"][role]["fallback"]
      if is_fallback
      else active["roles"][role]
  )
  economics = active["economics"]
  agent_name = (
      (fallback_names or {}).get(role, f"{names[role]}_fallback")
      if is_fallback
      else names[role]
  )
  values = {
      "AGENT_NAME": agent_name,
      "MODEL_ID": info["model_id"],
      "MODEL_LABEL": info["label"],
      "ENUM": info["enum"] or "",
      "PLUGIN_DIR": tc.plugin_dir_for_prompts(),
      "SUBAGENT_TABLE": subagent_table(active["roles"], names, fallback_names),
      "ORCH_DOCTRINE": economics["orchestrator"]["summary"],
      "MODE": economics["mode"],
      "FINDINGS": findings_text(economics),
  }
  rendered = _substitute(copy.deepcopy(_load_template(role)), values)
  if role != "orchestrator" and not info["enum"]:
    # No known enum: ship the agent unpinned rather than with an empty pin,
    # which would silently pin it to the IDE's default model.
    rendered.pop("disable_model_selection", None)
    rendered.get("config", {}).pop("cascade_config", None)
    rendered["description"] = (
        rendered["description"].replace(" pinned to ", " intended for ")
        + " NOT PINNED YET: run learn_model and set_models."
    )
  leftover = re.findall(r"\{\{[A-Z_]+\}\}", json.dumps(rendered))
  if leftover:
    raise tc.ConfigError(f"template {role}.json has unknown placeholders {leftover}")
  return rendered


def dump_json(data: dict[str, Any]) -> str:
  return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_file(path: str, content: str) -> None:
  """Atomic write; removes a shadowing agent.md beside an agent.json."""
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = path + ".tmp"
  with io.open(tmp, "w", encoding="utf-8") as f:
    f.write(content)
  os.replace(tmp, path)
  if path.endswith("agent.json"):
    stale = os.path.join(os.path.dirname(path), "agent.md")
    if os.path.exists(stale):
      os.remove(stale)


def planned_writes(
    active: dict[str, Any], agents_dir: str | None = None
) -> dict[str, str]:
  """Returns path -> content for every persistent generated agent.json."""
  agents_dir = agents_dir or AGENTS_DIR
  writes = {}
  for role in tc.ALL_ROLES:
    path = os.path.join(agents_dir, tc.AGENT_NAMES[role], "agent.json")
    writes[path] = dump_json(render_agent(role, active, tc.AGENT_NAMES))
  for role in tc.SUBAGENT_ROLES:
    if active["roles"][role].get("fallback"):
      fallback_agent_dir = f"{tc.AGENT_NAMES[role]}_fallback"
      path = os.path.join(agents_dir, fallback_agent_dir, "agent.json")
      writes[path] = dump_json(
          render_agent(role, active, tc.AGENT_NAMES, is_fallback=True)
      )
  return writes


def cleanup_fallback_agents(
    agents_dir: str | None = None, active: dict[str, Any] | None = None
) -> list[str]:
  """Removes agents/tw_*_fallback directories whose role no longer has a fallback."""
  agents_dir = agents_dir or AGENTS_DIR
  if not os.path.isdir(agents_dir):
    return []
  active = active or tc.load_active()
  active_fallback_dirs = set()
  for role in tc.SUBAGENT_ROLES:
    if active["roles"][role].get("fallback"):
      active_fallback_dirs.add(f"{tc.AGENT_NAMES[role]}_fallback")

  removed = []
  for entry in os.listdir(agents_dir):
    if entry.startswith("tw_") and entry.endswith("_fallback"):
      if entry not in active_fallback_dirs:
        dir_path = os.path.join(agents_dir, entry)
        if os.path.isdir(dir_path):
          shutil.rmtree(dir_path)
          removed.append(dir_path)
  return removed


def stale_files(active: dict[str, Any], **kwargs: str | None) -> list[str]:
  stale = []
  for path, content in planned_writes(active, **kwargs).items():
    try:
      with io.open(path, encoding="utf-8") as f:
        if f.read() == content:
          continue
    except OSError:
      pass
    stale.append(path)
  return stale


def _conv_dir(conversation_id: str, brain_root: str | None) -> str:
  if not conversation_id or not _CONV_ID_RE.fullmatch(conversation_id):
    raise tc.ConfigError(f"invalid conversation id {conversation_id!r}")
  if brain_root is None:
    app_dir = tc.find_app_dir_for_conversation(conversation_id) or tc.app_data_dir()
    brain_root = os.path.join(app_dir, "brain")
  return os.path.join(brain_root, conversation_id)


def install_session(
    active: dict[str, Any], conversation_id: str, brain_root: str | None = None
) -> dict[str, Any]:
  """Writes subagent pins into one live conversation; returns the manifest."""
  unpinned = [r for r in tc.SUBAGENT_ROLES if not active["roles"][r].get("enum")]
  if unpinned:
    raise tc.ConfigError(f"no enum known for roles {unpinned}; run learn_model")

  for role in tc.SUBAGENT_ROLES:
    fb = active["roles"][role].get("fallback")
    if fb and not fb.get("enum"):
      raise tc.ConfigError(
          f"no enum known for fallback role {role} ({fb['model_id']}); run learn_model"
      )

  conv_dir = _conv_dir(conversation_id, brain_root)
  agents_root = os.path.join(conv_dir, ".agents", "agents")
  names = dict(tc.AGENT_NAMES)
  fallback_names: dict[str, str] = {}
  for role in tc.SUBAGENT_ROLES:
    names[role] = ephemeral_agent_name(role, active["roles"][role]["model_id"])
    fb = active["roles"][role].get("fallback")
    if fb:
      fallback_names[role] = ephemeral_agent_name(role, fb["model_id"])

  for role in tc.SUBAGENT_ROLES:
    write_file(
        os.path.join(agents_root, names[role], "agent.json"),
        dump_json(
            render_agent(
                role,
                active,
                names,
                is_fallback=False,
                fallback_names=fallback_names,
            )
        ),
    )
    if role in fallback_names:
      write_file(
          os.path.join(agents_root, fallback_names[role], "agent.json"),
          dump_json(
              render_agent(
                  role,
                  active,
                  names,
                  is_fallback=True,
                  fallback_names=fallback_names,
              )
          ),
      )

  orch_fb = active["roles"]["orchestrator"].get("fallback")
  orch_fb_id = orch_fb["model_id"] if orch_fb else None

  subagent_manifest = {}
  for role in tc.SUBAGENT_ROLES:
    sub_dict: dict[str, Any] = {
        "type_name": names[role],
        "model_id": active["roles"][role]["model_id"],
        "enum": active["roles"][role]["enum"],
    }
    if role in fallback_names:
      sub_dict["fallback"] = {
          "type_name": fallback_names[role],
          "model_id": active["roles"][role]["fallback"]["model_id"],
          "enum": active["roles"][role]["fallback"]["enum"],
      }
    else:
      sub_dict["fallback"] = None
    subagent_manifest[role] = sub_dict

  manifest = {
      "conversation_id": conversation_id,
      "orchestrator": active["roles"]["orchestrator"]["model_id"],
      "orchestrator_fallback": orch_fb_id,
      "subagents": subagent_manifest,
      "pairing_verdict": active["economics"]["mode"],
      "findings": active["economics"]["findings"],
  }
  write_file(os.path.join(conv_dir, SESSION_MANIFEST), dump_json(manifest))
  return manifest


def load_session(
    conversation_id: str, brain_root: str | None = None
) -> dict[str, Any] | None:
  """Returns a conversation's session manifest, or None."""
  try:
    path = os.path.join(_conv_dir(conversation_id, brain_root), SESSION_MANIFEST)
    with io.open(path, encoding="utf-8") as f:
      return json.load(f)
  except (OSError, ValueError, tc.ConfigError):
    return None
