import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.database import Database
from app.llm import analyze_simulation, _simulation_only_context


class SimulationAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Database(Path(self.temporary.name) / "history.sqlite3")
        self.db.initialize()
        self._insert_worker(
            "simulation-1",
            simulation=True,
            baseline={
                "pid": 8765,
                "name": "private-host-process",
                "cpu_percent": 38.0,
            },
        )
        self.db.execute(
            """INSERT INTO measurements
            (process_id,timestamp,cpu_percent,memory_mb,memory_percent,
            memory_growth_mb_s,process_state,responsive)
            VALUES(?,?,?,?,?,?,?,?)""",
            ("simulation-1", 100.0, 40.0, 32.0, 1.0, 0.1, "running", 1),
        )

    def _insert_worker(self, process_id, *, simulation, baseline=None):
        config = {"simulation": simulation}
        if baseline is not None:
            config["simulation_baseline"] = baseline
        self.db.execute(
            """INSERT INTO managed_processes
            (id,name,workload_type,pid,status,policy,config_json,started_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                process_id,
                process_id,
                "cpu",
                9999,
                "running",
                "preemptguard",
                json.dumps(config),
                "2026-01-01T00:00:00+00:00",
            ),
        )

    def test_analysis_context_excludes_host_baseline_identifiers(self):
        context = _simulation_only_context("simulation-1", self.db)
        serialized = json.dumps(context)

        self.assertIn("40.0", serialized)
        self.assertNotIn("private-host-process", serialized)
        self.assertNotIn("8765", serialized)
        self.assertNotIn("9999", serialized)

    def test_analysis_sends_only_simulation_context_to_ollama(self):
        with patch(
            "app.llm._request_json",
            return_value={"message": {"content": "The worker is healthy."}},
        ) as request:
            result = analyze_simulation("simulation-1", self.db)

        prompt = request.call_args.kwargs["payload"]["messages"][1]["content"]
        self.assertNotIn("private-host-process", prompt)
        self.assertNotIn("8765", prompt)
        self.assertEqual(result["source"], "local_simulation_history_only")
        self.assertEqual(result["analysis"], "The worker is healthy.")

    def test_real_process_records_are_rejected_before_ollama_call(self):
        self._insert_worker("ordinary-worker", simulation=False)
        with patch("app.llm._request_json") as request:
            with self.assertRaises(PermissionError):
                analyze_simulation("ordinary-worker", self.db)
        request.assert_not_called()

    def test_similar_context_only_includes_simulation_workers(self):
        self._insert_worker("simulation-2", simulation=True)
        self._insert_worker("ordinary-worker", simulation=False)
        context = _simulation_only_context("simulation-1", self.db)

        self.assertEqual(
            [case["scenario"] for case in context["similar_local_simulation_cases"]],
            ["cpu"],
        )


if __name__ == "__main__":
    unittest.main()
