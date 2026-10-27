import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

const API = "/api";
const pages = ["Overview", "Processes", "Simulator", "Decisions", "Experiments", "Settings"];

async function request(path, options) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function Metric({ label, value, detail }) {
  return <article className="metric"><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>;
}

function RiskBadge({ category }) {
  return <span className={`badge ${(category || "unknown").toLowerCase()}`}>{category || "Awaiting data"}</span>;
}

function App() {
  const [page, setPage] = useState("Overview");
  const [overview, setOverview] = useState(null);
  const [processes, setProcesses] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [recoveries, setRecoveries] = useState([]);
  const [settings, setSettings] = useState(null);
  const [modelStatus, setModelStatus] = useState(null);
  const [modelEvaluation, setModelEvaluation] = useState(null);
  const [selected, setSelected] = useState("");
  const [history, setHistory] = useState(null);
  const [workload, setWorkload] = useState("healthy");
  const [policy, setPolicy] = useState("preemptguard");
  const [experimentType, setExperimentType] = useState("crash");
  const [experimentDuration, setExperimentDuration] = useState(40);
  const [experimentRepetitions, setExperimentRepetitions] = useState(1);
  const [experimentSeed, setExperimentSeed] = useState(42);
  const [experimentHorizon, setExperimentHorizon] = useState(30);
  const [experiment, setExperiment] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [overviewData, processData, decisionData, recoveryData, settingsData, modelData] = await Promise.all([
        request("/overview"), request("/processes"), request("/decisions?limit=100"),
        request("/recovery?limit=100"), request("/settings"), request("/settings/model"),
      ]);
      setOverview(overviewData);
      setProcesses(processData);
      setDecisions(decisionData);
      setRecoveries(recoveryData);
      setSettings(settingsData);
      setModelStatus(modelData);
      if (modelData.training_report) {
        setModelEvaluation((current) => current || modelData.training_report);
      }
      setError("");
    } catch (reason) {
      setError(reason.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!selected) {
      setHistory(null);
      return undefined;
    }
    let active = true;
    const load = () => request(`/processes/${selected}/history`).then((value) => {
      if (active) setHistory(value);
    }).catch((reason) => { if (active) setError(reason.message); });
    load();
    const timer = window.setInterval(load, 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [selected]);

  const chartRows = useMemo(() => (overview?.recent_measurements || []).map((item) => ({
    ...item,
    time: new Date(item.timestamp * 1000).toLocaleTimeString(),
  })).reverse(), [overview]);

  async function run(action, success) {
    setBusy(true);
    setError("");
    try {
      await action();
      setNotice(success);
      await refresh();
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  }

  async function trainModel() {
    await run(async () => {
      const result = await request(
        `/settings/model/train?horizon_s=${settings?.prediction_horizon || 30}&activate=true`,
        { method: "POST" },
      );
      setModelEvaluation(result);
    }, "Group-held-out model training and evaluation completed.");
  }

  async function startWorkload(event) {
    event.preventDefault();
    await run(() => request("/processes", {
      method: "POST",
      body: JSON.stringify({ workload_type: workload, policy }),
    }), "Managed workload launched.");
  }

  async function startExperiment(event) {
    event.preventDefault();
    await run(async () => {
      const created = await request("/experiments", {
        method: "POST",
        body: JSON.stringify({
          workload_type: {
            workload_type: experimentType,
            fault_after_s: Math.min(15, Math.max(3, Number(experimentHorizon) - 1)),
          },
          duration_s: Number(experimentDuration),
          repetitions: Number(experimentRepetitions),
          randomization_seed: Number(experimentSeed),
          prediction_horizon: Number(experimentHorizon),
          sampling_interval: settings?.monitoring_interval || 1,
        }),
      });
      setExperiment({ experiment_id: created.experiment_id, status: created.status });
    }, "Experiment queued; the comparison will use live measurements.");
  }

  useEffect(() => {
    if (!experiment?.experiment_id || ["completed", "failed"].includes(experiment.status)) return undefined;
    let active = true;
    const poll = async () => {
      try {
        const next = await request(`/experiments/${experiment.experiment_id}`);
        if (active) setExperiment(next);
      } catch (reason) {
        if (active) setError(reason.message);
      }
    };
    const timer = window.setInterval(poll, 1500);
    poll();
    return () => { active = false; window.clearInterval(timer); };
  }, [experiment?.experiment_id, experiment?.status]);

  async function saveSettings(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const body = Object.fromEntries(form.entries());
    for (const key of ["monitoring_interval", "prediction_horizon", "low_risk_threshold",
      "high_risk_threshold", "recheck_interval", "restart_cooldown",
      "max_restart_attempts", "verification_period", "max_workload_memory_mb"]) {
      body[key] = Number(body[key]);
    }
    for (const key of ["max_operation_retries", "minimum_prediction_confidence"]) {
      body[key] = Number(body[key]);
    }
    await run(async () => setSettings(await request("/settings", {
      method: "PUT", body: JSON.stringify(body),
    })), "Settings saved.");
  }

  const chosenProcess = processes.find((item) => item.id === selected);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" onClick={() => setPage("Overview")}>
          <span className="brand-icon">P</span><span>PreemptGuard<small>PROCESS RECOVERY LAB</small></span>
        </a>
        <p className="nav-label">RESEARCH WORKSPACE</p>
        <nav>{pages.map((item, index) => (
          <button className={page === item ? "nav-item active" : "nav-item"} key={item} onClick={() => setPage(item)}>
            <span className="nav-number">0{index + 1}</span>{item}
          </button>
        ))}</nav>
        <div className="sidebar-note"><i /> Local monitor active<small>Only PreemptGuard-owned workloads can be restarted.</small></div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div><span className="eyebrow">OPERATING SYSTEMS · FAILURE RECOVERY</span><h1>{page}</h1></div>
          <div className="top-actions"><span className="live-label"><i /> LIVE LOCAL DATA</span><button className="button secondary" onClick={refresh}>Refresh</button></div>
        </header>
        {error && <div className="alert error" role="alert">{error}</div>}
        {notice && <div className="alert success" role="status">{notice}<button onClick={() => setNotice("")}>Dismiss</button></div>}

        {page === "Overview" && <section>
          <p className="lede">A measured, confidence-aware process recovery lab. Risk scores are transparent heuristic scores—not calibrated failure probabilities.</p>
          <div className="metric-grid">
            <Metric label="Managed processes" value={overview?.total_processes ?? "—"} detail="Launched and owned here" />
            <Metric label="Responsive now" value={overview?.healthy_processes ?? "—"} detail="Fresh workload heartbeat" />
            <Metric label="Moderate / high risk" value={overview?.moderate_or_high_risk ?? "—"} detail="Latest process estimates" />
            <Metric label="Confirmed exits" value={overview?.confirmed_failures ?? "—"} detail="Observed termination samples" />
            <Metric label="Verified recoveries" value={overview?.successful_recoveries ?? "—"} detail="Heartbeat verified after restart" />
            <Metric label="Unnecessary restarts" value={overview?.unnecessary_restarts ?? "Not measured"} detail="Per-run proxy is shown in experiment results" />
            <Metric label="Mean recovery" value={overview?.average_recovery_time_s == null ? "—" : `${overview.average_recovery_time_s.toFixed(2)}s`} detail="Observed recovery duration" />
            <Metric label="Measured downtime" value={`${(overview?.measured_downtime_s ?? 0).toFixed(2)}s`} detail="Sum of measured restart durations" />
          </div>
          <div className="panel chart-panel">
            <div className="panel-heading"><div><span className="eyebrow">MOST RECENT MEASUREMENT PER PROCESS</span><h2>Live CPU and memory observations</h2></div><span className="unit-label">CPU % · memory MB</span></div>
            <div className="chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={chartRows}><CartesianGrid strokeDasharray="3 3" vertical={false} /><XAxis dataKey="process_id" tickFormatter={(value) => value.slice(0, 6)} /><YAxis /><Tooltip labelFormatter={(label) => `Process ${label}`} /><Line dataKey="cpu_percent" name="CPU %" stroke="#3577d4" connectNulls dot={false} /><Line dataKey="memory_mb" name="Memory MB" stroke="#1a9b80" connectNulls dot={false} /></LineChart></ResponsiveContainer></div>
          </div>
        </section>}

        {page === "Processes" && <section>
          <p className="lede">Only child processes launched by this application are controllable. Metrics are sampled locally with psutil.</p>
          <div className="panel"><ProcessTable items={processes} onSelect={setSelected} onRun={run} busy={busy} /></div>
          {chosenProcess && <ProcessDetails process={chosenProcess} history={history} />}
        </section>}

        {page === "Simulator" && <section>
          <p className="lede">Start isolated, explicitly managed workloads. Memory growth is capped by the Settings limit; CPU work is bounded to short intervals.</p>
          <form className="panel form-panel" onSubmit={startWorkload}>
            <div className="form-grid">
              <label>Workload type<select value={workload} onChange={(event) => setWorkload(event.target.value)}>
                {["healthy", "cpu", "cpu_memory", "memory", "unresponsive", "crash", "intermittent", "repeated_failure", "temporary_io"].map((item) => <option key={item} value={item}>{item.replaceAll("_", " ")}</option>)}
              </select></label>
              <label>Recovery policy<select value={policy} onChange={(event) => setPolicy(event.target.value)}>
                <option value="preemptguard">PreemptGuard · predictive</option><option value="threshold">Threshold baseline</option><option value="fixed">Fixed-restart baseline</option>
              </select></label>
            </div>
            <p className="help-text">Default fault begins after 15 seconds; process and memory caps are enforced in the worker.</p>
            <button className="button" disabled={busy}>Start managed workload</button>
          </form>
          <div className="panel"><div className="panel-heading"><h2>Currently managed</h2></div><ProcessTable items={processes} onSelect={setSelected} onRun={run} busy={busy} /></div>
        </section>}

        {page === "Decisions" && <section>
          <p className="lede">Each row records the observed risk score, history-coverage estimate, chosen action, and reason.</p>
          <div className="panel table-wrap"><table><thead><tr><th>Time</th><th>Process</th><th>Risk score</th><th>History coverage</th><th>Decision</th><th>Relative cost</th><th>Reason / outcome</th></tr></thead>
            <tbody>{decisions.map((item) => <tr key={item.id}><td>{new Date(item.timestamp * 1000).toLocaleTimeString()}</td><td>{item.process_name}</td><td>{item.risk_score.toFixed(2)}</td><td>{(item.confidence * 100).toFixed(0)}%</td><td><span className="action-label">{item.action}</span></td><td>{Number(item.estimated_cost).toFixed(2)}</td><td>{item.reason}{item.outcome ? ` · ${item.outcome}` : ""}</td></tr>)}</tbody></table>
            {!decisions.length && <Empty>No decisions recorded yet.</Empty>}</div>
          <div className="panel"><div className="panel-heading"><h2>Recovery verification</h2></div><div className="table-wrap"><table><thead><tr><th>Process</th><th>Attempt</th><th>Old → new PID</th><th>Verification</th><th>Time</th></tr></thead><tbody>
            {recoveries.map((item) => <tr key={item.id}><td>{item.process_name}</td><td>{item.attempt_number}</td><td>{item.old_pid} → {item.new_pid}</td><td>{item.status}</td><td>{item.duration_s?.toFixed(2)}s</td></tr>)}
          </tbody></table>{!recoveries.length && <Empty>No recovery attempts yet.</Empty>}</div></div>
        </section>}

        {page === "Experiments" && <section>
          <p className="lede">Each policy is measured on both a healthy control and a failure scenario. The observation duration must exceed the prediction horizon so negative windows can be labelled. Metrics stay unavailable when the measured outcomes do not support them.</p>
          <form className="panel form-panel" onSubmit={startExperiment}>
            <div className="form-grid">
              <label>Failure scenario<select value={experimentType} onChange={(event) => setExperimentType(event.target.value)}>
                {["crash", "repeated_failure", "unresponsive", "intermittent"].map((item) => <option key={item} value={item}>{item.replaceAll("_", " ")}</option>)}
              </select></label>
              <label>Observation duration (seconds)<input type="number" min={Number(experimentHorizon) + 3 * (settings?.monitoring_interval || 1)} max="3600" value={experimentDuration} onChange={(event) => setExperimentDuration(event.target.value)} /></label>
              <label>Complete-run repetitions<input type="number" min="1" max="10" value={experimentRepetitions} onChange={(event) => setExperimentRepetitions(event.target.value)} /></label>
              <label>Policy-order randomization seed<input type="number" min="0" max="4294967295" value={experimentSeed} onChange={(event) => setExperimentSeed(event.target.value)} /></label>
              <label>Prediction horizon (seconds)<input type="number" min="4" max="600" value={experimentHorizon} onChange={(event) => {
                const horizon = Number(event.target.value);
                setExperimentHorizon(event.target.value);
                setExperimentDuration((duration) => Math.max(
                  Number(duration),
                  horizon + 3 * (settings?.monitoring_interval || 1),
                ));
              }} /></label>
            </div>
            <p className="help-text">Each repetition measures 10 runs (five policies × two workload conditions). A 40-second run with one repetition takes about 7 minutes. The selected logistic model is used for every prediction in the measured runs.</p>
            <button className="button" disabled={busy || experiment?.status === "running"}>Run randomized repeated comparison</button>
          </form>
          {experiment && <ExperimentResult experiment={experiment} />}
          <div className="panel"><div className="panel-heading"><h2>Experiment history</h2></div><ExperimentHistory onError={(reason) => setError(reason.message)} onSelect={(id) => request(`/experiments/${id}`).then(setExperiment).catch((reason) => setError(reason.message))} /></div>
        </section>}

        {page === "Settings" && settings && <section>
          <p className="lede">Settings are validated by the backend and persisted in SQLite. Change thresholds to test policy sensitivity.</p>
          <form className="panel form-panel" onSubmit={saveSettings}>
            <div className="settings-grid">
              {[
                ["monitoring_interval", "Sampling interval (seconds)", 0.25, 30, 0.25],
                ["prediction_horizon", "Prediction horizon (seconds)", 1, 600, 1],
                ["low_risk_threshold", "Low / moderate threshold", 0, 1, 0.01],
                ["high_risk_threshold", "Moderate / high threshold", 0, 1, 0.01],
                ["recheck_interval", "Recheck delay (seconds)", 0.5, 60, 0.5],
                ["restart_cooldown", "Restart cooldown (seconds)", 0, 3600, 1],
                ["max_restart_attempts", "Maximum restart attempts", 0, 20, 1],
                ["max_operation_retries", "Maximum temporary operation retries", 0, 10, 1],
                ["verification_period", "Verification period (seconds)", 0.5, 60, 0.5],
                ["max_workload_memory_mb", "Workload memory cap (MB)", 1, 256, 1],
                ["minimum_prediction_confidence", "Minimum history-confidence estimate", 0, 1, 0.01],
              ].map(([key, label, min, max, step]) => <label key={key}>{label}<input type="number" name={key} min={min} max={max} step={step} defaultValue={settings[key]} required /></label>)}
              <label>Active recovery policy<select name="active_policy" defaultValue={settings.active_policy}><option value="preemptguard">PreemptGuard</option><option value="threshold">Threshold baseline</option><option value="fixed">Fixed-restart baseline</option></select></label>
              <label>Prediction model<select name="prediction_model" defaultValue={settings.prediction_model}><option value="rule_based">Rule-based baseline</option><option value="logistic_regression" disabled={!modelStatus?.trained_model_available}>Trained logistic regression</option></select></label>
            </div>
            <p className="help-text">Confidence estimates the amount of recent history, not the probability that the prediction is correct. Logistic-regression outputs use sigmoid calibration fitted on grouped training folds; held-out Brier/ECE diagnostics do not guarantee calibration on new workloads.</p>
            <div className="button-row"><button className="button" disabled={busy}>Save settings</button><button type="button" className="button secondary" disabled={busy} onClick={trainModel}>Train and evaluate logistic regression</button><button type="button" className="button danger" onClick={() => {
              if (window.confirm("Permanently delete experiment history and stopped process records? All workloads must be stopped first.")) {
                run(() => request("/history/reset?confirm=true", { method: "POST" }), "History cleared.");
              }
            }}>Reset stored history</button></div>
            <p className="help-text">Active estimator: {modelStatus?.active_model || "rule_based"} · Trained model: {modelStatus?.trained_model_available ? "available" : "not trained"}</p>
            {modelEvaluation && <ModelEvaluation result={modelEvaluation} />}
          </form>
        </section>}
        <footer>PreemptGuard · local-only research prototype · rule scores are not calibrated probabilities.</footer>
      </main>
    </div>
  );
}

