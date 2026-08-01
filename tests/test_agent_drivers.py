from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_generation.contracts import AgentRunRequest
from agent_generation.drivers.base import (
    ARTIFACT_INSTRUCTION,
    INLINE_ARTIFACT_INSTRUCTION,
)
from agent_generation.drivers.opencode import OpenCodeDriver
from agent_generation.drivers.pi import PiDriver


SECRET = "do-not-persist-this-api-key"


def make_request(workspace: Path) -> AgentRunRequest:
    return AgentRunRequest(
        sample_id="Prob001_zero_sample01",
        agent_name="test-agent",
        model="qwen3.6-coder",
        task="spec-to-rtl",
        prompt_text="Produce TopModule.",
        rules_text=None,
        workspace=workspace,
        timeout_seconds=60,
        max_turns=12,
        max_tool_calls=24,
        max_input_tokens=16384,
        per_call_max_tokens=8192,
    )


class PiDriverTests(unittest.TestCase):
    def test_pi_writes_env_backed_model_config_and_narrow_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = make_request(Path(tmp))
            driver = PiDriver(
                base_url="http://127.0.0.1:58000/v1",
                api_key_environment="VLLM_API_KEY",
                thinking_enabled=True,
            )

            config_paths = driver.write_config(request)
            command = driver.build_command(request)
            environment = driver.environment(request)

            self.assertEqual(len(config_paths), 2)
            config_text = config_paths[0].read_text()
            config = json.loads(config_text)
            settings = json.loads(config_paths[1].read_text())
            provider = config["providers"]["vllm-local"]
            model = provider["models"][0]
            self.assertEqual(provider["baseUrl"], "http://127.0.0.1:58000/v1")
            self.assertEqual(provider["api"], "openai-completions")
            self.assertEqual(provider["apiKey"], "$VLLM_API_KEY")
            self.assertEqual(provider["compat"]["maxTokensField"], "max_tokens")
            self.assertEqual(
                provider["compat"]["thinkingFormat"], "qwen-chat-template"
            )
            self.assertEqual(model["id"], "qwen3.6-coder")
            self.assertEqual(model["contextWindow"], 24576)
            self.assertEqual(model["maxTokens"], 8192)
            self.assertEqual(
                settings["compaction"],
                {
                    "enabled": True,
                    "reserveTokens": 8192,
                    "keepRecentTokens": 8192,
                },
            )
            self.assertNotIn(SECRET, config_text)

            self.assertEqual(command[0], "/agent-tools/node_modules/.bin/pi")
            self.assertIn("--mode", command)
            self.assertIn("json", command)
            self.assertIn("--no-session", command)
            self.assertIn("--no-context-files", command)
            self.assertIn("--no-extensions", command)
            self.assertIn("--no-skills", command)
            self.assertIn("--no-prompt-templates", command)
            self.assertIn("read,write,edit,bash", command)
            self.assertIn("--system-prompt", command)
            self.assertNotIn("--append-system-prompt", command)
            system_prompt = command[command.index("--system-prompt") + 1]
            self.assertEqual(system_prompt, PI_ARTIFACT_SYSTEM_PROMPT)
            self.assertIn("MUST invoke the write or edit tool", system_prompt)
            self.assertIn("chat text is never a submission", system_prompt)
            self.assertNotIn("Read /workspace/TASK.md", command[-1])
            self.assertIn("Public task specification:", command[-1])
            self.assertIn(request.prompt_text, command[-1])
            self.assertEqual(command[command.index("--thinking") + 1], "high")
            self.assertEqual(
                driver.profile_id, "pi-minimal-system-inline-artifact-thinking-v1"
            )
            self.assertNotIn(SECRET, " ".join(command))

            self.assertIn(
                ("PI_CODING_AGENT_DIR", "/workspace/.agent-config/pi"),
                environment.variables,
            )
            self.assertEqual(environment.inherit, ("VLLM_API_KEY",))

    def test_pi_can_disable_request_thinking_without_changing_submission_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = make_request(Path(tmp))
            driver = PiDriver(
                base_url="http://127.0.0.1:58000/v1",
                thinking_enabled=False,
            )

            driver.write_config(request)
            command = driver.build_command(request)

            self.assertEqual(command[command.index("--thinking") + 1], "off")
            self.assertIn(request.prompt_text, command[-1])
            self.assertEqual(
                driver.profile_id,
                "pi-minimal-system-inline-artifact-no-thinking-v1",
            )


