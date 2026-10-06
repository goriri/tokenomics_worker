"""Tests for tokenomics-worker: config resolution, economics, rendering, MCP.

Run from the plugin root:  python3 -m unittest discover -s tests -v

Every test runs against a private fixture registry with made-up enums and a
private app data directory, so nothing here depends on the machine's IDE
state or on any real model enum.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
for sub in ("lib", "scripts", "mcp"):
  sys.path.insert(0, os.path.join(ROOT, sub))

import render  # noqa: E402
import tokenomics_config as tc  # noqa: E402
import tokenomics_tools_server as srv  # noqa: E402
import verify_and_record  # noqa: E402

FIXTURE_REGISTRY = {
    "models": {
        "big-tokens": {
            "enum": "MODEL_FAKE_BIG_TOKENS",
            "label": "Big model, token-metered",
            "billing": {
                "unit": "tokens", "bucket": "T", "daily_limit": 1000000,
                "token_weights": {"output_and_thinking": 12.5},
                "cache_ttl_seconds": 300,
            },
        },
        "big-calls": {
            "enum": "MODEL_FAKE_BIG_CALLS",
            "label": "Big model, call-metered",
            "billing": {"unit": "calls", "bucket": "C0", "daily_limit": 100},
        },
        "fast-high": {
            "enum": "MODEL_FAKE_FAST_HIGH",
            "label": "Fast model, high",
            "billing": {"unit": "calls", "bucket": "C", "daily_limit": 1000},
        },
        "fast-medium": {
            "enum": "MODEL_FAKE_FAST_MEDIUM",
            "label": "Fast model, medium",
            "billing": {"unit": "calls", "bucket": "C", "daily_limit": 1000},
        },
        "fast-low": {
            "enum": "MODEL_FAKE_FAST_LOW",
            "label": "Fast model, low",
            "billing": {"unit": "calls", "bucket": "C", "daily_limit": 1000},
        },
        "named-only": {"label": "Known name, no enum"},
    }
}


def _model(model_id, unit, bucket=None, alias=False, weights=None, ttl=None,
           enum="MODEL_X"):
  billing = {"unit": unit}
  if bucket:
    billing["bucket"] = bucket
    billing["daily_limit"] = 1000
  if weights:
    billing["token_weights"] = weights
  if ttl:
    billing["cache_ttl_seconds"] = ttl
  return {
      "model_id": model_id,
      "enum": enum,
      "label": model_id,
      "billing": billing,
      "alias": alias,
  }


def _roles(orch, sub):
  return {"orchestrator": orch, "scout": sub, "worker": sub, "redteam": sub}


class FixtureMixin:
  """Private config, fixture registry and app data dir for each test."""

  def make_config(self, **overrides):
    data = {
        "orchestrator": "big-tokens",
        "subagent": "fast-high",
        "role_overrides": {"scout": None, "worker": None, "redteam": None},
        "custom_models": {},
    }
    data.update(overrides)
    self.tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.tmp, True)
    self.cfg_path = os.path.join(self.tmp, "tokenomics.config.json")
    with open(self.cfg_path, "w", encoding="utf-8") as f:
      json.dump(data, f)
    registry = os.path.join(self.tmp, "models.json")
    with open(registry, "w", encoding="utf-8") as f:
      json.dump(FIXTURE_REGISTRY, f)
    self.app_dir = os.path.join(self.tmp, "app")
    for d in ("brain", "conversations"):
      os.makedirs(os.path.join(self.app_dir, d))
    for p in (
        mock.patch.object(tc, "CONFIG_PATH", self.cfg_path),
        mock.patch.object(tc, "REGISTRY_PATH", registry),
        mock.patch.dict(os.environ, {tc.APP_DIR_ENV: self.app_dir}),
    ):
      p.start()
      self.addCleanup(p.stop)
    return self.cfg_path

  def make_conversation(self, conv_id, enum):
    conn = sqlite3.connect(
        os.path.join(self.app_dir, "conversations", f"{conv_id}.db")
    )
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    value = enum.encode()
    # Prompt text mentioning another enum must not confuse the reader.
    noise = b"\n\x04text\x12\x14MODEL_FAKE_DECOY_ENUM"
    field = b"\n\nmodel_enum\x12" + bytes([len(value)]) + value
    conn.execute("INSERT INTO gen_metadata VALUES (0, ?, 0)", (noise + field,))
    conn.commit()
    conn.close()

  def call(self, name, args=None):
    res = srv.handle_json_rpc_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    })["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


class LoadConfigTest(FixtureMixin, unittest.TestCase):

  def test_valid_config_loads(self):
    cfg = tc.load_config(self.make_config())
    self.assertEqual(cfg["subagent"], "fast-high")
    self.assertIsNone(cfg["role_overrides"]["scout"])

  def test_missing_orchestrator_rejected(self):
    with self.assertRaisesRegex(tc.ConfigError, "orchestrator"):
      tc.load_config(self.make_config(orchestrator=""))

  def test_unknown_role_override_rejected(self):
    with self.assertRaisesRegex(tc.ConfigError, "unknown role_overrides"):
      tc.load_config(self.make_config(role_overrides={"judge": "x"}))

  def test_bad_custom_enum_rejected(self):
    with self.assertRaisesRegex(tc.ConfigError, "MODEL_"):
      tc.load_config(self.make_config(custom_models={"m": {"enum": "M1"}}))

  def test_bad_billing_unit_rejected(self):
    with self.assertRaisesRegex(tc.ConfigError, "billing.unit"):
      tc.load_config(self.make_config(
          custom_models={"m": {"billing": {"unit": "bogus"}}}
      ))

  def test_invalid_json_reported(self):
    path = self.make_config()
    with open(path, "w") as f:
      f.write("{nope")
    with self.assertRaisesRegex(tc.ConfigError, "not valid JSON"):
      tc.load_config(path)

  def test_falls_back_to_shipped_defaults(self):
    self.make_config()
    os.remove(self.cfg_path)
    cfg = tc.load_config()
    with open(tc.DEFAULT_CONFIG_PATH) as f:
      self.assertEqual(cfg["subagent"], json.load(f)["subagent"])


class AppDataDirTest(unittest.TestCase):

  def test_env_override_wins(self):
    with mock.patch.dict(os.environ, {tc.APP_DIR_ENV: "/some/where"}):
      self.assertEqual(tc.app_data_dir(), "/some/where")

  def test_detects_most_recent_ide_folder(self):
    home = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, home, True)
    for name, mtime in (("old-ide", 1000), ("new-ide", 2000), ("config", 3000)):
      base = os.path.join(home, ".gemini", name)
      os.makedirs(os.path.join(base, "conversations"))
      if name != "config":  # No brain/: not an IDE folder.
        os.makedirs(os.path.join(base, "brain"))
      os.utime(os.path.join(base, "conversations"), (mtime, mtime))
    env = {"HOME": home}
    with mock.patch.dict(os.environ, env):
      os.environ.pop(tc.APP_DIR_ENV, None)
      self.assertEqual(
          tc.app_data_dir(), os.path.join(home, ".gemini", "new-ide")
      )

  def test_missing_dir_is_a_config_error(self):
    home = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, home, True)
    with mock.patch.dict(os.environ, {"HOME": home}):
      os.environ.pop(tc.APP_DIR_ENV, None)
      with self.assertRaisesRegex(tc.ConfigError, tc.APP_DIR_ENV):
        tc.app_data_dir()


class RegistryTest(FixtureMixin, unittest.TestCase):

  def test_registry_resolves_fixture(self):
    self.make_config()
    reg = tc.load_registry()
    self.assertEqual(reg["fast-high"]["enum"], "MODEL_FAKE_FAST_HIGH")
    self.assertEqual(reg["big-tokens"]["billing"]["unit"], "tokens")

  def test_custom_models_win(self):
    cfg = tc.load_config(self.make_config(custom_models={
        "fast-high": {"enum": "MODEL_CUSTOM_1"},
        "mine": {
            "enum": "MODEL_CUSTOM_2",
            "billing": {"unit": "calls", "bucket": "B", "daily_limit": 5},
        },
    }))
    reg = tc.load_registry(cfg)
    self.assertEqual(reg["fast-high"]["enum"], "MODEL_CUSTOM_1")
    self.assertEqual(reg["fast-high"]["enum_source"], "custom_models")
    self.assertEqual(reg["mine"]["billing"]["bucket"], "B")

  def test_billing_like_keeps_assumed_note_alongside_custom_notes(self):
    cfg = tc.load_config(self.make_config(custom_models={
        "new-one": {"enum": "MODEL_N", "billing_like": "fast-high",
                    "notes": "Learned yesterday."},
    }))
    info = tc.load_registry(cfg)["new-one"]
    self.assertEqual(info["billing"]["bucket"], "C")
    self.assertIn("Learned yesterday.", info["notes"])
    self.assertIn("assumed", info["notes"])

  def test_unknown_model_points_at_learn_model(self):
    self.make_config()
    reg = tc.load_registry()
    with self.assertRaises(tc.ConfigError) as ctx:
      tc.resolve_model("fast-ultra", reg)
    self.assertIn("fast-high", str(ctx.exception))
    self.assertIn("learn_model", str(ctx.exception))

  def test_resolve_roles_applies_overrides_and_collects_errors(self):
    cfg = tc.load_config(self.make_config(
        role_overrides={"scout": "fast-low", "worker": "nope-1"}
    ))
    reg = tc.load_registry(cfg)
    with self.assertRaisesRegex(tc.ConfigError, "worker"):
      tc.resolve_roles(cfg, reg)
    cfg["role_overrides"]["worker"] = None
    roles = tc.resolve_roles(cfg, reg)
    self.assertEqual(roles["scout"]["model_id"], "fast-low")
    self.assertEqual(roles["worker"]["model_id"], "fast-high")

  def test_unpinned_defaults_load_but_are_flagged(self):
    cfg = tc.load_config(self.make_config(subagent="named-only"))
    reg = tc.load_registry(cfg)
    with self.assertRaises(tc.ConfigError):
      tc.build_active(cfg, reg)
    active = tc.build_active(cfg, reg, allow_unpinned=True)
    self.assertEqual(active["economics"]["mode"], "counterproductive")
    self.assertTrue(any("not pinned" in f["message"]
                        for f in active["economics"]["findings"]))


class EconomicsTest(unittest.TestCase):

  def assess(self, orch, sub):
    return tc.assess_economics(_roles(orch, sub))

  def test_tokens_to_calls_is_offload(self):
    e = self.assess(_model("o", "tokens", "T"), _model("s", "calls", "C"))
    self.assertEqual(e["mode"], "offload")

  def test_same_model_is_counterproductive(self):
    m = _model("o", "tokens", "T")
    e = self.assess(m, dict(m))
    self.assertEqual(e["mode"], "counterproductive")
    self.assertIn("inherit", e["findings"][0]["message"])

  def test_shared_bucket_is_partial(self):
    e = self.assess(_model("o", "calls", "B"), _model("s", "calls", "B"))
    self.assertEqual(e["mode"], "partial")
    self.assertIn("shares quota bucket", e["findings"][0]["message"])

  def test_calls_to_tokens_is_inverted(self):
    e = self.assess(_model("o", "calls", "C"), _model("s", "tokens", "T"))
    self.assertEqual(e["mode"], "counterproductive")
    self.assertIn("Inverted", e["findings"][0]["message"])

  def test_calls_to_calls_different_buckets_is_offload(self):
    e = self.assess(_model("o", "calls", "C1"), _model("s", "calls", "C2"))
    self.assertEqual(e["mode"], "offload")

  def test_tokens_to_tokens_is_partial(self):
    e = self.assess(_model("o", "tokens", "T1"), _model("s", "tokens", "T2"))
    self.assertEqual(e["mode"], "partial")

  def test_unknown_billing_warns(self):
    e = self.assess(_model("o", "tokens", "T"), _model("s", "unknown"))
    self.assertEqual(e["mode"], "partial")

  def test_unpinned_subagent_is_risk(self):
    e = self.assess(_model("o", "tokens", "T"),
                    _model("s", "calls", "C", enum=None))
    self.assertEqual(e["mode"], "counterproductive")

  def test_alias_warns_even_when_offload(self):
    e = self.assess(
        _model("o", "tokens", "T"), _model("s", "calls", "C", alias=True)
    )
    self.assertEqual(e["mode"], "partial")
    self.assertTrue(any("alias" in f["message"] for f in e["findings"]))

  def test_doctrine_flips_with_billing_unit(self):
    calls = tc.orchestrator_doctrine(_model("o", "calls", "C"))
    self.assertEqual(calls["unit"], "calls")
    self.assertFalse(calls["cache_discipline"])
    self.assertIn("Turn count IS the bill", calls["summary"])
    tokens = tc.orchestrator_doctrine(
        _model("o", "tokens", "T", weights={"output_and_thinking": 12.5},
               ttl=300)
    )
    self.assertEqual(tokens["unit"], "tokens")
    self.assertTrue(tokens["cache_discipline"])
    self.assertIn("safe margin 240s", tokens["summary"])


class RenderTest(FixtureMixin, unittest.TestCase):

  def test_unpinned_role_renders_without_pin(self):
    cfg = tc.load_config(self.make_config(subagent="named-only"))
    active = tc.build_active(cfg, tc.load_registry(cfg), allow_unpinned=True)
    agent = render.render_agent("worker", active, tc.AGENT_NAMES)
    self.assertNotIn("disable_model_selection", agent)
    self.assertNotIn("cascade_config", agent["config"])
    self.assertIn("NOT PINNED", agent["description"])
    with self.assertRaisesRegex(tc.ConfigError, "learn_model"):
      render.install_session(active, "conv-1")

  def test_no_template_placeholder_survives(self):
    self.make_config()
    active = tc.load_active()
    for role in tc.ALL_ROLES:
      text = json.dumps(render.render_agent(role, active, tc.AGENT_NAMES))
      self.assertNotIn("{{", text)


class SetModelsTest(FixtureMixin, unittest.TestCase):
  """Drives model choice through the MCP tools, as the IDE would."""

  def setUp(self):
    self.make_config()
    self.out = os.path.join(self.tmp, "out")
    p = mock.patch.object(render, "AGENTS_DIR", os.path.join(self.out, "agents"))
    p.start()
    self.addCleanup(p.stop)
    self.brain = os.path.join(self.app_dir, "brain")

  def read_json(self, *parts):
    with open(os.path.join(*parts)) as f:
      return json.load(f)

  def test_session_pins_are_immediate_and_leave_defaults_alone(self):
    err, body = self.call("set_models", {
        "conversation_id": "abc-123", "subagent": "fast-medium",
        "scout": "fast-low",
    })
    self.assertFalse(err, body)
    self.assertEqual(body["status"], "SESSION")
    names = body["use_these_type_names"]
    self.assertEqual(names["scout"], "tw_scout_fast_low")
    self.assertEqual(names["worker"], "tw_worker_fast_medium")
    agent = self.read_json(self.brain, "abc-123", ".agents", "agents",
                           names["worker"], "agent.json")
    self.assertEqual(agent["name"], names["worker"])
    self.assertTrue(agent["disable_model_selection"])
    self.assertEqual(
        agent["config"]["cascade_config"]["planner_config"]["plan_model"],
        "MODEL_FAKE_FAST_MEDIUM",
    )
    self.assertEqual(tc.load_config()["subagent"], "fast-high")
    self.assertFalse(os.path.exists(os.path.join(self.out, "agents")))
    with mock.patch.object(tc, "read_conversation_model_enum", return_value=None):
      _, cfg = self.call("get_model_config", {"conversation_id": "abc-123"})
    self.assertEqual(
        cfg["session_pins"]["subagents"]["scout"]["model_id"], "fast-low"
    )

  def test_session_requires_conversation_id(self):
    err, text = self.call("set_models", {"subagent": "fast-low"})
    self.assertTrue(err)
    self.assertIn("conversation_id", text)

  def test_persist_writes_defaults_and_unpinned_orchestrator(self):
    err, body = self.call("set_models", {"persist": True, "subagent": "fast-medium"})
    self.assertFalse(err, body)
    self.assertEqual(body["status"], "PERSISTED")
    self.assertEqual(tc.load_config()["subagent"], "fast-medium")
    worker = self.read_json(self.out, "agents", "tw_worker", "agent.json")
    self.assertEqual(
        worker["config"]["cascade_config"]["planner_config"]["plan_model"],
        "MODEL_FAKE_FAST_MEDIUM",
    )
    orch = self.read_json(self.out, "agents", "tw_orchestrator", "agent.json")
    self.assertTrue(orch["main_agent"])
    self.assertNotIn("disable_model_selection", orch)  # picker decides
    self.assertNotIn("cascade_config", orch["config"])
    self.assertEqual(render.stale_files(tc.load_active()), [])

  def test_orchestrator_needs_billing_not_enum(self):
    err, body = self.call("set_models", {
        "conversation_id": "c1", "orchestrator": "brand-new-orchestrator",
    })
    self.assertFalse(err, body)
    self.assertEqual(body["pairing_verdict"], "partial")  # unknown billing

  def test_unknown_subagent_points_at_learn_model(self):
    err, text = self.call("set_models", {
        "conversation_id": "c1", "subagent": "named-only",
    })
    self.assertTrue(err)
    self.assertIn("learn_model", text)

  def test_strict_refuses_counterproductive(self):
    err, body = self.call("set_models", {
        "conversation_id": "c1", "subagent": "big-tokens", "strict": True,
    })
    self.assertFalse(err)
    self.assertEqual(body["status"], "REFUSED")
    self.assertFalse(os.path.exists(os.path.join(self.brain, "c1")))

  def test_dry_run_writes_nothing(self):
    _, body = self.call("set_models", {
        "conversation_id": "c1", "subagent": "fast-low", "dry_run": True,
    })
    self.assertEqual(body["status"], "DRY_RUN")
    self.assertFalse(os.path.exists(os.path.join(self.brain, "c1")))

  def test_conversation_id_validated(self):
    err, text = self.call("set_models", {
        "conversation_id": "../../etc", "subagent": "fast-low",
    })
    self.assertTrue(err)
    self.assertIn("invalid conversation id", text)

  def test_list_models(self):
    _, body = self.call("list_models", {"filter": "fast"})
    ids = [m["model_id"] for m in body["models"]]
    self.assertIn("fast-high", ids)
    self.assertNotIn("big-tokens", ids)


class LearnModelTest(FixtureMixin, unittest.TestCase):

  def setUp(self):
    self.make_config()

  def test_reads_structured_field_not_prompt_text(self):
    self.make_conversation("conv-a", "MODEL_FAKE_REAL_ONE")
    self.assertEqual(
        tc.read_conversation_model_enum("conv-a"), "MODEL_FAKE_REAL_ONE"
    )

  def test_learn_then_pin(self):
    self.make_conversation("conv-b", "MODEL_FAKE_NEW")
    err, body = self.call("learn_model", {
        "model_id": "new-model", "conversation_id": "conv-b",
        "billing_like": "fast-high",
    })
    self.assertFalse(err, body)
    cfg = tc.load_config()
    self.assertEqual(cfg["custom_models"]["new-model"]["enum"], "MODEL_FAKE_NEW")
    info = tc.resolve_model("new-model", tc.load_registry(cfg))
    self.assertEqual(info["billing"]["unit"], "calls")
    self.assertIn("assumed", info["notes"])
    err, body = self.call("set_models", {
        "conversation_id": "conv-c", "subagent": "new-model",
    })
    self.assertFalse(err, body)

  def test_learn_with_explicit_billing(self):
    self.make_conversation("conv-d", "MODEL_FAKE_D")
    err, _ = self.call("learn_model", {
        "model_id": "d-model", "conversation_id": "conv-d",
        "billing": {"unit": "tokens", "bucket": "X"},
    })
    self.assertFalse(err)
    info = tc.load_registry(tc.load_config())["d-model"]
    self.assertEqual(info["billing"], {"unit": "tokens", "bucket": "X"})
    err, text = self.call("learn_model", {
        "model_id": "d-model", "conversation_id": "conv-d",
        "billing": {"unit": "bogus"},
    })
    self.assertTrue(err)
    self.assertIn("billing.unit", text)

  def test_learn_never_touches_shipped_defaults(self):
    self.make_conversation("conv-e", "MODEL_FAKE_E")
    with open(tc.DEFAULT_CONFIG_PATH) as f:
      before = f.read()
    tc.learn_model("e-model", "conv-e")
    with open(tc.DEFAULT_CONFIG_PATH) as f:
      self.assertEqual(f.read(), before)

  def test_missing_conversation(self):
    with self.assertRaisesRegex(tc.ConfigError, "no conversation database"):
      tc.learn_model("x-model", "nope")


class ShippedFilesTest(unittest.TestCase):

  def test_generated_agents_match_active_config(self):
    """Fails if the config or a template changed without re-rendering."""
    stale = render.stale_files(tc.load_active())
    self.assertEqual(stale, [], "re-render with set_models persist=true")

  def test_shipped_registry_and_defaults_carry_no_enums(self):
    with open(os.path.join(ROOT, "models.json")) as f:
      models = json.load(f)["models"]
    self.assertFalse([m for m, e in models.items() if e.get("enum")])
    with open(tc.DEFAULT_CONFIG_PATH) as f:
      self.assertEqual(json.load(f)["custom_models"], {})

  def test_fresh_install_loads_unpinned(self):
    active = tc.load_active(
        tc.DEFAULT_CONFIG_PATH, os.path.join(ROOT, "models.json")
    )
    for role in tc.SUBAGENT_ROLES:
      self.assertIsNone(active["roles"][role]["enum"])
      agent = render.render_agent(role, active, tc.AGENT_NAMES)
      self.assertNotIn("disable_model_selection", agent)

  def test_every_manifest_is_valid_json(self):
    for rel in ("plugin.json", "mcp_config.json", "models.json",
                "tokenomics.default.json"):
      with open(os.path.join(ROOT, rel)) as f:
        json.load(f)
    for role in ("scout", "worker", "redteam", "orchestrator"):
      with open(os.path.join(ROOT, "templates", "agents", f"{role}.json")) as f:
        json.load(f)


class McpServerTest(FixtureMixin, unittest.TestCase):

  def setUp(self):
    self.make_config(subagent="fast-low")

  def test_initialize_and_list(self):
    init = srv.handle_json_rpc_request({"id": 1, "method": "initialize"})
    self.assertEqual(init["result"]["serverInfo"]["name"], "tokenomics-worker-tools")
    tools = srv.handle_json_rpc_request({"id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    self.assertEqual(names, set(srv.TOOL_HANDLERS))
    self.assertEqual(len(names), 8)

  def test_notifications_get_no_response(self):
    self.assertIsNone(
        srv.handle_json_rpc_request({"method": "notifications/initialized"})
    )

  def test_get_model_config_is_self_sufficient(self):
    err, body = self.call("get_model_config")
    self.assertFalse(err, body)
    defaults = body["plugin_defaults"]
    worker = defaults["subagents"]["worker"]
    self.assertEqual(worker["model_id"], "fast-low")
    self.assertEqual(worker["type_name"], "tw_worker")
    self.assertTrue(worker["pinned"])
    self.assertEqual(defaults["pairing_verdict"], "offload")
    self.assertTrue(defaults["findings"])
    self.assertIn("Tokens are the bill", defaults["orchestrator_cost_rules"])

  def test_live_model_is_reported(self):
    self.make_conversation("conv-live", "MODEL_FAKE_FAST_LOW")
    _, body = self.call("get_model_config", {"conversation_id": "conv-live"})
    self.assertEqual(body["live_model"]["enum"], "MODEL_FAKE_FAST_LOW")
    self.assertEqual(body["live_model"]["model_ids"], ["fast-low"])

  def test_quota_status_groups_shared_models(self):
    _, body = self.call("get_quota_status")
    self.assertEqual(len(body["subagents"]), 1)
    self.assertEqual(len(body["subagents"][0]["type_names"]), 3)
    err, _ = self.call("get_quota_status", {"role": "nope"})
    self.assertTrue(err)

  def test_unknown_tool_is_error(self):
    err, _ = self.call("nope")
    self.assertTrue(err)

  def test_bad_config_surfaces_as_tool_error(self):
    with open(tc.CONFIG_PATH, "w") as f:
      json.dump({"orchestrator": "big-tokens", "subagent": "made-up"}, f)
    err, text = self.call("set_models", {"conversation_id": "c", "dry_run": True})
    self.assertTrue(err)
    self.assertIn("made-up", text)

  def test_cache_status_for_call_metered_orchestrator(self):
    with open(tc.CONFIG_PATH, "w") as f:
      json.dump({"orchestrator": "big-calls", "subagent": "fast-high"}, f)
    now = srv.get_current_time()
    with mock.patch.object(srv, "get_last_turn_timestamp", return_value=now):
      body = srv.get_cache_status("abc")
    self.assertFalse(body["affects_quota"])
    self.assertTrue(body["estimate"])
    self.assertIn("call-metered", body["recommendation"])

  def test_cache_status_states_for_token_metered_orchestrator(self):
    now = srv.get_current_time()
    for ago, state in ((10, "WARM"), (250, "EXPIRING"), (400, "COLD")):
      with mock.patch.object(
          srv, "get_last_turn_timestamp",
          return_value=now - dt.timedelta(seconds=ago)), \
          mock.patch.object(srv, "get_current_time", return_value=now):
        self.assertEqual(srv.get_cache_status("abc")["cache_state"], state)

  def test_tampering_scan(self):
    diff = (
        "diff --git a/foo_test.py b/foo_test.py\n"
        "--- a/foo_test.py\n+++ b/foo_test.py\n"
        "-    self.assertEqual(a, b)\n"
        "+    x = 1  # pylint: disable=unused\n"
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n"
        "-    assert x\n"
    )
    v = srv.scan_diff_for_tampering(diff)
    self.assertEqual([x["type"] for x in v], ["DELETED_ASSERTION", "ADDED_SUPPRESSION"])

  def test_record_event_uses_configured_models(self):
    ledger = os.path.join(self.tmp, "ledger.jsonl")
    with mock.patch.object(srv, "TELEMETRY_FILE", ledger):
      err, body = self.call("record_orchestration_event", {
          "task_tier": "tier_2_pipeline", "verification_outcome": "PASSED",
          "conductor_turns": 4, "subagent_calls_offloaded": 10,
      })
    self.assertFalse(err, body)
    with open(ledger) as f:
      event = json.loads(f.readline())
    self.assertEqual(event["orchestrator_model"], "big-tokens")
    self.assertEqual(event["subagent_models"]["scout"], "fast-low")
    self.assertEqual(event["leverage_ratio"], 2.5)

  def test_process_message_parse_error(self):
    out = json.loads(srv.process_message("{bad"))
    self.assertEqual(out["error"]["code"], -32700)


class VerifyAndRecordTest(unittest.TestCase):

  def test_compute_weighted_tokens_uses_supplied_weights(self):
    self.assertEqual(
        verify_and_record.compute_weighted_tokens(
            0, 0, 0, 10, 0, weights={"output_and_thinking": 1.0}
        ),
        10.0,
    )

  def test_parser_defaults(self):
    args = verify_and_record.build_parser().parse_args([])
    self.assertEqual(args.plugin_name, "tokenomics-worker")
    self.assertIsNone(args.conductor_model)
    self.assertFalse(hasattr(args, "table_name"))


if __name__ == "__main__":
  unittest.main()