function ProcessTable({ items, onSelect, onRun, busy }) {
  if (!items.length) return <Empty>There are no managed workloads yet. Start one in the Simulator page.</Empty>;
  return <div className="table-wrap"><table><thead><tr><th>Process / PID</th><th>Workload</th><th>CPU</th><th>Memory</th><th>Growth</th><th>Health</th><th>Risk</th><th>Action</th><th>Controls</th></tr></thead>
    <tbody>{items.map((item) => {
      const metric = item.metrics;
      const prediction = item.prediction;
      return <tr key={item.id}>
        <td><button className="text-button" onClick={() => onSelect(item.id)}>{item.name}</button><small>PID {item.pid ?? "—"}</small></td>
        <td>{item.workload_type.replaceAll("_", " ")}</td>
        <td>{metric?.cpu_percent == null ? "—" : `${metric.cpu_percent.toFixed(1)}%`}</td>
        <td>{metric?.memory_mb == null ? "—" : `${metric.memory_mb.toFixed(1)} MB`}</td>
        <td>{metric?.memory_growth_mb_s == null ? "—" : `${metric.memory_growth_mb_s.toFixed(2)} MB/s`}</td>
        <td><span className={`health ${metric?.responsive ? "good" : "bad"}`}>{metric?.responsive ? "Responsive" : item.status}</span><small>{metric?.process_state || "unknown state"}</small></td>
        <td><RiskBadge category={prediction?.category} /><small>{prediction ? `${(prediction.risk_score * 100).toFixed(0)}% risk score · ${Math.round(prediction.confidence * 100)}% history coverage` : "Waiting for samples"}</small></td>
        <td>{prediction?.recommended_action || "—"}</td>
        <td className="controls-cell"><button className="mini-button" onClick={() => onSelect(item.id)}>Details</button>
          {item.managed && item.status !== "stopped" && <><button className="mini-button" disabled={busy} onClick={() => onRun(() => request(`/processes/${item.id}/restart`, { method: "POST" }), "Restart verified.")}>Restart</button><button className="mini-button" disabled={busy} onClick={() => onRun(() => request(`/processes/${item.id}/stop`, { method: "POST" }), "Workload stopped.")}>Stop</button></>}</td>
      </tr>;
    })}</tbody></table></div>;
}