class OpenCodeDriverTests(unittest.TestCase):
    def test_opencode_writes_env_backed_provider_and_artifact_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = make_request(Path(tmp))
            driver = OpenCodeDriver(
                base_url="http://127.0.0.1:58000/v1",
                api_key_environment="VLLM_API_KEY",
                temperature=0.6,
                top_p=0.95,
                thinking_enabled=False,
            )

            config_paths = driver.write_config(request)
            command = driver.build_command(request)
            environment = driver.environment(request)

            self.assertEqual(len(config_paths), 1)
            config_text = config_paths[0].read_text()
            config = json.loads(config_text)
            agent = config["agent"]["benchmark"]
            provider = config["provider"]["vllm-local"]
            model = provider["models"]["qwen3.6-coder"]

            self.assertEqual(agent["mode"], "primary")
            self.assertEqual(agent["steps"], 12)
            self.assertEqual(agent["permission"]["*"], "deny")
            self.assertEqual(agent["permission"]["read"], "allow")
            self.assertEqual(agent["permission"]["edit"], "allow")
            self.assertEqual(agent["permission"]["bash"], "allow")
            self.assertEqual(agent["permission"]["task"], "deny")
            self.assertEqual(agent["permission"]["webfetch"], "deny")
            self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
            self.assertEqual(
                provider["options"]["baseURL"], "http://127.0.0.1:58000/v1"
            )
            self.assertEqual(provider["options"]["apiKey"], "{env:VLLM_API_KEY}")
            self.assertFalse(
                model["options"]["chat_template_kwargs"]["enable_thinking"]
            )
            self.assertEqual(model["limit"]["context"], 24576)
            self.assertEqual(model["limit"]["output"], 8192)
            self.assertNotIn("input", model["limit"])
            self.assertNotIn(SECRET, config_text)

            self.assertEqual(
                command[0], "/agent-tools/node_modules/.bin/opencode"
            )
            self.assertIn("--pure", command)
            self.assertIn("run", command)
            self.assertEqual(command[command.index("--format") + 1], "json")
            self.assertEqual(command[command.index("--dir") + 1], "/workspace")
            self.assertEqual(
                command[command.index("--model") + 1],
                "vllm-local/qwen3.6-coder",
            )
            self.assertEqual(command[command.index("--agent") + 1], "benchmark")
            self.assertTrue(command[-1].startswith(INLINE_ARTIFACT_INSTRUCTION))
            self.assertIn(request.prompt_text, command[-1])
            self.assertNotIn("Read /workspace/TASK.md", command[-1])
            self.assertEqual(
                driver.profile_id, "opencode-inline-artifact-no-thinking-v1"
            )
            self.assertNotIn(SECRET, " ".join(command))

            self.assertIn(
                ("OPENCODE_CONFIG", "/workspace/.agent-config/opencode.json"),
                environment.variables,
            )
            self.assertEqual(environment.inherit, ("VLLM_API_KEY",))

    def test_driver_event_parsers_reject_non_json_without_failing_generation(self):
        drivers = (
            PiDriver(base_url="http://127.0.0.1:58000/v1"),
            OpenCodeDriver(base_url="http://127.0.0.1:58000/v1"),
        )

        for driver in drivers:
            with self.subTest(driver=driver.profile_id):
                self.assertIsNone(driver.parse_event("not-json"))
                self.assertEqual(
                    driver.parse_event('{"type":"agent_start"}'),
                    {"type": "agent_start"},
                )

    def test_pi_and_opencode_receive_identical_inline_public_task_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = make_request(Path(tmp))
            pi_command = PiDriver(
                base_url="http://127.0.0.1:58000/v1"
            ).build_command(request)
            opencode_command = OpenCodeDriver(
                base_url="http://127.0.0.1:58000/v1"
            ).build_command(request)

            self.assertEqual(pi_command[-1], opencode_command[-1])
            self.assertEqual(
                pi_command[-1], INLINE_ARTIFACT_INSTRUCTION + request.prompt_text
            )

    def test_drivers_classify_only_completed_budget_events(self):
        pi = PiDriver(base_url="http://127.0.0.1:58000/v1")
        opencode = OpenCodeDriver(base_url="http://127.0.0.1:58000/v1")

        self.assertEqual(pi.classify_budget_event('{"type":"turn_end"}'), "turn")
        self.assertEqual(
            pi.classify_budget_event('{"type":"tool_execution_end"}'), "tool"
        )
        self.assertIsNone(
            pi.classify_budget_event('{"type":"tool_execution_start"}')
        )
        self.assertEqual(
            opencode.classify_budget_event(
                '{"type":"tool_use","part":{"state":{"status":"completed"}}}'
            ),
            "tool",
        )
        self.assertIsNone(
            opencode.classify_budget_event(
                '{"type":"tool_use","part":{"state":{"status":"running"}}}'
            )
        )
        self.assertIsNone(
            opencode.classify_budget_event('{"type":"step_finish"}')
        )


if __name__ == "__main__":
    unittest.main()
