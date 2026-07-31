import unittest

from agent_generation.drivers.opencode import OpenCodeDriver
from agent_generation.drivers.pi import PiDriver
from agent_generation.metrics import aggregate_trajectory_usage


class AgentTrajectoryMetricsTests(unittest.TestCase):
    def test_pi_usage_is_summed_across_assistant_turns(self):
        driver = PiDriver(base_url="http://127.0.0.1:58000/v1")
        trajectory = "\n".join(
            (
                '{"type":"turn_end"}',
                '{"type":"tool_execution_start","toolName":"write"}',
                '{"type":"message_end","message":{"role":"assistant",'
                '"usage":{"input":100,"output":20}}}',
                "not-json",
                '{"type":"turn_end"}',
                '{"type":"tool_execution_start","toolName":"bash"}',
                '{"type":"message_end","message":{"role":"assistant",'
                '"usage":{"input":130,"output":25}}}',
            )
        )

        usage = aggregate_trajectory_usage(driver, trajectory)

        self.assertEqual(usage.input_tokens, 230)
        self.assertEqual(usage.output_tokens, 45)
        self.assertEqual(usage.turns, 2)
        self.assertEqual(usage.tool_calls, 2)
        self.assertEqual(usage.usage_source, "trajectory")

    def test_opencode_counts_step_tokens_reasoning_and_completed_tools(self):
        driver = OpenCodeDriver(base_url="http://127.0.0.1:58000/v1")
        trajectory = "\n".join(
            (
                '{"type":"step_start","part":{"type":"step-start"}}',
                '{"type":"tool_use","part":{"type":"tool",'
                '"state":{"status":"completed"}}}',
                '{"type":"step_finish","part":{"type":"step-finish",'
                '"tokens":{"input":200,"output":30,"reasoning":40}}}',
                '{"type":"step_finish","part":{"type":"step-finish",'
                '"tokens":{"input":250,"output":35,"reasoning":45}}}',
            )
        )

        usage = aggregate_trajectory_usage(driver, trajectory)

        self.assertEqual(usage.input_tokens, 450)
        self.assertEqual(usage.output_tokens, 150)
        self.assertEqual(usage.turns, 2)
        self.assertEqual(usage.tool_calls, 1)
        self.assertEqual(usage.usage_source, "trajectory")

    def test_no_usage_events_remain_unknown_without_fabricated_tokens(self):
        driver = PiDriver(base_url="http://127.0.0.1:58000/v1")

        usage = aggregate_trajectory_usage(
            driver,
            '{"type":"agent_start"}\nplain diagnostic\n',
        )

        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.output_tokens)
        self.assertEqual(usage.turns, 0)
        self.assertEqual(usage.tool_calls, 0)
        self.assertEqual(usage.usage_source, "trajectory")


if __name__ == "__main__":
    unittest.main()