function ProcessDetails({ process, history }) {
  const data = (history?.measurements || []).map((item) => ({
    ...item, at: new Date(item.timestamp * 1000).toLocaleTimeString(),
  }));
  return <div className="panel detail-panel"><div className="panel-heading"><div><span className="eyebrow">PROCESS DETAIL</span><h2>{process.name} · PID {process.pid || "—"}</h2></div><RiskBadge category={process.prediction?.category} /></div>
    <p className="help-text">Observed CPU/memory samples and predicted risk are separate series. Raw memory uses process RSS reported by psutil.</p>
    <div className="chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={data}><CartesianGrid strokeDasharray="3 3" vertical={false} /><XAxis dataKey="at" /><YAxis yAxisId="cpu" domain={[0, 100]} /><YAxis yAxisId="rss" orientation="right" domain={["auto", "auto"]} /><Tooltip /><Line yAxisId="cpu" dataKey="cpu_percent" name="Observed CPU %" stroke="#3577d4" dot={false} connectNulls /><Line yAxisId="rss" dataKey="memory_mb" name="Observed RSS MB" stroke="#1a9b80" dot={false} connectNulls /></LineChart></ResponsiveContainer></div>
    <div className="chart risk-chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={history?.predictions || []}><CartesianGrid strokeDasharray="3 3" vertical={false} /><XAxis dataKey="timestamp" tickFormatter={(value) => new Date(value * 1000).toLocaleTimeString()} /><YAxis domain={[0, 1]} /><Tooltip /><Line dataKey="risk_score" name="Predicted risk score" stroke="#d67836" dot={false} /></LineChart></ResponsiveContainer></div>
    <h3>Latest prediction explanation</h3><p>{process.prediction?.contributors?.join(", ") || "No factors available yet."}</p>
    <h3>Recent decisions</h3><ul className="compact-list">{(history?.decisions || []).slice(-5).reverse().map((item) => <li key={item.id}><b>{item.action}</b> — {item.reason}</li>)}</ul>
  </div>;
}

