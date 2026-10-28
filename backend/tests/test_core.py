import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil

from app.core.database import Database
from app.experiments.metrics import summarize_run
from app.api.experiments import (
    ExperimentRequest,
    build_experiment_scenarios,
    summarize_policies,
)
from app.api.monitoring import get_overview
from app.monitoring.features import extract_features
from app.monitoring.system_processes import SystemProcessMonitor, assess_process
from app.prediction.risk_estimator import estimate_risk
from app.recovery.decision_engine import choose_action
from app.schemas import SettingsUpdate, WorkloadRequest, validate_settings_update


class MonitoringTests(unittest.TestCase):
    def test_system_process_sampling_calculates_rss_growth(self):
        def fake_process(rss):
            return SimpleNamespace(info={
                "pid": 123,
                "name": "sample-app",
                "create_time": 1.0,
                "status": psutil.STATUS_RUNNING,
                "cpu_percent": 40.0,
                "memory_info": SimpleNamespace(rss=rss * 1024 * 1024),
            })

        monitor = SystemProcessMonitor()
        monitor._logical_cpu_count = 2
        monitor._total_memory = 1000 * 1024 * 1024
        with (
            patch(
                "app.monitoring.system_processes.psutil.process_iter",
                side_effect=[[fake_process(100)], [fake_process(106)]],
            ),
            patch("app.monitoring.system_processes.time.time", side_effect=[100.0, 102.0]),
        ):
            monitor.refresh()
            first = monitor.snapshot()["processes"][0]
            monitor.refresh()
            second = monitor.snapshot()["processes"][0]

        self.assertIsNone(first["growth_mb_s"])
        self.assertAlmostEqual(second["growth_mb_s"], 3.0)
        self.assertEqual(second["memory_mb"], 106.0)
        self.assertEqual(second["risk_category"], "Moderate")
        self.assertIn("Resident memory is growing", second["risk_evidence"])

    def test_system_process_samples_persist_locally(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db = Database(Path(temporary.name) / "host-history.sqlite3")
        db.initialize()
        fake = SimpleNamespace(info={
            "pid": 321,
            "name": "sample-app",
            "create_time": 10.0,
            "status": psutil.STATUS_RUNNING,
            "cpu_percent": 12.5,
            "memory_info": SimpleNamespace(rss=40 * 1024 * 1024),
        })
        monitor = SystemProcessMonitor(db)
        with (
            patch("app.monitoring.system_processes.psutil.process_iter", return_value=[fake]),
            patch("app.monitoring.system_processes.time.time", return_value=100.0),
        ):
            monitor.refresh()

        samples = db.query(
            "SELECT * FROM host_process_samples WHERE pid=? AND process_started_at=?",
            (321, 10.0),
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["name"], "sample-app")
        self.assertEqual(samples[0]["memory_mb"], 40.0)
        self.assertNotIn("command_line", samples[0])

    def test_host_recovery_does_not_run_without_selected_live_statistics(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db = Database(Path(temporary.name) / "host-history.sqlite3")
        db.initialize()
        db.execute(
            """INSERT INTO host_action_consents
            (pid,process_started_at,action,created_at) VALUES(?,?,?,?)""",
            (321, 10.0, "resume", 1.0),
        )
        fake = SimpleNamespace(info={
            "pid": 321,
            "name": "sample-app",
            "create_time": 10.0,
            "status": psutil.STATUS_STOPPED,
            "cpu_percent": 0.0,
            "memory_info": SimpleNamespace(rss=40 * 1024 * 1024),
        })
        monitor = SystemProcessMonitor(db)
        with (
            patch("app.monitoring.system_processes.psutil.process_iter", return_value=[fake]),
            patch("app.monitoring.system_processes.time.time", return_value=100.0),
        ):
            monitor.refresh()

        self.assertEqual(
            monitor.snapshot()["processes"][0]["state"],
            psutil.STATUS_STOPPED,
        )
        self.assertEqual(db.query("SELECT * FROM host_action_runs"), [])

    def test_system_process_advice_is_read_only_and_tailored_to_growth(self):
        assessment = assess_process(
            state=psutil.STATUS_RUNNING,
            cpu_percent=10.0,
            normalized_cpu_percent=5.0,
            memory_mb=500.0,
            memory_percent=2.0,
            growth_mb_s=6.0,
            high_cpu_samples=0,
        )
        self.assertIn("memory trend", assessment["recommended_action"])
        self.assertIn("gracefully", assessment["recovery_method"])

    def test_system_process_advice_requests_no_action_for_normal_load(self):
        assessment = assess_process(
            state=psutil.STATUS_RUNNING,
            cpu_percent=1.0,
            normalized_cpu_percent=0.1,
            memory_mb=20.0,
            memory_percent=0.1,
            growth_mb_s=0.0,
            high_cpu_samples=0,
        )
        self.assertEqual(assessment["health"], "Normal")
        self.assertEqual(assessment["risk_category"], "Low")
        self.assertEqual(
            assessment["recommended_action"],
            "No recovery needed; continue monitoring.",
        )

    def test_overview_counts_only_measured_responsive_processes(self):
        processes = [
            {"status": "running", "metrics": {"responsive": 1}, "prediction": None},
            {"status": "running", "metrics": None, "prediction": None},
            {"status": "running", "metrics": {"responsive": None}, "prediction": None},
        ]
        with (
            patch("app.api.monitoring.process_manager.list_processes", return_value=processes),
            patch(
                "app.api.monitoring.database.query",
                side_effect=[[], [{"count": 0}], []],
            ),
        ):
            overview = get_overview()

        self.assertEqual(overview["healthy_processes"], 1)


class FeatureTests(unittest.TestCase):
    def test_empty_history_returns_safe_features(self):
        features = extract_features([])
        self.assertEqual(features["history_samples"], 0)
        self.assertEqual(features["responsive"], 0)

    def test_features_use_only_supplied_past_samples(self):
        samples = [
            {
                "process_state": "running",
                "cpu_percent": value,
                "memory_mb": 20 + value,
                "memory_growth_mb_s": 1.0 + value / 10,
                "responsive": 1,
            }
            for value in (10, 20, 30, 50)
        ]
        features = extract_features(samples)
        self.assertEqual(features["history_samples"], 4)
        self.assertEqual(features["cpu_current"], 50)
        self.assertGreater(features["cpu_trend"], 0)
        self.assertEqual(features["memory_current_mb"], 70)

    def test_features_include_recent_failures_and_restarts(self):
        sample = {
            "process_state": "running",
            "cpu_percent": 0.0,
            "memory_mb": 20.0,
            "memory_growth_mb_s": 0.0,
            "responsive": 1,
            "recent_failure_count": 2,
            "recent_restart_count": 3,
        }
        features = extract_features([sample])
        self.assertEqual(features["recent_failures"], 2)
        self.assertEqual(features["recent_restarts"], 3)


class RiskAndDecisionTests(unittest.TestCase):
    def test_risk_has_bounded_score_and_explanation(self):
        prediction = estimate_risk(
            "managed-id",
            {
                "cpu_mean": 90, "cpu_trend": 20,
                "memory_growth_mb_s": 3, "memory_acceleration_mb_s2": 1,
                "missed_heartbeats": 1, "abnormal_samples": 5,
                "history_samples": 10,
            },
            30, 0.35, 0.65,
        )
        self.assertGreaterEqual(prediction.risk_score, 0)
        self.assertLessEqual(prediction.risk_score, 1)
        self.assertTrue(prediction.contributors)
        self.assertIn("uncalibrated", prediction.model_name)

    def test_combined_cpu_and_memory_pressure_can_reach_high_risk(self):
        prediction = estimate_risk(
            "managed-id",
            {
                "cpu_mean": 95, "cpu_trend": 40,
                "memory_growth_mb_s": 3, "memory_acceleration_mb_s2": 1.5,
                "missed_heartbeats": 0, "abnormal_samples": 10,
                "history_samples": 10,
            },
            30, 0.35, 0.65,
        )
        self.assertEqual(prediction.category, "High")
        self.assertGreaterEqual(prediction.risk_score, 0.65)

    def test_low_confidence_requests_recheck(self):
        decision = choose_action(
            policy="preemptguard", risk_score=0.4, confidence=0.3,
            low_threshold=0.35, high_threshold=0.65, responsive=True,
            failed=False, restart_count=0, restart_limit=3,
        )
        self.assertEqual(decision.action, "recheck")

    def test_temporary_stale_heartbeat_rechecks_before_restart(self):
        decision = choose_action(
            policy="preemptguard", risk_score=0.4, confidence=0.6,
            low_threshold=0.35, high_threshold=0.65, responsive=False,
            failed=False, restart_count=0, restart_limit=3,
            confirmed_unresponsive=False,
        )
        self.assertEqual(decision.action, "recheck")

    def test_restart_limit_prevents_retry(self):
        decision = choose_action(
            policy="preemptguard", risk_score=1, confidence=0.9,
            low_threshold=0.35, high_threshold=0.65, responsive=False,
            failed=True, restart_count=3, restart_limit=3,
        )
        self.assertEqual(decision.action, "investigate")

    def test_confirmed_unresponsive_process_restarts_when_allowed(self):
        decision = choose_action(
            policy="preemptguard", risk_score=0.7, confidence=0.7,
            low_threshold=0.35, high_threshold=0.65, responsive=False,
            failed=False, restart_count=0, restart_limit=3,
            confirmed_unresponsive=True,
        )
        self.assertEqual(decision.action, "restart")

    def test_temporary_operation_failure_retries_before_restart(self):
        decision = choose_action(
            policy="preemptguard", risk_score=0.1, confidence=0.7,
            low_threshold=0.35, high_threshold=0.65, responsive=True,
            failed=False, restart_count=0, restart_limit=3,
            workload_type="temporary_io", operation_status="failed",
            operation_retry_count=0, max_operation_retries=2,
        )
        self.assertEqual(decision.action, "retry")

    def test_sustained_cpu_workload_is_throttled_once(self):
        decision = choose_action(
            policy="preemptguard", risk_score=0.5, confidence=0.7,
            low_threshold=0.35, high_threshold=0.65, responsive=True,
            failed=False, restart_count=0, restart_limit=3,
            workload_type="cpu", cpu_overloaded=True, throttled=False,
        )
        self.assertEqual(decision.action, "throttle")

    def test_combined_cpu_memory_workload_is_throttled_once(self):
        decision = choose_action(
            policy="preemptguard", risk_score=0.7, confidence=0.9,
            low_threshold=0.35, high_threshold=0.65, responsive=True,
            failed=False, restart_count=0, restart_limit=3,
            workload_type="cpu_memory", cpu_overloaded=True, throttled=False,
        )
        self.assertEqual(decision.action, "throttle")

    def test_confidence_ablation_rechecks_below_configured_minimum(self):
        decision = choose_action(
            policy="confidence_recheck", risk_score=0.9, confidence=0.4,
            low_threshold=0.35, high_threshold=0.65, responsive=True,
            failed=False, restart_count=0, restart_limit=3,
            minimum_confidence=0.6,
        )
        self.assertEqual(decision.action, "recheck")

    def test_baselines_have_distinct_rules(self):
        fixed = choose_action(
            policy="fixed", risk_score=0.9, confidence=0.9,
            low_threshold=0.35, high_threshold=0.65, responsive=True,
            failed=False, restart_count=0, restart_limit=3,
        )
        threshold = choose_action(
            policy="threshold", risk_score=0.1, confidence=0.9,
            low_threshold=0.35, high_threshold=0.65, responsive=True,
            failed=False, restart_count=0, restart_limit=3,
            threshold_detected=True,
        )
        self.assertEqual(fixed.action, "continue monitoring")
        self.assertEqual(threshold.action, "restart")


class MetricTests(unittest.TestCase):
    def test_experiment_pairs_healthy_control_with_failure_scenario(self):
        scenarios = build_experiment_scenarios(
            WorkloadRequest(workload_type="unresponsive", fault_after_s=12)
        )
        self.assertEqual(
            [(name, workload.workload_type) for name, workload in scenarios],
            [
                ("healthy_control", "healthy"),
                ("failure_scenario", "unresponsive"),
            ],
        )

    def test_non_failure_experiment_workload_uses_crash_scenario(self):
        scenarios = build_experiment_scenarios(
            WorkloadRequest(workload_type="cpu")
        )
        self.assertEqual(scenarios[1][1].workload_type, "crash")

    def test_experiment_requires_horizon_plus_negative_observation_window(self):
        with self.assertRaises(ValueError):
            ExperimentRequest(
                workload_type=WorkloadRequest(
                    workload_type="crash", fault_after_s=15
                ),
                duration_s=32,
                prediction_horizon=30,
            )
        request = ExperimentRequest(
            workload_type=WorkloadRequest(
                workload_type="crash", fault_after_s=15
            ),
            duration_s=33,
            prediction_horizon=30,
        )
        self.assertEqual(request.duration_s, 33)

    def test_labeled_runs_report_zero_scores_when_no_positive_is_predicted(self):
        measurements = [
            {"timestamp": 0.0, "process_state": "running", "responsive": 1},
            {"timestamp": 5.0, "process_state": "running", "responsive": 1},
            {"timestamp": 10.0, "process_state": "running", "responsive": 1},
            {"timestamp": 20.0, "process_state": "failed", "responsive": 0},
        ]
        predictions = [
            {
                "timestamp": timestamp,
                "risk_score": 0.2,
                "model_name": "rule_based_v1 (uncalibrated risk score)",
            }
            for timestamp in (0.0, 5.0, 10.0)
        ]
        result = summarize_run(
            measurements, predictions, [], horizon_s=10, risk_threshold=0.65
        )
        self.assertEqual(result["labeled_prediction_samples"], 3)
        self.assertEqual(result["true_negatives"], 2)
        self.assertEqual(result["false_negatives"], 1)
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["recall"], 0.0)
        self.assertEqual(result["f1"], 0.0)
        self.assertEqual(result["false_positive_rate"], 0.0)
        self.assertEqual(
            result["prediction_model_counts"],
            {"rule_based_v1 (uncalibrated risk score)": 3},
        )

    def test_policy_summary_reports_mean_spread_and_available_count(self):
        rows = [
            {"policy": "threshold", "metrics": {"f1": 0.4, "precision": None}},
            {"policy": "threshold", "metrics": {"f1": 0.6, "precision": 0.5}},
        ]
        summary = summarize_policies(rows)[0]
        self.assertEqual(summary["run_count"], 2)
        self.assertEqual(summary["metrics"]["f1"]["mean"], 0.5)
        self.assertAlmostEqual(summary["metrics"]["f1"]["std"], 0.1)
        self.assertEqual(summary["metrics"]["precision"]["n"], 1)
        self.assertEqual(summary["metrics"]["precision"]["std"], 0.0)

    def test_policy_summary_keeps_control_and_failure_conditions_separate(self):
        rows = [
            {
                "policy": "preemptguard",
                "scenario": "healthy_control",
                "metrics": {"f1": 0.0},
            },
            {
                "policy": "preemptguard",
                "scenario": "failure_scenario",
                "metrics": {"f1": 0.5},
            },
        ]
        summaries = summarize_policies(rows)
        self.assertEqual(
            [(item["scenario"], item["run_count"], item["metrics"]["f1"]["mean"])
             for item in summaries],
            [("healthy_control", 1, 0.0), ("failure_scenario", 1, 0.5)],
        )

    def test_metrics_mark_unlabeled_runs_as_unavailable(self):
        result = summarize_run([], [], [], horizon_s=30)
        self.assertIsNone(result["precision"])
        self.assertEqual(result["unnecessary_restarts"], 0)
        self.assertEqual(result["sample_count"], 0)
        self.assertIsNone(result["recovery_success_rate"])


    def test_metrics_use_failure_horizon(self):
        measurements = [
            {"timestamp": 10.0, "process_state": "running"},
            {"timestamp": 31.0, "process_state": "failed"},
        ]
        predictions = [
            {"timestamp": 0.0, "risk_score": 0.8},
            {"timestamp": 10.0, "risk_score": 0.8},
            {"timestamp": 20.0, "risk_score": 0.1},
        ]
        result = summarize_run(measurements, predictions, [], horizon_s=30)
        self.assertEqual(result["true_positives"], 1)
        self.assertEqual(result["false_negatives"], 1)
        self.assertEqual(result["false_positive_rate"], 1)
        self.assertEqual(result["first_early_warning_lead_time_s"], 21.0)

    def test_metrics_exclude_predictions_at_or_during_observed_failure(self):
        measurements = [
            {"timestamp": 0.0, "process_state": "running", "responsive": 1},
            {"timestamp": 10.0, "process_state": "failed", "responsive": 0},
            {"timestamp": 20.0, "process_state": "running", "responsive": 1},
            {"timestamp": 50.0, "process_state": "running", "responsive": 1},
        ]
        predictions = [
            {"timestamp": 0.0, "risk_score": 0.9},
            {"timestamp": 10.0, "risk_score": 1.0},
            {"timestamp": 20.0, "risk_score": 0.1},
        ]
        result = summarize_run(measurements, predictions, [], horizon_s=30)
        self.assertEqual(result["true_positives"], 1)
        self.assertEqual(result["false_positives"], 0)
        self.assertEqual(result["true_negatives"], 1)
        self.assertEqual(result["labeled_prediction_samples"], 2)

    def test_metrics_report_recovery_rates_and_complete_unnecessary_restart_window(self):
        measurements = [
            {"timestamp": 10.0, "process_state": "running", "responsive": 1},
            {"timestamp": 11.0, "process_state": "running", "responsive": 1},
            {"timestamp": 12.0, "process_state": "running", "responsive": 1},
            {"timestamp": 13.0, "process_state": "running", "responsive": 1},
        ]
        recoveries = [
            {"timestamp": 10.0, "duration_s": 1.0, "status": "healthy"},
            {"timestamp": 12.0, "duration_s": 3.0, "status": "failed"},
        ]
        result = summarize_run(
            measurements, [], recoveries, horizon_s=3,
            post_recovery_observation_s=1,
        )
        self.assertEqual(result["recovery_success_rate"], 0.5)
        self.assertEqual(result["mean_recovery_time_s"], 2.0)
        self.assertEqual(result["median_recovery_time_s"], 2.0)
        self.assertEqual(result["unnecessary_restarts"], 1)


class PersistenceAndValidationTests(unittest.TestCase):
    def test_settings_are_validated_as_a_pair(self):
        with self.assertRaises(ValueError):
            validate_settings_update(SettingsUpdate(
                low_risk_threshold=0.8,
                high_risk_threshold=0.7,
            ))

    def test_database_persists_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.initialize()
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?)",
                ("prediction_horizon", "45"),
            )
            self.assertEqual(
                db.query("SELECT value FROM app_settings WHERE key=?", ("prediction_horizon",))[0]["value"],
                "45",
            )


if __name__ == "__main__":
    unittest.main()
