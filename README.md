# PreemptGuard

PreemptGuard is a local Operating Systems monitoring prototype for observing visible processes on the current computer, estimating resource-pressure signals, and recommending safe recovery steps. It also retains an isolated simulator for evaluating managed child-process recovery policies. It uses a Python/FastAPI backend, SQLite persistence, and a React/Vite dashboard.

> Host processes are monitored locally. While a process is selected in Live Statistics, abrupt changes can trigger a recovery check. PreemptGuard asks before every host-process action and applies only one method per incident; it does not cycle through alternatives. Termination and restart may lose unsaved work. PreemptGuard-owned simulation workers use their separate recovery controls. Risk scores are heuristic triage indicators, not calibrated failure probabilities.

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

- The backend retains APIs for locally launched `healthy`, `cpu`, `memory`, `unresponsive`, `crash`, `intermittent`, and `repeated_failure` child workloads. CPU busy work is time-sliced; simulated memory growth is capped (default 64 MB, configurable up to 256 MB).
- **Computer Processes** lists process IDs and names visible to the current account. Search by process name or PID and select a PID to open its dedicated **Live Statistics** section. Process command lines and executable paths are not collected by monitoring; restricted system processes may have incomplete statistics.
- **Live Statistics** shows the selected process's current CPU, RSS memory, memory percentage, and growth, along with separate live CPU, RSS, memory-growth, and heuristic-risk graphs built from successive five-second snapshots.
- **Simulation** captures the selected host process's current metrics as a read-only baseline, then starts a separate, bounded PreemptGuard-owned worker for a selected healthy or fault scenario. Memory-growth simulations use the baseline's measured growth rate when available (capped at 4 MB/s); CPU and memory load start immediately, while crash/unresponsive/I/O fault behavior begins after the configured delay. Up to three workers can run concurrently. Expand any run for live measurements, CPU/RSS/growth/risk graphs, the decision and recovery timeline, and recorded samples. The original host process is never modified.
- **Overview** shows a graph of the highest host-process heuristic risk scores. Host-process scores are advisory signals, not failure probabilities. Optional model-guided recovery is available only for a process/action pair you explicitly approve.
- Measurements of PID, process CPU percentage, RSS memory in MB, memory percentage, process state, heartbeat age, uptime, and memory growth. CPU use comes from worker-reported process CPU time when available (with `psutil` as a fallback); RSS and memory percentage are reported by the worker because Windows parent-process sampling may not reflect its current working set.
- Rolling features: current/mean CPU, CPU trend, recent memory growth/acceleration, missed heartbeats, abnormal samples, and history length.
- A transparent rule-based risk score and an evidence-amount confidence estimate. A process that exits is assigned score 1.0. These scores are not calibrated probabilities.
- Rule-based risk scoring and an evidence-amount confidence estimate, plus a local online, case-based recovery selector trained from verified simulation recovery outcomes and user feedback on real-process actions. It will not recommend an action until at least five nearby outcomes exist and their weighted past success rate is at least 70%; this is a conservative filter, not a probability, guarantee, or validated accuracy claim.
- Fault-aware managed actions: retry a temporary simulated operation, throttle a sustained CPU workload, recheck uncertain/stale observations, or restart an unresponsive/failed managed worker. CPU throttling is a PreemptGuard action triggered by a three-sample CPU average of at least 35% sustained across three consecutive observations; it reduces the simulator worker's CPU duty cycle. Each decision records its reason and feature evidence. Actions are restricted to child processes launched by PreemptGuard.
- Heartbeat and worker-control handoffs use atomic file replacement with bounded retries for transient Windows file-sharing locks. Worker stderr is captured and attached to failure/recovery evidence instead of being silently discarded.
- Restart cooldown and maximum-attempt controls. Recovery verifies a new PID, running state, multiple fresh heartbeats, and health over the configured verification period; results are `healthy`, `failed`, or `inconclusive`.
- SQLite records for managed workloads, measurements, predictions/features, decisions, recovery attempts, experiment runs, and settings. Data persists in `.data\preemptguard.sqlite3`.
- Host-process samples are retained locally in SQLite for selected-PID history. Host-process names, PIDs, metrics, and recommendations are not sent to Ollama; only simulation-worker data and synthetic simulation history are included in optional AI analysis.
- Process samples, explicitly configured restart commands, action records, and user feedback are stored locally in `.data\preemptguard.sqlite3`. Approval is requested separately for every action and is not persisted. While a process is selected in Live Statistics, abrupt CPU increases (>=30 percentage points), memory increases (>=50 MB or 20%), memory growth crossing 5 MB/s, transition to stopped/zombie, or transition to High risk trigger a recovery check. A stopped process gets a direct resume recommendation. For other states, the learner selects one method from similar labeled outcomes when at least five nearby outcomes meet the success threshold, even if the risk score is below High. The dashboard asks before every action and never cycles through alternatives automatically. If there is no supported method, it takes no action and offers explicit manual choices. Recovery checks run only while Live Statistics is selected and live sampling is running; no host-process recovery runs in the background.
- Recovery actions on host processes: resume a stopped process; lower/restore process priority; terminate; restart only after configuring an explicit executable, argument list, and optional working directory (launched without a shell); or retry one failed recovery once. Termination/restart can lose unsaved work. Protected Windows processes and PreemptGuard-owned workers cannot be controlled through host recovery.
- Current host-process measurements provide the query context; action learning uses outcome-labelled examples only: verified simulation recoveries or the user's "helped / did not help" feedback. "Not sure" is saved but not treated as a training label. Raw unlabelled measurements alone cannot show whether a recovery helped. The bounded recent example set is updated continuously from SQLite; this is case-based learning, not model fine-tuning, and does not by itself prove improved precision.
- **Model Statistics** reports simulated risk-prediction precision/recall/accuracy using a 0.65 risk threshold and fully observed labels, plus recovery durations and labeled outcomes. Precision is undefined when no failures were predicted; the interface says so rather than substituting a score. Real-process feedback is reported separately from simulated results, and simulated recovery examples and real-process labels both feed the local recovery selector.
- Ollama is optional and runs locally. Configure `PREEMPTGUARD_OLLAMA_URL` (default `http://127.0.0.1:11434`) and `PREEMPTGUARD_OLLAMA_MODEL` (default `llama3.2`) before starting the backend. Install/start Ollama and pull the chosen model separately. Ollama only explains synthetic simulations; the separate local case-based selector handles learned recovery recommendations.
- API documentation at `/docs`; the dashboard polls actual API values every two seconds.
- Repeated comparisons of fixed-restart, threshold, and PreemptGuard, plus four ablation policies: threshold without prediction, predictive fixed threshold, confidence-aware recheck, and the complete policy. Results and CSV export include measured prediction, recovery, and monitoring-overhead metrics when observable.

