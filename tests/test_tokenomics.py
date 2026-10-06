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
import http.server
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
for sub in ("lib", "scripts", "mcp"):
  sys.path.insert(0, os.path.join(ROOT, sub))

import catalog  # noqa: E402
import render  # noqa: E402
import tokenomics_config as tc  # noqa: E402
import tokenomics_tools_server as srv  # noqa: E402
import verify_and_record  # noqa: E402


class FakeLSHandler(http.server.BaseHTTPRequestHandler):
  """Fake Antigravity Language Server for testing catalog fetch."""
  recorded_csrf: str | None = None
  request_payload: bytes = b""

  def do_POST(self):
    FakeLSHandler.recorded_csrf = self.headers.get("x-codeium-csrf-token")
    length = int(self.headers.get("Content-Length", 0))
    FakeLSHandler.request_payload = self.rfile.read(length)
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    payload = {
        "clientModelConfigs": [
            {
                "modelId": "fast-high",
                "label": "Fast model, high",
                "isRecommended": True,
                "modelOrAlias": {"model": "MODEL_FAKE_FAST_HIGH"},
                "quotaInfo": {
                    # No remainingFraction -> proto3 zero value -> 0.0
                    "resetTime": "2026-10-06T21:00:00Z",
                },
            },
            {
                "modelId": "fast-low",
                "label": "Fast model, low",
                "isRecommended": False,
                "modelOrAlias": {"model": "MODEL_FAKE_FAST_LOW"},
                "quotaInfo": {
                    "remainingFraction": 0.8,
                    "resetTime": "2026-10-06T21:00:00Z",
                },
            },
        ]
    }
    self.wfile.write(json.dumps(payload).encode("utf-8"))

  def log_message(self, format, *args):
    pass

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
        "fallbacks": {
            "orchestrator": None,
            "subagent": None,
            "scout": None,
            "worker": None,
            "redteam": None,
        },
        "fallback_threshold": 0.05,
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
        mock.patch.object(catalog, "read_catalog", return_value=None),
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

  def setUp(self):
    p = mock.patch.object(catalog, "read_catalog", return_value=None)
    p.start()
    self.addCleanup(p.stop)

  def test_generated_agents_match_active_config(self):
    """Fails if the config or a template changed without re-rendering.

    A clean checkout ships agents rendered from the defaults with no catalog.
    With a private tokenomics.config.json, agents/ was re-rendered locally
    from that config plus the live catalog cache, so compare against those.
    """
    if os.path.exists(tc.CONFIG_PATH):
      mock.patch.stopall()
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
    self.assertEqual(len(names), 10)

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
    self.assertNotIn("table_name", vars(args))


class CatalogLiveFetchTest(unittest.TestCase):

  def test_fetch_live_catalog_via_fake_server(self):
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeLSHandler)
    port = server.server_port
    thread = threading.Thread(target=server.handle_request)
    thread.daemon = True
    thread.start()

    env = {
        "ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{port}",
        "ANTIGRAVITY_CSRF_TOKEN": "test-csrf-token",
    }
    result = catalog.fetch_live_catalog(env=env, timeout=5.0)
    server.server_close()
    thread.join(timeout=1.0)

    self.assertEqual(FakeLSHandler.recorded_csrf, "test-csrf-token")
    self.assertEqual(result["source"], "language_server")
    models = result["models"]
    self.assertEqual(len(models), 2)
    self.assertEqual(models[0]["model_id"], "fast-high")
    # Missing remainingFraction in proto3 JSON defaults to 0.0
    self.assertEqual(models[0]["remaining_fraction"], 0.0)
    self.assertEqual(models[1]["model_id"], "fast-low")
    self.assertEqual(models[1]["remaining_fraction"], 0.8)
    self.assertEqual(models[0]["quota_group"], "quota-group-1")
    self.assertEqual(models[1]["quota_group"], "quota-group-2")

  def test_refusal_of_non_local_hosts(self):
    for bad_host in ("example.com:8080", "192.168.1.100:5000", "8.8.8.8:80"):
      with self.assertRaisesRegex(tc.ConfigError, "non-local host"):
        catalog.fetch_from_language_server(bad_host, "some-token")

  def test_cli_models_fallback_parsing(self):
    mock_proc = mock.Mock(
        returncode=0,
        stdout=(
            "Fetching available models...\n"
            "fast-high\tFast Model (High)\n"
            "fast-low\tFast Model (Low)\n"
        ),
        stderr="",
    )
    with mock.patch("subprocess.run", return_value=mock_proc):
      models = catalog.fetch_from_cli_models()
      self.assertEqual(len(models), 2)
      self.assertEqual(models[0]["model_id"], "fast-high")
      self.assertEqual(models[0]["label"], "Fast Model (High)")
      self.assertIsNone(models[0]["enum"])
      self.assertIsNone(models[0]["remaining_fraction"])
      self.assertIsNone(models[0]["quota_group"])

  def test_quota_group_inference(self):
    models = [
        {"model_id": "m1", "remaining_fraction": 0.8, "reset_time": "2026-10-06T21:00:00Z"},
        {"model_id": "m2", "remaining_fraction": 0.8, "reset_time": "2026-10-06T21:00:00Z"},
        {"model_id": "m3", "remaining_fraction": 0.4, "reset_time": "2026-10-06T21:00:00Z"},
        {"model_id": "m4", "remaining_fraction": None, "reset_time": None},
    ]
    catalog._assign_quota_groups(models)
    self.assertEqual(models[0]["quota_group"], "quota-group-1")
    self.assertEqual(models[1]["quota_group"], "quota-group-1")
    self.assertEqual(models[2]["quota_group"], "quota-group-2")
    self.assertIsNone(models[3]["quota_group"])


