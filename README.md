# PreemptGuard

PreemptGuard is a local Operating Systems research prototype for observing **managed child processes**, estimating short-horizon failure risk from recent measurements, and comparing bounded recovery policies. It uses a Python/FastAPI backend, SQLite persistence, and a React/Vite dashboard.

> This is a student research prototype. The initial predictor produces an interpretable, uncalibrated **risk score**, not a calibrated probability. Workloads are launched as children by PreemptGuard; the application never adopts or terminates arbitrary system processes.

## Research question

Can lightweight, confidence-aware failure prediction and a risk-based recovery policy reduce unnecessary process restarts and application downtime compared with fixed-restart and threshold-based policies?

No performance advantage is assumed. The experiment runner records measured observations and reports unavailable metrics as `null`; it does not manufacture results.

## Operating Systems concepts

- Process creation and lifecycle management with `subprocess`.
- Process identifiers, CPU time, RSS memory, and process state using `psutil`.
- Heartbeats as a process responsiveness/health signal.
- Rolling-window monitoring and trend-based features.
- Failure detection, restart limits, cooldowns, and replacement-process verification.
- Persistent experiment and recovery records in SQLite.

## Architecture

```text
Managed simulator workers
          ↓
psutil sampler + heartbeat reader ──→ SQLite measurements
          ↓
Past-only feature extraction
          ↓
Interpretable risk score + evidence confidence
          ↓
Fixed / threshold / PreemptGuard decision policies
          ↓
Continue monitoring / recheck / bounded managed restart
          ↓
Heartbeat verification + decision and recovery history
          ↓
FastAPI REST API → React dashboard and experiment export
```

The backend is organized under `backend/app/`: API routes, configuration/database, monitoring/features, prediction, recovery, workload worker/manager, and experiment metrics are separate modules.

## Implemented functionality

- Locally launched `healthy`, `cpu`, `memory`, `unresponsive`, `crash`, `intermittent`, and `repeated_failure` child workloads. CPU busy work is time-sliced; simulated memory growth is capped (default 64 MB, configurable up to 256 MB).
- Measurements of PID, process CPU percentage, RSS memory in MB, memory percentage, process state, heartbeat age, uptime, and memory growth. CPU use comes from worker-reported process CPU time when available (with `psutil` as a fallback); RSS and memory percentage are reported by the worker because Windows parent-process sampling may not reflect its current working set.
- Rolling features: current/mean CPU, CPU trend, recent memory growth/acceleration, missed heartbeats, abnormal samples, and history length.
- A transparent rule-based risk score and an evidence-amount confidence estimate. A process that exits is assigned score 1.0. These scores are not calibrated probabilities.
- Optional logistic-regression prediction of a recorded exit or sustained heartbeat failure within the configured future horizon. The heuristic remains the default baseline; the model uses sigmoid calibration fitted on group-disjoint training folds, and no model is shipped.
- Fault-aware managed actions: retry a temporary simulated operation, throttle a sustained CPU workload, recheck uncertain/stale observations, or restart an unresponsive/failed managed worker. CPU throttling is a PreemptGuard action triggered by a three-sample CPU average of at least 35% sustained across three consecutive observations; it reduces the simulator worker's CPU duty cycle. Each decision records its reason and feature evidence. Actions are restricted to child processes launched by PreemptGuard.
- Heartbeat and worker-control handoffs use atomic file replacement with bounded retries for transient Windows file-sharing locks. Worker stderr is captured and attached to failure/recovery evidence instead of being silently discarded.
- Restart cooldown and maximum-attempt controls. Recovery verifies a new PID, running state, multiple fresh heartbeats, and health over the configured verification period; results are `healthy`, `failed`, or `inconclusive`.
- SQLite records for managed workloads, measurements, predictions/features, decisions, recovery attempts, experiment runs, and settings. Data persists in `.data\preemptguard.sqlite3`.
- API documentation at `/docs`; the dashboard polls actual API values every two seconds.
- Repeated comparisons of fixed-restart, threshold, and PreemptGuard, plus four ablation policies: threshold without prediction, predictive fixed threshold, confidence-aware recheck, and the complete policy. Results and CSV export include measured prediction, recovery, and monitoring-overhead metrics when observable.

## Limitations / incomplete research deliverables

This remains a local research prototype. It does not claim that PreemptGuard outperforms a baseline; use repeated measured runs to assess that question.