## Limitations / incomplete research deliverables

This remains a local research prototype. It does not claim that PreemptGuard outperforms a baseline; use repeated measured runs to assess that question.

- System-process statistics are scoped to processes visible under the current Windows account and sampled while the backend is running. First CPU readings and memory-growth readings need a subsequent sample; access-restricted processes can have unavailable values. Host recovery is opt-in per action and process instance; actions can still fail due to Windows permissions or application behavior.
- Metrics are measured only for simulator workloads started in the current backend session. Persisted old workload rows are visible but cannot be controlled after a server restart because process ownership handles are intentionally not reattached.
- The displayed confidence is `min(0.9, 0.25 + 0.065 × history_samples)`: it measures available history only, not calibrated probability or statistical confidence. Rechecks schedule a future observation and update the risk estimate; they do not change that confidence interpretation.
- Ollama does not fine-tune or train a model from stored workloads. It performs local inference over selected simulation records and similar synthetic simulation history; it cannot change the learned host-process selector or the managed simulation recovery policy.
- Experiment policies are independently randomized within each repetition; the seed and order are saved for reproducibility. Host scheduling can still confound results; increase repetitions and interpret differences cautiously. Summary standard deviations describe between-run spread, not confidence intervals.
- “Unnecessary restart” is only an operational proxy: a replacement remains responsive and failure-free throughout the configured post-recovery observation interval. It is not a counterfactual about the process that was stopped.
- Recovery duration and total downtime measure restart initiation through verification; they are not a full user-visible outage measurement from fault onset.
- CPU/RSS overhead is sampled from the whole backend process, including API and other work; it is not isolated monitor-only cost.
- Checkpoint-based rollback is not implemented. The simulator has no stable checkpoint/restore interface to support it safely.
- Backend settings cover sampler interval, horizon, thresholds, recheck delay, cooldown, restart limit, verification interval, memory cap, and active policy. The dashboard exposes model statistics rather than a settings page; these controls retain their backend defaults and are used by simulations and experiment requests.
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