class ResolveQueryTest(unittest.TestCase):

  def setUp(self):
    self.models = [
        {"model_id": "fast-high", "label": "Fast Model (High)"},
        {"model_id": "fast-low", "label": "Fast Model (Low)"},
        {"model_id": "big-tokens", "label": "Big Model Token"},
    ]

  def test_exact_id(self):
    self.assertEqual(catalog.resolve_query("fast-high", self.models), "fast-high")

  def test_exact_label(self):
    self.assertEqual(
        catalog.resolve_query("fast model (high)", self.models), "fast-high"
    )

  def test_fuzzy_unique(self):
    self.assertEqual(catalog.resolve_query("high fast", self.models), "fast-high")
    self.assertEqual(catalog.resolve_query("low", self.models), "fast-low")

  def test_ambiguous_raises_candidates(self):
    with self.assertRaises(tc.ConfigError) as ctx:
      catalog.resolve_query("fast", self.models)
    self.assertIn("fast-high", str(ctx.exception))
    self.assertIn("fast-low", str(ctx.exception))

  def test_no_match_raises_close_candidates(self):
    with self.assertRaises(tc.ConfigError) as ctx:
      catalog.resolve_query("nonexistent-turbo", self.models)
    self.assertIn("no model matches query", str(ctx.exception))

  def test_exact_id_beats_fuzzy(self):
    models = [
        {"model_id": "fast", "label": "Fast Basic"},
        {"model_id": "fast-high", "label": "Fast High"},
    ]
    self.assertEqual(catalog.resolve_query("fast", models), "fast")


class FallbackConfigAndPrecedenceTest(FixtureMixin, unittest.TestCase):

  def test_registry_precedence_with_catalog(self):
    self.make_config(
        custom_models={
            "fast-high": {
                "label": "Fast High Custom",
                "enum": "MODEL_CUSTOM_HIGH",
            },
        }
    )
    fake_catalog = {
        "fetched_at": "2026-10-06T20:00:00Z",
        "source": "language_server",
        "models": [
            {
                "model_id": "fast-high",
                "label": "Fast High Live",
                "enum": "MODEL_FAKE_FAST_HIGH",
                "remaining_fraction": 0.5,
                "reset_time": "2026-10-06T22:00:00Z",
                "quota_group": "quota-group-1",
                "recommended": True,
            },
            {
                "model_id": "live-only-model",
                "label": "Live Only Model",
                "enum": "MODEL_FAKE_LIVE_ONLY",
                "remaining_fraction": 0.9,
                "reset_time": "2026-10-06T22:00:00Z",
                "quota_group": "quota-group-2",
                "recommended": False,
            },
        ],
    }
    reg = tc.load_registry(tc.load_config(), catalog=fake_catalog)
    self.assertEqual(reg["fast-high"]["label"], "Fast High Custom")
    self.assertEqual(reg["fast-high"]["enum"], "MODEL_CUSTOM_HIGH")
    self.assertEqual(reg["fast-high"]["enum_source"], "custom_models")
    self.assertEqual(reg["fast-high"]["remaining_fraction"], 0.5)
    self.assertEqual(reg["fast-high"]["quota_group"], "quota-group-1")
    self.assertIn("live-only-model", reg)
    self.assertEqual(reg["live-only-model"]["enum_source"], "live")
    self.assertEqual(
        reg["live-only-model"]["billing"]["bucket"], "quota-group-2"
    )
    self.assertTrue(reg["live-only-model"]["billing"].get("bucket_inferred"))

  def test_fallback_config_validation(self):
    cfg = tc.load_config(
        self.make_config(
            fallbacks={"subagent": "fast-low"}, fallback_threshold=0.1
        )
    )
    self.assertEqual(cfg["fallbacks"]["subagent"], "fast-low")
    self.assertEqual(cfg["fallback_threshold"], 0.1)

    with self.assertRaisesRegex(tc.ConfigError, "fallback_threshold"):
      tc.load_config(self.make_config(fallback_threshold=1.5))

    with self.assertRaisesRegex(tc.ConfigError, "same as primary"):
      tc.resolve_roles(
          tc.load_config(self.make_config(fallbacks={"subagent": "fast-high"})),
          tc.load_registry(),
      )