function ExperimentResult({ experiment }) {
  const runs = experiment.results?.comparison || [];
  const summaries = experiment.results?.policy_summary || [];
  return <div className="panel"><div className="panel-heading"><div><span className="eyebrow">MEASURED RUN</span><h2>{experiment.status} · {experiment.experiment_id}</h2></div>{experiment.status === "completed" && <a className="button link-button" href={`${API}/experiments/${experiment.experiment_id}/export.csv`}>Export CSV</a>}</div>
    {experiment.results?.randomization && <p className="help-text">Policy and scenario order was independently randomized per repetition (seed {experiment.results.randomization.seed}). The active predictor was {experiment.results.prediction_model || "not recorded"}.</p>}
    {summaries.length > 0 && <><h3>Across-repetition summary (mean ± standard deviation)</h3><div className="table-wrap"><table><thead><tr><th>Policy</th><th>Condition</th><th>Runs</th><th>F1</th><th>Precision</th><th>Recall</th><th>FPR</th><th>Recovery success</th><th>Downtime</th></tr></thead><tbody>
      {summaries.map((summary) => <tr key={`${summary.policy}-${summary.scenario}`}><td>{summary.policy}</td><td>{summary.scenario || "measured"}</td><td>{summary.run_count}</td><td>{formatSummaryMetric(summary.metrics.f1)}</td><td>{formatSummaryMetric(summary.metrics.precision)}</td><td>{formatSummaryMetric(summary.metrics.recall)}</td><td>{formatSummaryMetric(summary.metrics.false_positive_rate)}</td><td>{formatSummaryMetric(summary.metrics.recovery_success_rate)}</td><td>{formatSummaryMetric(summary.metrics.total_downtime_s)} s</td></tr>)}
    </tbody></table></div></>}
    {runs.length > 0 ? <div className="table-wrap"><table><thead><tr><th>Policy</th><th>Condition</th><th>Model samples / mean score</th><th>Labelled windows</th><th>TP / FP / FN / TN</th><th>Precision</th><th>Recall</th><th>F1</th><th>False-positive rate</th><th>First warning lead</th><th>Recovery success</th><th>Unnecessary restarts</th><th>Mean / median recovery</th><th>Downtime</th><th>Monitor CPU / RSS</th></tr></thead><tbody>
      {runs.map((run) => <tr key={run.process_id}><td>{run.policy}</td><td>{run.scenario || "measured"} · {run.workload_type.replaceAll("_", " ")}</td><td>{formatPredictorOutput(run.metrics)}</td><td>{run.metrics.labeled_prediction_samples} / {run.metrics.prediction_sample_count}</td><td>{run.metrics.true_positives} / {run.metrics.false_positives} / {run.metrics.false_negatives} / {run.metrics.true_negatives}</td><td>{formatMetric(run.metrics.precision)}</td><td>{formatMetric(run.metrics.recall)}</td><td>{formatMetric(run.metrics.f1)}</td><td>{formatMetric(run.metrics.false_positive_rate)}</td><td>{formatSeconds(run.metrics.first_early_warning_lead_time_s)}</td><td>{formatMetric(run.metrics.recovery_success_rate)}</td><td>{run.metrics.unnecessary_restarts == null ? `— (${run.metrics.unassessed_unnecessary_restart_count} unassessed)` : run.metrics.unnecessary_restarts}</td><td>{formatSeconds(run.metrics.mean_recovery_time_s)} / {formatSeconds(run.metrics.median_recovery_time_s)}</td><td>{formatSeconds(run.metrics.total_downtime_s)}</td><td>{formatMetric(run.metrics.cpu_overhead_percent)}% / {formatMetric(run.metrics.memory_overhead_mb)} MB</td></tr>)}
    </tbody></table></div> : <p className="help-text">Experiment is {experiment.status}. Results will appear when measured runs finish.</p>}
    {experiment.results?.ablation_study && <div className="ablation-list"><h3>Ablation variants</h3>{experiment.results.ablation_study.comparisons?.map((item) => <p key={item.policy}><b>{item.component}:</b> {item.runs.length} measured run(s), policy {item.policy}.</p>)}</div>}
    {runs.length > 0 && <p className="help-text">Prediction metrics use fully observed horizon windows. Precision, recall, and F1 use a zero_division=0 convention when labelled samples exist but the model makes no positive predictions; they remain unavailable only when there are no labelled windows. Recovery metrics require a measured recovery attempt. Unnecessary restarts require the full post-recovery observation interval. Overhead is sampled from the backend process. Policy means and standard deviations describe measured runs, not confidence intervals.</p>}
  </div>;
}

