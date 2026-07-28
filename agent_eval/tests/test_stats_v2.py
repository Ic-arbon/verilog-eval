import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class StatsV2Tests(unittest.TestCase):
    def test_report_combines_original_summary_and_generator_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            agent_root = run_root / "opencode"
            agent_root.mkdir()
            (agent_root / "summary.csv").write_text(
                "Prob001_zero,1,1,1.0,.\n"
                "Prob002_mux,0,1,0.0,S\n"
            )
            records = [
                (
                    "Prob001_zero",
                    {
                        "status": "completed",
                        "submitted": True,
                        "duration_seconds": 2.0,
                        "metrics": {
                            "turns": 2,
                            "tool_calls": 1,
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "parse_errors": 0,
                        },
                    },
                ),
                (
                    "Prob002_mux",
                    {
                        "status": "missing_submission",
                        "submitted": False,
                        "duration_seconds": 1.0,
                        "metrics": {
                            "turns": 1,
                            "tool_calls": 0,
                            "input_tokens": 80,
                            "output_tokens": 10,
                            "parse_errors": 0,
                        },
                    },
                ),
            ]
            for problem, record in records:
                sidecar = agent_root / problem / "sample01/agent.json"
                sidecar.parent.mkdir(parents=True)
                record.update({"agent": "opencode", "problem": problem})
                sidecar.write_text(json.dumps(record))

            repo_root = Path(__file__).resolve().parents[2]
            completed = subprocess.run(
                [
                    sys.executable,
                    str(repo_root / "scripts/agent-eval-stats"),
                    "--json",
                    str(run_root),
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            stats = report["agents"]["opencode"]
            self.assertEqual(stats["total"], 2)
            self.assertEqual(stats["passed"], 1)
            self.assertEqual(stats["submitted"], 1)
            self.assertEqual(
                stats["agent_status"],
                {"completed": 1, "missing_submission": 1},
            )
            self.assertEqual(stats["verilog_eval_symbol"], {".": 1, "S": 1})
            self.assertEqual(stats["metric_totals"]["total_tokens"], 210)


if __name__ == "__main__":
    unittest.main()