class SessionAndFallbackRenderTest(FixtureMixin, unittest.TestCase):

  def test_session_install_with_fallbacks(self):
    cfg_path = self.make_config(
        fallbacks={"subagent": "fast-low", "orchestrator": "big-calls"}
    )
    active = tc.build_active(tc.load_config(cfg_path), tc.load_registry())
    brain_dir = os.path.join(self.app_dir, "brain")
    manifest = render.install_session(active, "conv-fb", brain_root=brain_dir)
    self.assertEqual(manifest["orchestrator_fallback"], "big-calls")
    worker_fb = manifest["subagents"]["worker"]["fallback"]
    self.assertIsNotNone(worker_fb)
    self.assertEqual(worker_fb["model_id"], "fast-low")
    self.assertEqual(worker_fb["type_name"], "tw_worker_fast_low")

    pin_path = os.path.join(
        brain_dir,
        "conv-fb",
        ".agents",
        "agents",
        "tw_worker_fast_low",
        "agent.json",
    )
    self.assertTrue(os.path.exists(pin_path))

  def test_plugin_default_fallback_rendering_and_cleanup(self):
    cfg_path = self.make_config(fallbacks={"scout": "fast-low"})
    active = tc.build_active(tc.load_config(cfg_path), tc.load_registry())
    agents_dir = os.path.join(self.app_dir, "agents")
    writes = render.planned_writes(active, agents_dir=agents_dir)
    scout_fb_path = os.path.join(agents_dir, "tw_scout_fallback", "agent.json")
    self.assertIn(scout_fb_path, writes)
    for p, c in writes.items():
      render.write_file(p, c)

    stale_dir = os.path.join(agents_dir, "tw_stale_fallback")
    os.makedirs(stale_dir, exist_ok=True)
    removed = render.cleanup_fallback_agents(
        agents_dir=agents_dir, active=active
    )
    self.assertIn(stale_dir, removed)
    self.assertFalse(os.path.exists(stale_dir))
    self.assertTrue(os.path.exists(os.path.join(agents_dir, "tw_scout_fallback")))