function ModelEvaluation({ result }) {
  const metrics = result.metrics || {};
  return <div className="model-evaluation">
    <h3>Repeated grouped-holdout evaluation · {result.model}</h3>
    <p>{result.dataset_rows} labelled windows from {result.run_count} runs ({result.positive_run_count} with positive and {result.negative_run_count} with negative labels). The final model uses {result.final_model_fit_run_count} runs; {result.cross_validation_test_run_count} distinct runs were held out across {metrics.valid_holdout_count} valid splits. Evaluation windows do not overlap within each run.</p>
    <p>Logistic regression F1 {formatMetric(metrics.grouped_holdout_mean?.f1)} ± {formatMetric(metrics.grouped_holdout_std?.f1)} · Rule baseline F1 {formatMetric(metrics.rule_based_baseline_grouped_holdout_mean?.f1)} ± {formatMetric(metrics.rule_based_baseline_grouped_holdout_std?.f1)}</p>
    <p>Precision {formatMetric(metrics.precision)} · Recall {formatMetric(metrics.recall)} · F1 {formatMetric(metrics.f1)} · FPR {formatMetric(metrics.false_positive_rate)} · Brier {formatMetric(metrics.brier_score)} · ECE {formatMetric(metrics.expected_calibration_error_10_bins)} · Mean lead {formatSeconds(metrics.mean_early_warning_lead_time_s)}</p>
    <p>{result.label_definition} A sigmoid calibrator is fitted using group-disjoint training folds. Brier score and expected calibration error are reported on held-out runs; these diagnostics do not guarantee calibration on new workloads.</p>
  </div>;
}

