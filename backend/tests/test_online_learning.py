import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from app.core.database import Database
from app.api.learning import (
    get_learning_status,
    get_pending_learning_feedback,
    get_process_recommendation,
)
from app.host_recovery import (
    HostRecoveryError,
    execute_recovery,
)
from app.online_learning import process_features, recommend_action, record_feedback


class OnlineLearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Database(Path(self.temporary.name) / "learning.sqlite3")
        self.db.initialize()

    def test_existing_feedback_schema_is_migrated_for_pending_feedback(self):
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE host_recovery_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pid INTEGER NOT NULL,
                    process_started_at REAL NOT NULL,
                    process_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    helped INTEGER,
                    features_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            connection.commit()
        finally:
            connection.close()

        legacy_db = Database(legacy_path)
        legacy_db.initialize()
        run_columns = {
            row["name"] for row in legacy_db.query("PRAGMA table_info(host_action_runs)")
        }
        self.assertIn("duration_s", run_columns)
        legacy_db.execute(
            """INSERT INTO host_action_runs
            (pid,process_started_at,process_name,action,status,details,features_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (7, 10.0, "test-app.exe", "lower_priority", "requested", "{}", "{}", 1.0),
        )

        with patch("app.api.learning.database", legacy_db):
            pending = get_pending_learning_feedback()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action_run_id"], 1)

        record_feedback(
            pid=7,
            process_started_at=10.0,
            process_name="test-app.exe",
            action="lower_priority",
            action_run_id=1,
            helped=True,
            features={},
            db=legacy_db,
        )
        with patch("app.api.learning.database", legacy_db):
            self.assertEqual(get_pending_learning_feedback(), [])

    def add_host_sample(self, pid=54321, started_at=100.0, risk_score=0.4):
        risk_category = (
            "Critical" if risk_score >= 0.85 else
            "High" if risk_score >= 0.65 else
            "Moderate" if risk_score >= 0.3 else "Low"
        )
        self.db.execute(
            """INSERT INTO host_process_samples
            (pid,process_started_at,sampled_at,name,state,cpu_percent,memory_mb,
            memory_percent,growth_mb_s,risk_score,risk_category,health,
            risk_evidence_json,recommended_action,recovery_method)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, started_at, 200.0, "test-app.exe", "running", 50.0, 100.0,
                2.0, 0.1, risk_score, risk_category, "Elevated resource use", "[]",
                "review", "review",
            ),
        )

    def test_model_recommends_only_after_five_nearby_outcomes(self):
        current = {
            "cpu_percent": 50.0,
            "memory_percent": 2.0,
            "growth_mb_s": 0.1,
            "risk_score": 0.7,
        }
        cases = [
            {
                "action": "lower_priority",
                "helped": True,
                "features": process_features(current),
                "source": "actual",
            }
            for _ in range(5)
        ]
        result = recommend_action(current, self.db, cases)

        self.assertEqual(result["recommendation"], "lower_priority")
        self.assertEqual(result["estimates"][0]["similar_outcomes"], 5)

    def test_model_status_reports_simulation_training_and_measured_performance(self):
        self.db.execute(
            """INSERT INTO managed_processes
            (id,name,workload_type,pid,status,policy,config_json,started_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                "simulation-1", "test simulation", "crash", 123, "completed",
                "preemptguard", json.dumps({"simulation": True}), "2026-10-28T00:00:00Z",
            ),
        )
        self.db.execute(
            """INSERT INTO measurements
            (process_id,timestamp,process_state,responsive)
            VALUES(?,?,?,?)""",
            ("simulation-1", 1.0, "running", 1),
        )
        self.db.execute(
            """INSERT INTO measurements
            (process_id,timestamp,process_state,responsive)
            VALUES(?,?,?,?)""",
            ("simulation-1", 2.0, "failed", 0),
        )
        self.db.execute(
            """INSERT INTO predictions
            (process_id,timestamp,risk_score,category,confidence,horizon_s,
            model_name,features_json,contributors_json,recommended_action)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "simulation-1", 1.0, 0.9, "Critical", 0.8, 2,
                "rule_based_v1", "{}", "[]", "restart",
            ),
        )
        for timestamp, status, duration in (
            (2.0, "healthy", 2.5),
            (3.0, "failed", 4.5),
        ):
            self.db.execute(
                """INSERT INTO recovery_attempts
                (process_id,timestamp,attempt_number,status,duration_s,reason)
                VALUES(?,?,?,?,?,?)""",
                ("simulation-1", timestamp, 1, status, duration, "test outcome"),
            )

        self.add_host_sample(pid=7, started_at=10.0)
        self.db.execute(
            """INSERT INTO host_action_runs
            (pid,process_started_at,process_name,action,status,details,
            features_json,duration_s,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (7, 10.0, "test-app.exe", "lower_priority", "requested", "{}", "{}", 0.2, 1.0),
        )
        record_feedback(
            pid=7,
            process_started_at=10.0,
            process_name="test-app.exe",
            action="lower_priority",
            action_run_id=1,
            helped=True,
            features=process_features({
                "cpu_percent": 50.0,
                "memory_percent": 2.0,
                "growth_mb_s": 0.1,
                "risk_score": 0.4,
            }),
            db=self.db,
        )

        with patch("app.api.learning.database", self.db):
            status = get_learning_status()

        self.assertEqual(status["training_sources"], {
            "actual_processes": 1,
            "simulated_recoveries": 2,
        })
        self.assertEqual(status["prediction_evaluation"]["true_positives"], 1)
        self.assertEqual(status["prediction_evaluation"]["precision"], 1.0)
        self.assertEqual(status["prediction_evaluation"]["accuracy"], 1.0)
        self.assertEqual(status["recovery_performance"]["simulations"]["attempts"], 2)
        self.assertEqual(
            status["recovery_performance"]["simulations"]["mean_duration_s"],
            3.5,
        )
        self.assertEqual(status["actual_feedback"]["helpfulness_rate"], 1.0)
        self.assertEqual(
            status["recovery_performance"]["actual_processes"]["mean_duration_s"],
            0.2,
        )

    def test_model_does_not_recommend_recovery_below_high_risk(self):
        current = {
            "cpu_percent": 50.0,
            "memory_percent": 2.0,
            "growth_mb_s": 0.1,
            "risk_score": 0.64,
        }
        cases = [
            {
                "action": "lower_priority",
                "helped": True,
                "features": process_features(current),
                "source": "actual",
            }
            for _ in range(5)
        ]

        result = recommend_action(current, self.db, cases)

        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["estimates"], [])
        self.assertIn("high or critical risk", result["reason"])

    def test_model_recommends_recovery_for_high_risk_with_enough_outcomes(self):
        current = {
            "cpu_percent": 50.0,
            "memory_percent": 2.0,
            "growth_mb_s": 0.1,
            "risk_score": 0.65,
        }
        cases = [
            {
                "action": "lower_priority",
                "helped": True,
                "features": process_features(current),
                "source": "actual",
            }
            for _ in range(5)
        ]

        self.assertEqual(
            recommend_action(current, self.db, cases)["recommendation"],
            "lower_priority",
        )

    def test_recommendation_endpoint_returns_learned_method_for_high_risk(self):
        self.add_host_sample(pid=7, started_at=10.0, risk_score=0.7)
        features = process_features({
            "cpu_percent": 50.0,
            "memory_percent": 2.0,
            "growth_mb_s": 0.1,
            "risk_score": 0.7,
        })
        for action_run_id in range(1, 6):
            record_feedback(
                pid=7,
                process_started_at=10.0,
                process_name="test-app.exe",
                action="lower_priority",
                action_run_id=action_run_id,
                helped=True,
                features=features,
                db=self.db,
            )

        with patch("app.api.learning.database", self.db):
            result = get_process_recommendation(7, 10.0)

        self.assertEqual(result["recommendation"], "lower_priority")

    def test_sudden_change_can_use_learned_method_below_high_risk(self):
        self.add_host_sample(pid=7, started_at=10.0, risk_score=0.4)
        features = process_features({
            "cpu_percent": 50.0,
            "memory_percent": 2.0,
            "growth_mb_s": 0.1,
            "risk_score": 0.4,
        })
        for action_run_id in range(1, 6):
            record_feedback(
                pid=7,
                process_started_at=10.0,
                process_name="test-app.exe",
                action="lower_priority",
                action_run_id=action_run_id,
                helped=True,
                features=features,
                db=self.db,
            )

        with patch("app.api.learning.database", self.db):
            ordinary = get_process_recommendation(7, 10.0)
            incident = get_process_recommendation(7, 10.0, sudden_change=True)

        self.assertIsNone(ordinary["recommendation"])
        self.assertEqual(incident["recommendation"], "lower_priority")

    def test_stopped_process_recommendation_is_resume_even_at_low_risk(self):
        self.add_host_sample(pid=7, started_at=10.0, risk_score=0.2)
        self.db.execute(
            "UPDATE host_process_samples SET state=? WHERE pid=? AND process_started_at=?",
            ("stopped", 7, 10.0),
        )

        with patch("app.api.learning.database", self.db):
            result = get_process_recommendation(7, 10.0)

        self.assertEqual(result["recommendation"], "resume")
        self.assertEqual(result["model"], "stopped_process_resume_v1")

    def test_model_declines_with_too_few_or_unsuccessful_cases(self):
        current = {
            "cpu_percent": 50.0,
            "memory_percent": 2.0,
            "growth_mb_s": 0.1,
            "risk_score": 0.7,
        }
        features = process_features(current)
        too_few = [
            {
                "action": "lower_priority",
                "helped": True,
                "features": features,
                "source": "actual",
            }
            for _ in range(4)
        ]
        failures = [
            {
                "action": "lower_priority",
                "helped": False,
                "features": features,
                "source": "actual",
            }
            for _ in range(5)
        ]

        self.assertIsNone(recommend_action(current, self.db, too_few)["recommendation"])
        self.assertIsNone(recommend_action(current, self.db, failures)["recommendation"])

    def test_feedback_is_saved_as_local_action_outcome(self):
        record_feedback(
            pid=7,
            process_started_at=10.0,
            process_name="test-app.exe",
            action="lower_priority",
            action_run_id=22,
            helped=True,
            features=process_features({
                "cpu_percent": 50,
                "memory_percent": 2,
                "growth_mb_s": 0.1,
                "risk_score": 0.4,
            }),
            db=self.db,
        )
        examples = recommend_action({
            "cpu_percent": 50,
            "memory_percent": 2,
            "growth_mb_s": 0.1,
            "risk_score": 0.4,
        }, self.db)["training_example_count"]
        self.assertEqual(examples, 1)

    def test_unsure_feedback_is_stored_but_not_used_as_training_label(self):
        record_feedback(
            pid=7,
            process_started_at=10.0,
            process_name="test-app.exe",
            action="lower_priority",
            action_run_id=23,
            helped=None,
            features=process_features({
                "cpu_percent": 50,
                "memory_percent": 2,
                "growth_mb_s": 0.1,
                "risk_score": 0.4,
            }),
            db=self.db,
        )
        self.assertEqual(
            self.db.query("SELECT helped FROM host_recovery_feedback")[0]["helped"],
            None,
        )
        self.assertEqual(
            recommend_action({
                "cpu_percent": 50,
                "memory_percent": 2,
                "growth_mb_s": 0.1,
                "risk_score": 0.4,
            }, self.db)["training_example_count"],
            0,
        )

    def test_simulation_outcomes_are_used_but_ordinary_workers_are_excluded(self):
        for process_id, simulation in (("sim", True), ("ordinary", False)):
            self.db.execute(
                """INSERT INTO managed_processes
                (id,name,workload_type,status,policy,config_json,started_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    process_id,
                    process_id,
                    "cpu",
                    "stopped",
                    "preemptguard",
                    json.dumps({"simulation": simulation}),
                    "2026-01-01",
                ),
            )
            self.db.execute(
                """INSERT INTO measurements
                (process_id,timestamp,cpu_percent,memory_percent,memory_growth_mb_s,
                process_state,responsive)
                VALUES(?,?,?,?,?,?,?)""",
                (process_id, 200.0, 50.0, 2.0, 0.1, "running", 1),
            )
            self.db.execute(
                """INSERT INTO recovery_attempts
                (process_id,timestamp,attempt_number,status,reason)
                VALUES(?,?,?,?,?)""",
                (process_id, 201.0, 1, "healthy", "verified"),
            )
        from app.online_learning import load_training_examples

        examples = load_training_examples(self.db)
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]["source"], "simulation")


class HostRecoveryApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Database(Path(self.temporary.name) / "recovery.sqlite3")
        self.db.initialize()
        self.db.execute(
            """INSERT INTO host_process_samples
            (pid,process_started_at,sampled_at,name,state,cpu_percent,memory_mb,
            memory_percent,growth_mb_s,risk_score,risk_category,health,
            risk_evidence_json,recommended_action,recovery_method)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                54321, 100.0, 200.0, "test-app.exe", "running", 50.0, 100.0,
                2.0, 0.1, 0.4, "Moderate", "Elevated resource use", "[]",
                "review", "review",
            ),
        )

    def fake_processes(self):
        parent = Mock()
        parent.children.return_value = []
        process = Mock()
        process.create_time.return_value = 100.0
        process.name.return_value = "test-app.exe"
        process.is_running.return_value = True
        process.nice.return_value = psutil.NORMAL_PRIORITY_CLASS
        return parent, process

    def test_action_requires_approval_and_remembers_it_for_exact_instance(self):
        parent, process = self.fake_processes()
        with patch("app.host_recovery.psutil.Process", side_effect=[parent, process]):
            with self.assertRaises(PermissionError):
                execute_recovery(
                    pid=54321,
                    process_started_at=100.0,
                    action="lower_priority",
                    db=self.db,
                )

        parent, process = self.fake_processes()
        with patch("app.host_recovery.psutil.Process", side_effect=[parent, process]):
            result = execute_recovery(
                pid=54321,
                process_started_at=100.0,
                action="lower_priority",
                allow_action=True,
                db=self.db,
            )

        self.assertEqual(result["status"], "requested")
        self.assertEqual(
            self.db.query("SELECT action FROM host_action_consents")[0]["action"],
            "lower_priority",
        )
        recorded_run = self.db.query(
            "SELECT status,duration_s FROM host_action_runs"
        )[0]
        self.assertEqual(recorded_run["status"], "requested")
        self.assertGreaterEqual(recorded_run["duration_s"], 0.0)
        parent, process = self.fake_processes()
        with patch("app.host_recovery.psutil.Process", side_effect=[parent, process]):
            repeated = execute_recovery(
                pid=54321,
                process_started_at=100.0,
                action="lower_priority",
                db=self.db,
            )
        self.assertEqual(repeated["status"], "requested")

    def test_stopped_process_can_be_resumed_after_approval(self):
        self.db.execute(
            "UPDATE host_process_samples SET state=? WHERE pid=? AND process_started_at=?",
            ("stopped", 54321, 100.0),
        )
        parent, process = self.fake_processes()
        process.status.return_value = psutil.STATUS_STOPPED
        with patch("app.host_recovery.psutil.Process", side_effect=[parent, process]):
            result = execute_recovery(
                pid=54321,
                process_started_at=100.0,
                action="resume",
                allow_action=True,
                db=self.db,
            )

        self.assertEqual(result["action"], "resume")
        self.assertEqual(result["status"], "requested")
        process.resume.assert_called_once_with()
        self.assertEqual(
            self.db.query("SELECT action FROM host_action_consents")[0]["action"],
            "resume",
        )

    def test_one_time_recovery_approval_is_not_persisted(self):
        parent, process = self.fake_processes()
        with patch("app.host_recovery.psutil.Process", side_effect=[parent, process]):
            result = execute_recovery(
                pid=54321,
                process_started_at=100.0,
                action="lower_priority",
                allow_action=True,
                remember_action=False,
                db=self.db,
            )

        self.assertEqual(result["status"], "requested")
        self.assertEqual(self.db.query("SELECT * FROM host_action_consents"), [])
        self.assertEqual(
            self.db.query("SELECT action FROM host_action_runs")[0]["action"],
            "lower_priority",
        )

    def test_pid_reuse_is_rejected(self):
        parent, process = self.fake_processes()
        process.create_time.return_value = 101.0
        with patch("app.host_recovery.psutil.Process", side_effect=[parent, process]):
            with self.assertRaises(HostRecoveryError):
                execute_recovery(
                    pid=54321,
                    process_started_at=100.0,
                    action="lower_priority",
                    allow_action=True,
                    db=self.db,
                )


if __name__ == "__main__":
    unittest.main()