class RoutingRulesTest(FixtureMixin, unittest.TestCase):

  def setUp(self):
    cfg_path = self.make_config(
        fallbacks={"subagent": "fast-low", "orchestrator": "big-calls"},
        fallback_threshold=0.05,
    )
    self.active = tc.build_active(tc.load_config(cfg_path), tc.load_registry())

  def test_healthy_primary(self):
    cat = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": [
            {"model_id": "fast-high", "remaining_fraction": 0.8},
            {"model_id": "fast-low", "remaining_fraction": 0.9},
            {"model_id": "big-tokens", "remaining_fraction": 0.5},
            {"model_id": "big-calls", "remaining_fraction": 0.5},
        ],
    }
    routes = tc.route_roles(self.active, cat)
    self.assertFalse(routes["worker"]["using_fallback"])
    self.assertEqual(routes["worker"]["model_id"], "fast-high")
    self.assertIn("healthy", routes["worker"]["reason"])

  def test_primary_exhausted_routes_to_fallback(self):
    cat = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": [
            {"model_id": "fast-high", "remaining_fraction": 0.02},
            {"model_id": "fast-low", "remaining_fraction": 0.8},
            {"model_id": "big-tokens", "remaining_fraction": 0.5},
        ],
    }
    routes = tc.route_roles(self.active, cat)
    self.assertTrue(routes["worker"]["using_fallback"])
    self.assertEqual(routes["worker"]["model_id"], "fast-low")
    self.assertIn("routed to fallback", routes["worker"]["reason"])

  def test_primary_missing_from_catalog(self):
    cat = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": [
            {"model_id": "fast-low", "remaining_fraction": 0.8},
            {"model_id": "big-tokens", "remaining_fraction": 0.5},
        ],
    }
    routes = tc.route_roles(self.active, cat)
    self.assertTrue(routes["worker"]["using_fallback"])
    self.assertEqual(routes["worker"]["model_id"], "fast-low")
    self.assertIn("absent from live catalog", routes["worker"]["reason"])

  def test_fallback_worse_than_primary_stays_on_primary(self):
    cat = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": [
            {"model_id": "fast-high", "remaining_fraction": 0.04},
            {"model_id": "fast-low", "remaining_fraction": 0.01},
            {"model_id": "big-tokens", "remaining_fraction": 0.5},
        ],
    }
    routes = tc.route_roles(self.active, cat)
    self.assertFalse(routes["worker"]["using_fallback"])
    self.assertEqual(routes["worker"]["model_id"], "fast-high")
    self.assertIn("not better than primary", routes["worker"]["reason"])

  def test_no_catalog_stays_on_primary(self):
    routes = tc.route_roles(self.active, catalog=None)
    self.assertFalse(routes["worker"]["using_fallback"])
    self.assertEqual(routes["worker"]["model_id"], "fast-high")
    self.assertIn("no live quota data", routes["worker"]["reason"])

  def test_orchestrator_routing_action(self):
    cat = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": [
            {"model_id": "big-tokens", "remaining_fraction": 0.01},
            {"model_id": "big-calls", "remaining_fraction": 0.8},
            {"model_id": "fast-high", "remaining_fraction": 0.5},
        ],
    }
    routes = tc.route_roles(self.active, cat)
    orch = routes["orchestrator"]
    self.assertTrue(orch["using_fallback"])
    self.assertEqual(orch["model_id"], "big-calls")
    self.assertIsNone(orch["use_type_name"])
    self.assertEqual(
        orch["action"],
        "Ask the user to run /model and pick 'Big model, call-metered'",
    )


class McpServerExtendedTest(FixtureMixin, unittest.TestCase):

  def test_set_models_defaults_orchestrator_from_live_model(self):
    self.make_config(orchestrator="big-tokens", subagent="fast-high")
    self.make_conversation("conv-live-orch", "MODEL_FAKE_BIG_CALLS")
    err, body = self.call(
        "set_models", {"conversation_id": "conv-live-orch"}
    )
    self.assertFalse(err, body)
    self.assertEqual(
        body["orchestrator"]["model_id"], "big-calls"
    )
    changes_str = " ".join(body["changes"])
    self.assertIn("detected from live conversation", changes_str)

  def test_list_models_with_picker(self):
    self.make_config()
    sample_catalog = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "language_server",
        "models": [
            {
                "model_id": "fast-high",
                "label": "Fast model, high",
                "enum": "MODEL_FAKE_FAST_HIGH",
                "remaining_fraction": 0.8,
                "reset_time": "2026-10-06T21:00:00Z",
                "recommended": True,
            }
        ],
    }
    with mock.patch.object(catalog, "read_catalog", return_value=sample_catalog):
      err, body = self.call("list_models")
      self.assertFalse(err, body)
      self.assertIn("picker", body)
      self.assertIn("Fast model, high", body["picker"])
      self.assertIsNotNone(body.get("catalog_age_seconds"))

  def test_route_models_tool(self):
    self.make_config()
    err, body = self.call("route_models")
    self.assertFalse(err, body)
    self.assertIn("routing", body)
    self.assertIn("worker", body["routing"])

  def test_set_model_info_tool(self):
    self.make_config()
    err, body = self.call(
        "set_model_info",
        {
            "model_id": "brand-new-custom",
            "label": "Brand New Custom",
            "billing_like": "fast-high",
        },
    )
    self.assertFalse(err, body)
    self.assertEqual(body["model_id"], "brand-new-custom")
    cfg = tc.load_config()
    self.assertIn("brand-new-custom", cfg["custom_models"])
    self.assertEqual(
        cfg["custom_models"]["brand-new-custom"]["label"], "Brand New Custom"
    )


if __name__ == "__main__":
  unittest.main()