function ExperimentHistory({ onSelect, onError }) {
  const [items, setItems] = useState([]);
  useEffect(() => {
    request("/experiments").then(setItems).catch(onError);
    const timer = window.setInterval(() => request("/experiments").then(setItems).catch(onError), 3000);
    return () => window.clearInterval(timer);
  }, []);
  if (!items.length) return <Empty>No saved experiments yet.</Empty>;
  return <div className="table-wrap"><table><thead><tr><th>Created</th><th>ID</th><th>Status</th><th>Action</th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td>{new Date(item.created_at).toLocaleString()}</td><td>{item.id.slice(0, 10)}</td><td>{item.status}</td><td><button className="mini-button" onClick={() => onSelect(item.id)}>Open</button></td></tr>)}</tbody></table></div>;
}

function formatMetric(value) {
  return value == null ? "—" : Number(value).toFixed(3);
}
function formatSummaryMetric(metric) {
  if (!metric || metric.mean == null) return "—";
  return `${Number(metric.mean).toFixed(3)} ± ${Number(metric.std).toFixed(3)} (n=${metric.n})`;
}
function formatPredictorOutput(metrics) {
  if (!metrics.prediction_model_counts) return "Model details not recorded";
  const models = Object.entries(metrics.prediction_model_counts || {})
    .map(([name, count]) => `${name}: ${count}`)
    .join(", ");
  if (metrics.logistic_prediction_count > 0) {
    return `${models}; mean logistic score ${formatMetric(metrics.mean_logistic_risk_score)}`;
  }
  return models || "No predictions";
}
function formatSeconds(value) {
  return value == null ? "—" : `${Number(value).toFixed(2)}s`;
}
function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

export default App;