- Metrics are measured only for workloads started in the current backend session. Persisted old workload rows are visible but cannot be controlled after a server restart because process ownership handles are intentionally not reattached.
- The displayed confidence is `min(0.9, 0.25 + 0.065 × history_samples)`: it measures available history only, not calibrated probability or statistical confidence. Rechecks schedule a future observation and update the risk estimate; they do not change that confidence interpretation.
- The optional logistic-regression model applies sigmoid probability calibration using group-disjoint training folds. Brier score and expected calibration error are measured on repeated held-out runs, but do not guarantee calibration on new workloads. Evaluation uses repeated group holdouts, with non-overlapping test windows per run, and reports the heuristic baseline on the same test windows. The final model is fit on all eligible runs after evaluation.
- Experiment policies are independently randomized within each repetition; the seed and order are saved for reproducibility. Host scheduling can still confound results; increase repetitions and interpret differences cautiously. Summary standard deviations describe between-run spread, not confidence intervals.
- “Unnecessary restart” is only an operational proxy: a replacement remains responsive and failure-free throughout the configured post-recovery observation interval. It is not a counterfactual about the process that was stopped.
- Recovery duration and total downtime measure restart initiation through verification; they are not a full user-visible outage measurement from fault onset.
- CPU/RSS overhead is sampled from the whole backend process, including API and other work; it is not isolated monitor-only cost.
- Checkpoint-based rollback is not implemented. The simulator has no stable checkpoint/restore interface to support it safely.
- Settings cover sampler interval, horizon, thresholds, recheck delay, cooldown, restart limit, verification interval, memory cap, and active policy. Workload-specific CPU intensity and memory rates are currently set in the API schema rather than all being exposed in the dashboard.
- SQLite retention/cleanup is not automatic. Use the confirmed dashboard reset control only after stopping all active workloads.
- A browser is required for the React dashboard; no cloud service is used by the app. System-font fallbacks work offline.

## Requirements

- Windows 10/11
- Python 3.11+ (tested in this workspace with Python 3.13)
- Node.js 20+ and npm

## Windows setup and run

From PowerShell at the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

If you run tests directly from the repo root with `pytest`, the backend package is auto-configured so `app` resolves correctly.

Install the frontend dependencies:

```powershell
Set-Location frontend
npm install
```

Start the backend in a first terminal:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

If the venv is active, the shorter command is:

```powershell
Set-Location backend
py -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Start Vite in a second terminal:

```powershell
Set-Location frontend
npm run dev
```

Open <http://127.0.0.1:5173>. API documentation: <http://127.0.0.1:8000/docs>.
Stop both foreground servers with Ctrl+C.

For a production frontend build, run `npm run build` under `frontend`; then `GET /` serves the built dashboard from FastAPI. During development, use Vite at port 5173.

## Use the dashboard

1. Go to **Simulator** and select one of the eight managed workload types and a policy.
2. **Processes** shows actual PID, CPU, memory, memory growth, heartbeat health, latest risk score/confidence, and recommended action. Open details to see measurements, risk history, and decision reasons.
3. **Decisions** lists monitoring/recheck/restart decisions and recovery verification outcomes.
4. **Experiments** runs the same workload condition under five policies per repetition, shows the four-policy ablation, and displays available metrics. Export completed results as CSV.
5. **Settings** changes thresholds and recovery controls. Values are validated and saved in SQLite. The model control can train and activate logistic regression when enough labelled runs exist.

Manual API examples from PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/processes `
  -ContentType 'application/json' `
  -Body '{"workload_type":"healthy","policy":"preemptguard"}'
Invoke-RestMethod http://127.0.0.1:8000/api/processes
```

## Policies and cost-aware reasoning

| Policy | Implemented behavior |
| --- | --- |
| Fixed baseline | Continue monitoring until the managed process exits; restart only after confirmed failure. |
| Threshold baseline | Requires sustained high CPU/memory-growth samples or a stale heartbeat before recovery. |
| PreemptGuard | Uses history-derived risk and evidence confidence; rechecks moderate or under-observed cases, monitors healthy workloads, and restarts supported high-risk/unresponsive/failed managed workers. |

