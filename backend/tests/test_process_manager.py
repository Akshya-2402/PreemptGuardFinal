import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import settings_store
from app.core.database import Database
from app.schemas import WorkloadRequest
from app.workloads.files import atomic_replace_with_retry
from app.workloads.manager import ProcessManager


class ManagedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "manager.sqlite3")
        self.db.initialize()
        self.manager = ProcessManager(self.db)
        self.original_settings = settings_store.get()
        settings_store.update({
            "monitoring_interval": 0.5,
            "restart_cooldown": 0.0,
            "max_restart_attempts": 2,
            "max_operation_retries": 2,
            "verification_period": 2.5,
        })

    def test_atomic_file_replacement_retries_transient_windows_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tmp"
            destination = Path(directory) / "destination.json"
            source.write_text('{"ok":true}', encoding="utf-8")
            real_replace = os.replace
            attempts = 0

            def replace_with_transient_lock(src, dst):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("temporary Windows sharing violation")
                real_replace(src, dst)

            with patch("app.workloads.files.os.replace", side_effect=replace_with_transient_lock):
                atomic_replace_with_retry(source, destination)
            self.assertEqual(attempts, 2)
            self.assertEqual(destination.read_text(encoding="utf-8"), '{"ok":true}')

    async def asyncTearDown(self):
        self.manager.stop_all()
        settings_store.update(self.original_settings.model_dump())
        self.temp.cleanup()

    async def test_restart_requires_multiple_heartbeats_over_verification_period(self):
        managed = self.manager.start(WorkloadRequest(
            workload_type="healthy",
            policy="preemptguard",
            duration_s=60,
        ))
        result = await self.manager.restart(managed["id"], "unit test recovery")
        self.assertEqual(result["status"], "healthy")
        recovery = self.db.query(
            "SELECT * FROM recovery_attempts WHERE process_id=?",
            (managed["id"],),
        )[0]
        self.assertGreaterEqual(recovery["verification_period_s"], 2.0)
        self.assertNotEqual(recovery["old_pid"], recovery["new_pid"])

    async def test_temporary_operation_failure_is_retried_without_process_restart(self):
        managed = self.manager.start(WorkloadRequest(
            workload_type="temporary_io",
            policy="preemptguard",
            duration_s=40,
            fault_after_s=3,
        ))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await self.manager.sample_all()
            decisions = self.db.query(
                "SELECT action FROM decisions WHERE process_id=?",
                (managed["id"],),
            )
            if any(item["action"] == "retry" for item in decisions):
                break
            await asyncio.sleep(0.2)
        decisions = self.db.query(
            "SELECT action FROM decisions WHERE process_id=?",
            (managed["id"],),
        )
        self.assertIn("retry", [item["action"] for item in decisions])
        process = self.db.query(
            "SELECT retry_count,restart_count FROM managed_processes WHERE id=?",
            (managed["id"],),
        )[0]
        self.assertEqual(process["retry_count"], 1)
        self.assertEqual(process["restart_count"], 0)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            await self.manager.sample_all()
            latest = self.db.query(
                "SELECT operation_status FROM measurements WHERE process_id=? "
                "ORDER BY timestamp DESC LIMIT 1",
                (managed["id"],),
            )[0]
            if latest["operation_status"] == "succeeded":
                break
            await asyncio.sleep(0.2)
        self.assertEqual(latest["operation_status"], "succeeded")

    async def test_cpu_worker_is_measured_and_throttled_without_restart(self):
        managed = self.manager.start(WorkloadRequest(
            workload_type="cpu",
            policy="preemptguard",
            duration_s=40,
        ))
        deadline = time.monotonic() + 15
        throttle = []
        while time.monotonic() < deadline:
            await self.manager.sample_all()
            throttle = self.db.query(
                "SELECT timestamp,action,reason,outcome FROM decisions "
                "WHERE process_id=? AND action='throttle'",
                (managed["id"],),
            )
            if throttle:
                break
            await asyncio.sleep(0.5)

        self.assertTrue(throttle, "sustained CPU usage did not trigger throttling")
        before = self.db.query(
            "SELECT cpu_percent FROM measurements WHERE process_id=? AND timestamp<? "
            "AND cpu_percent IS NOT NULL ORDER BY timestamp DESC LIMIT 3",
            (managed["id"], throttle[0]["timestamp"]),
        )
        self.assertGreaterEqual(len(before), 2)
        self.assertTrue(any(row["cpu_percent"] >= 35.0 for row in before))
        self.assertEqual(self.manager._read_control(managed["id"])["throttle"], True)

        deadline = time.monotonic() + 3
        after = []
        while time.monotonic() < deadline:
            await self.manager.sample_all()
            after = self.db.query(
                "SELECT cpu_percent FROM measurements WHERE process_id=? AND timestamp>=? "
                "AND cpu_percent IS NOT NULL ORDER BY timestamp",
                (managed["id"], throttle[0]["timestamp"]),
            )
            if len(after) >= 3:
                break
            await asyncio.sleep(0.5)

        self.assertGreaterEqual(len(after), 3)
        self.assertLess(
            sum(row["cpu_percent"] for row in after[-3:]) / 3,
            sum(row["cpu_percent"] for row in before) / len(before),
        )
        process = self.db.query(
            "SELECT restart_count,status FROM managed_processes WHERE id=?",
            (managed["id"],),
        )[0]
        self.assertEqual(process["restart_count"], 0)
        self.assertEqual(process["status"], "running")

    async def test_memory_worker_rss_increases_as_allocated_pages_are_touched(self):
        managed = self.manager.start(WorkloadRequest(
            workload_type="memory",
            policy="fixed",
            duration_s=40,
            memory_rate_mb_s=4.0,
        ))
        deadline = time.monotonic() + 15
        rss_samples = []
        while time.monotonic() < deadline:
            await self.manager.sample_all()
            rows = self.db.query(
                "SELECT memory_mb FROM measurements WHERE process_id=? "
                "AND memory_mb IS NOT NULL ORDER BY timestamp",
                (managed["id"],),
            )
            rss_samples = [row["memory_mb"] for row in rows]
            if rss_samples and max(rss_samples) - min(rss_samples) >= 8.0:
                break
            await asyncio.sleep(0.5)

        self.assertGreaterEqual(
            max(rss_samples) - min(rss_samples),
            8.0,
            "RSS did not reflect the memory workload's touched allocations",
        )

    async def test_uncertain_recheck_reschedules_after_gathering_new_observation(self):
        settings_store.update({
            "recheck_interval": 0.5,
            "minimum_prediction_confidence": 0.95,
        })
        managed = self.manager.start(WorkloadRequest(
            workload_type="healthy",
            policy="confidence_recheck",
            duration_s=40,
        ))
        await self.manager.sample_all()
        first = self.db.query(
            "SELECT next_recheck_at FROM managed_processes WHERE id=?",
            (managed["id"],),
        )[0]["next_recheck_at"]
        self.assertIsNotNone(first)
        await asyncio.sleep(0.6)
        await self.manager.sample_all()
        second = self.db.query(
            "SELECT next_recheck_at FROM managed_processes WHERE id=?",
            (managed["id"],),
        )[0]["next_recheck_at"]
        measurements = self.db.query(
            "SELECT COUNT(*) AS count FROM measurements WHERE process_id=?",
            (managed["id"],),
        )[0]["count"]
        self.assertGreater(second, first)
        self.assertGreaterEqual(measurements, 2)


if __name__ == "__main__":
    unittest.main()