1. **Overview** shows the current visible-process count and the top resource-pressure signals.
2. **Computer Processes** lists process IDs and names. Select a PID to open **Live Statistics**.
3. **Live Statistics** shows the selected PID's current metrics and live CPU, RSS, memory-growth, and heuristic-risk graphs. Graphs need at least two samples and show up to the latest 60.
4. **Simulation** starts isolated healthy-control or fault-injection workers using the selected process as a read-only baseline. PreemptGuard monitors and attempts recovery only for these owned workers; you can stop them from the simulation results table.
5. **Model Statistics** reports measured risk-prediction precision from labeled simulation observations, recovery times by method, real-process helpfulness feedback, and the number of cases learned from simulations and actual processes. Empty or insufficient data is shown as not measured, never as a fabricated score. The model is a local case-based recovery selector plus a rule-based risk predictor; it does not fine-tune a neural network. The Simulation page reports local Ollama availability and offers advisory analysis for isolated simulation workers. Live Statistics detects sudden selected-process changes, requests one-time approval for recovery, records action outcomes, and provides restart configuration.

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

Experiments pair a healthy control with the selected failure scenario for every policy and repetition, with policy/scenario order randomized. The observation duration must exceed the prediction horizon by at least three sampling intervals; the failure must occur within the horizon. The measured-run table reports the rule-based risk estimator, label coverage, and confusion counts alongside performance metrics. Metrics that are mathematically undefined remain unavailable rather than being fabricated. Each repetition contains ten workload runs, so allow for the added runtime.

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

## Local Ollama simulation analysis

Install Ollama separately, start its local service, and pull a model (for example, `ollama pull llama3.2`). The backend defaults to `http://127.0.0.1:11434` and model `llama3.2`; override these with `PREEMPTGUARD_OLLAMA_URL` and `PREEMPTGUARD_OLLAMA_MODEL`. The dashboard reports whether the service and configured model are available.

The backend stores host-process samples and simulation history in `.data\preemptguard.sqlite3`. Host-process data stays local and is excluded from Ollama prompts, including the host-process baseline captured when a simulation starts. An analysis prompt contains only recorded metrics, decisions, recoveries, and similar cases for managed workers explicitly marked as synthetic simulations. Ollama analysis is not fine-tuning, is not a learned recovery policy, and cannot execute commands or trigger actions.

## Local continuous recovery learning

The local case-based selector compares a process's current CPU, memory, growth, and heuristic-risk measurements with past recovery outcomes. It learns from verified simulation restart outcomes and from the user's feedback after a real-process recovery. A "Not sure" response is retained but excluded from training. Unlabelled host samples help describe the current process state but are not treated as proof that any action worked.

The model updates its local outcome examples as soon as a new verified outcome or user label is saved. It considers actions only when at least five nearby outcomes exist and their weighted past success rate is at least 70%; otherwise it recommends no recovery action. This observed outcome rate is not a probability; the threshold is a guardrail, not a calibration guarantee. The project does not claim that continuous updates improve recovery precision; validate decisions with repeated tests before relying on them.

In **Live Statistics**, PreemptGuard detects sudden changes between samples for the selected process and compares the current sample with outcome-labeled cases. It asks for permission before every recovery attempt, executes only one selected method, and does not fall through to other methods after a failure. A stopped process is offered resume first; other methods are recommended from similar outcomes, with a manual choice available if the evidence is insufficient. Real-process action outcomes are added to learning only after you submit feedback. Restart requires an explicit executable and argument list; it is launched without a shell. Termination and restart can discard unsaved work. Protected Windows processes and PreemptGuard's own child workers are not eligible for this host-process control path.

## Tests

Run the PreemptGuard backend tests:

```powershell
Set-Location backend
py -m unittest discover -s tests -v
```