The initial decision engine compares transparent relative costs (not currency or universal real-world values): continued monitoring = `risk × 5 + (1 − confidence) × 3`; recheck = `1.2 + risk × 1.2`; restart = `2 + (1 − confidence) × 1.5`. A confirmed failure or heartbeat timeout follows the explicit recovery rule; otherwise the policy selects between these relative costs with the configured risk thresholds. The selected cost is recorded with each decision. Cost weights are not yet independently configurable or evaluated as a learned objective.

## Experiment metrics and interpretation

- **Prediction labels:** positive when a recorded exit or the beginning of three consecutive unresponsive samples occurs within the future horizon. Negative labels require measurements covering the full horizon; otherwise the window is censored. Features use only measurements available at the prediction timestamp.
- **Precision / recall / F1:** calculated over fully labelled prediction samples; when a denominator is zero but labels exist, the experiment uses the documented `zero_division=0` convention. **False-positive rate** is FP/(FP+TN). These per-sample counts are not independent because adjacent time windows overlap.
- **Prediction lead time:** time from the first qualifying warning to an observed failure event. It is unavailable when no true-positive warning is measured.
- **Recovery success rate:** verified healthy recoveries divided by all attempts. Recovery time runs from restart initiation through verification; total downtime is the sum of those durations.
- **Unnecessary restart:** counted only when the replacement is observed responsive and failure-free for the complete configured post-recovery interval. This is an operational proxy, not a counterfactual; unassessed attempts remain explicitly reported.
- **Monitoring overhead:** mean backend-process CPU percent and RSS memory over the recorded experiment interval. This includes API and other backend activity.

Experiments pair a healthy control with the selected failure scenario for every policy and repetition, with policy/scenario order randomized. The observation duration must exceed the prediction horizon by at least three sampling intervals; the failure must occur within the horizon. The measured-run table reports which predictor ran, logistic score count/mean, label coverage, and confusion counts alongside performance metrics. Metrics that are mathematically undefined remain unavailable rather than being fabricated. Each repetition contains ten workload runs, so allow for the added runtime.

Example PowerShell run with three complete repetitions across all five policies:

```powershell
$base = 'http://127.0.0.1:8000/api'
$started = Invoke-RestMethod -Method Post -Uri "$base/experiments" `
  -ContentType 'application/json' `
  -Body '{"workload_type":{"workload_type":"crash","fault_after_s":15},"duration_s":40,"repetitions":3,"randomization_seed":42,"prediction_horizon":30}'
$id = $started.experiment_id
do {
  Start-Sleep -Seconds 2
  $result = Invoke-RestMethod "$base/experiments/$id"
} while ($result.status -in @('queued', 'running'))
$result.results.comparison | Select-Object policy, repetition, metrics
Invoke-WebRequest "$base/experiments/$id/export.csv" -OutFile "preemptguard-$id.csv"
```

## Train and evaluate the optional model

No trained model is shipped. Generate training data by running at least eight completed managed workload runs, with both positive failure runs and negative healthy runs. For example, collect four `crash` runs and four `healthy` runs using the `fixed` policy, an observation duration of at least 35 seconds, and the default 30-second horizon. Ensure both labels occur in at least two separate runs; otherwise the grouped holdout will be rejected. The dashboard's Simulator supports these scenarios, or use the process API and leave each run monitored for the required duration. Then train and optionally activate logistic regression:

```powershell
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/api/settings/model/train?horizon_s=30&activate=true'
```

The trainer labels a window positive for an observed exit or three consecutive missed-heartbeat samples within the horizon; negative windows require complete future observation. It censors incomplete windows and samples recorded at or after observed unresponsiveness/failure. Repeated `GroupShuffleSplit` holdouts ensure complete workload runs never cross train/test within a split. Evaluation uses non-overlapping horizon windows and compares sigmoid-calibrated logistic regression with the heuristic on the same held-out windows. Calibration is fitted only inside the training portion using two group-disjoint folds. The report includes precision, recall, F1, false-positive rate, confusion counts, lead time, Brier score, expected calibration error, and variation across valid splits. It refuses inadequate data or fewer than five valid holdouts. The final model is fit on all eligible labelled runs after the holdout evaluation. Calibration is evaluated but may not generalize to new workloads; do not present the heuristic score as a probability or treat a local workload simulation as generalizable evidence. If the active model's horizon needs to change, retrain and activate it at that horizon.

## Tests

Run the PreemptGuard backend tests:

```powershell
Set-Location backend
py -m unittest discover -s tests -v
```
