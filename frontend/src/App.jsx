import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

const API = "/api";
const pages = ["Overview", "Computer Processes", "Live Statistics", "Simulation", "Model Statistics"];

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

function suddenChange(previous, current) {
  if (!previous || previous.pid !== current.pid
    || previous.process_started_at !== current.process_started_at) return null;
  if (previous.state !== "stopped" && current.state === "stopped") {
    return "Windows reports that the process stopped or was suspended.";
  }
  if (previous.state !== "zombie" && current.state === "zombie") {
    return "Windows reports that the process entered a zombie state.";
  }
  if (previous.cpu_percent != null && current.cpu_percent != null
    && current.cpu_percent - previous.cpu_percent >= 30) {
    return "CPU use rose by at least 30 percentage points between samples.";
  }
  if (previous.memory_mb != null && current.memory_mb != null) {
    const memoryIncrease = current.memory_mb - previous.memory_mb;
    if (memoryIncrease >= Math.max(50, previous.memory_mb * 0.2)) {
      return "Resident memory increased by at least 50 MB or 20% between samples.";
    }
  }
  if (previous.growth_mb_s != null && current.growth_mb_s != null
    && current.growth_mb_s >= 5 && previous.growth_mb_s < 5) {
    return "Resident memory growth rose above 5 MB per second.";
  }
  if (previous.risk_score != null && current.risk_score >= 0.65
    && previous.risk_score < 0.65) {
    return "The process risk score rose into the High range.";
  }
  return null;
}

function App() {
  const [page, setPage] = useState("Overview");
  const [systemProcesses, setSystemProcesses] = useState([]);
  const [systemProcessSampledAt, setSystemProcessSampledAt] = useState(null);
  const [systemProcessRefreshing, setSystemProcessRefreshing] = useState(false);
  const [systemProcessSearch, setSystemProcessSearch] = useState("");
  const [selectedSystemPid, setSelectedSystemPid] = useState(null);
  const [processHistoryByPid, setProcessHistoryByPid] = useState({});
  const [selectedLiveProcess, setSelectedLiveProcess] = useState(null);
  const [selectedLiveSampledAt, setSelectedLiveSampledAt] = useState(null);
  const [liveStatsRunning, setLiveStatsRunning] = useState(false);
  const [decisions, setDecisions] = useState([]);
  const [recoveries, setRecoveries] = useState([]);
  const [managedProcesses, setManagedProcesses] = useState([]);
  const [simulationFault, setSimulationFault] = useState("cpu");
  const [llmStatus, setLlmStatus] = useState(null);
  const [learningStatus, setLearningStatus] = useState(null);
  const [pendingFeedback, setPendingFeedback] = useState([]);
  const [hostActionContext, setHostActionContext] = useState(null);
  const [recoveryIncident, setRecoveryIncident] = useState(null);
  const [manualRecoveryAction, setManualRecoveryAction] = useState("");
  const [restartCommand, setRestartCommand] = useState({ executable: "", arguments: "[]", working_directory: "" });
  const [hostActionBusy, setHostActionBusy] = useState(false);
  const [feedbackBusyRun, setFeedbackBusyRun] = useState(null);
  const previousSelectedProcess = useRef(null);
  const promptedIncidents = useRef(new Set());
  const stoppedIncidentInstances = useRef(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [systemProcessData, decisionData, recoveryData, managedProcessData] = await Promise.all([
        request("/system/processes"), request("/decisions?limit=100"),
        request("/recovery?limit=100"), request("/processes"),
      ]);
      setSystemProcesses(systemProcessData.processes);
      setSystemProcessSampledAt(systemProcessData.sampled_at);
      setSystemProcessRefreshing(systemProcessData.refreshing);
      const selectedSample = systemProcessData.processes.find(
        (item) => item.pid === selectedSystemPid,
      );
      if (selectedSample) {
        const previous = previousSelectedProcess.current;
        const reason = page === "Live Statistics" && liveStatsRunning
          ? suddenChange(previous, selectedSample)
          : null;
        previousSelectedProcess.current = selectedSample;
        if (reason) {
          setRecoveryIncident({
            id: `${selectedSample.pid}:${selectedSample.process_started_at}:${systemProcessData.sampled_at}`,
            pid: selectedSample.pid,
            process_started_at: selectedSample.process_started_at,
            reason,
          });
        }
        setSelectedLiveProcess(selectedSample);
        setSelectedLiveSampledAt(systemProcessData.sampled_at);
      }
      setDecisions(decisionData);
      setRecoveries(recoveryData);
      setManagedProcesses(managedProcessData);
      setError("");
    } catch (reason) {
      setError(reason.message);
    }
  }, [liveStatsRunning, page, selectedSystemPid]);

  useEffect(() => {
    const process = selectedLiveProcess;
    if (page !== "Live Statistics" || !liveStatsRunning || !process?.process_started_at) {
      return undefined;
    }
    let active = true;
    const loadRecoveryContext = async () => {
      try {
        const query = `?process_started_at=${encodeURIComponent(process.process_started_at)}`;
        const incident = recoveryIncident?.pid === process.pid
          && recoveryIncident?.process_started_at === process.process_started_at;
        const [actions, recommendation] = await Promise.all([
          request(`/system/processes/${process.pid}/actions${query}`),
          request(`/system/processes/${process.pid}/recommendation${query}${incident ? "&sudden_change=true" : ""}`),
        ]);
        if (active) setHostActionContext({
          ...actions,
          recommendation,
          error: "",
          incidentId: incident ? recoveryIncident.id : null,
        });
      } catch (reason) {
        if (active) setHostActionContext({ error: reason.message });
      }
    };
    loadRecoveryContext();
    const timer = window.setInterval(loadRecoveryContext, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [
    page,
    liveStatsRunning,
    selectedLiveProcess?.pid,
    selectedLiveProcess?.process_started_at,
    recoveryIncident?.id,
  ]);

  useEffect(() => {
    const process = selectedLiveProcess;
    const action = hostActionContext?.recommendation?.recommendation;
    if (!process?.process_started_at
      || !recoveryIncident
      || recoveryIncident.pid !== process.pid
      || recoveryIncident.process_started_at !== process.process_started_at
      || hostActionContext?.incidentId !== recoveryIncident.id
      || !action
      || hostActionBusy) return;

    if (promptedIncidents.current.has(recoveryIncident.id)) return;
    promptedIncidents.current.add(recoveryIncident.id);

    const label = action.replaceAll("_", " ");
    if (window.confirm(
      `${process.name} (PID ${process.pid}): ${recoveryIncident.reason} `
      + `The best supported recovery from similar outcomes is to ${label}. `
      + "Apply this one method now? No alternative method will be tried automatically.",
    )) runHostRecovery(action, true);
    else {
      setNotice("Recovery was declined. No action was taken.");
      setRecoveryIncident(null);
    }
  }, [
    selectedLiveProcess?.pid,
    selectedLiveProcess?.process_started_at,
    hostActionContext?.recommendation?.recommendation,
    hostActionContext?.incidentId,
    recoveryIncident?.id,
    hostActionBusy,
  ]);

  useEffect(() => {
    const process = selectedLiveProcess;
    if (!process?.process_started_at) return;
    const instanceKey = `${process.pid}:${process.process_started_at}`;
    if (process.state !== "stopped") {
      stoppedIncidentInstances.current.delete(instanceKey);
      return;
    }
    if (page !== "Live Statistics" || !liveStatsRunning
      || stoppedIncidentInstances.current.has(instanceKey)
      || recoveryIncident?.pid === process.pid
        && recoveryIncident?.process_started_at === process.process_started_at) return;

    stoppedIncidentInstances.current.add(instanceKey);
    setRecoveryIncident({
      id: `${instanceKey}:stopped:${Date.now()}`,
      pid: process.pid,
      process_started_at: process.process_started_at,
      reason: "Windows reports that the selected process stopped or was suspended.",
    });
  }, [
    page,
    liveStatsRunning,
    selectedLiveProcess?.pid,
    selectedLiveProcess?.process_started_at,
    selectedLiveProcess?.state,
    recoveryIncident?.id,
  ]);

  useEffect(() => {
    if (!recoveryIncident || !hostActionContext?.incidentId
      || hostActionContext.incidentId !== recoveryIncident.id
      || recoveryIncident.noMethod
      || hostActionContext.recommendation?.recommendation
      || hostActionContext.recommendation?.setup_required_action) return;
    setNotice(
      `PreemptGuard detected a sudden change but found no proven recovery method: `
      + `${hostActionContext.recommendation?.reason || "insufficient similar outcomes"}. `
      + "No action was taken. Choose a method below only if you want to try one.",
    );
    setRecoveryIncident((current) => current && ({ ...current, noMethod: true }));
  }, [
    recoveryIncident?.id,
    hostActionContext?.incidentId,
    hostActionContext?.recommendation?.recommendation,
    hostActionContext?.recommendation?.setup_required_action,
  ]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    let active = true;
    const loadStatus = () => request("/llm/status").then((status) => {
      if (active) setLlmStatus(status);
    }).catch((reason) => {
      if (active) setLlmStatus({ available: false, error: reason.message });
    });
    loadStatus();
    const timer = window.setInterval(loadStatus, 15000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let active = true;
    const loadStatus = () => request("/learning/status")
      .then((status) => { if (active) setLearningStatus(status); })
      .catch((reason) => { if (active) setError(reason.message); });
    loadStatus();
    const timer = window.setInterval(loadStatus, 15000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let active = true;
    const loadPendingFeedback = () => request("/learning/pending-feedback")
      .then((pending) => { if (active) setPendingFeedback(pending); })
      .catch((reason) => { if (active) setError(reason.message); });
    loadPendingFeedback();
    const timer = window.setInterval(loadPendingFeedback, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!selectedSystemPid || !selectedLiveProcess) return undefined;
    let active = true;
    const loadHistory = async () => {
      try {
        const startedAt = selectedLiveProcess.process_started_at;
        const query = startedAt == null
          ? ""
          : `?process_started_at=${encodeURIComponent(startedAt)}&limit=60`;
        const history = await request(`/system/processes/${selectedSystemPid}/history${query}`);
        if (!active) return;
        const samples = history.map((item) => ({
          ...item,
          time: new Date(item.sampled_at * 1000).toLocaleTimeString(),
          risk_score: item.risk_score,
        }));
        setProcessHistoryByPid((current) => ({ ...current, [selectedSystemPid]: samples }));
        if (samples.length) setSelectedLiveSampledAt(samples[samples.length - 1].sampled_at);
      } catch (reason) {
        if (active) setError(reason.message);
      }
    };
    loadHistory();
    const timer = liveStatsRunning ? window.setInterval(loadHistory, 5000) : null;
    return () => {
      active = false;
      if (timer) window.clearInterval(timer);
    };
  }, [liveStatsRunning, selectedLiveProcess, selectedSystemPid]);

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

  async function runHostRecovery(action, approvalConfirmed = false) {
      if (!selectedLiveProcess?.process_started_at) return;
      if (!approvalConfirmed) {
        const labels = {
          resume: "resume this stopped process",
          lower_priority: "lower this process's CPU scheduling priority",
          restore_priority: "restore this process's saved original priority",
          terminate: "terminate this process (unsaved work may be lost)",
          restart_configured: "stop this process (which may lose unsaved work) and launch its configured replacement",
          retry: "retry its most recent failed recovery once",
        };
        if (!window.confirm(
          `PreemptGuard detected a change in the selected process. Allow it to `
          + `${labels[action]} now? This approval applies only to this one recovery attempt.`,
        )) {
          setNotice("Recovery was declined. No action was taken.");
          return;
        }
      }
      setHostActionBusy(true);
      setError("");
      try {
        await request(`/system/processes/${selectedLiveProcess.pid}/recovery`, {
          method: "POST",
          body: JSON.stringify({
            action,
            process_started_at: selectedLiveProcess.process_started_at,
            allow_action: true,
            remember_action: false,
          }),
        });
        setNotice(`Recovery action "${action}" was requested. Please record whether it helped.`);
        setRecoveryIncident(null);
        await refresh();
        const query = `?process_started_at=${encodeURIComponent(selectedLiveProcess.process_started_at)}`;
        const actions = await request(`/system/processes/${selectedLiveProcess.pid}/actions${query}`);
        const recommendation = await request(`/system/processes/${selectedLiveProcess.pid}/recommendation${query}`);
        setHostActionContext({ ...actions, recommendation, error: "" });
      } catch (reason) {
        setError(reason.message);
      } finally {
        setHostActionBusy(false);
      }
    }

  async function revokeSelectedApprovals(process = selectedLiveProcess) {
    if (!process?.process_started_at) return;
    const query = `?process_started_at=${encodeURIComponent(process.process_started_at)}`;
    const actions = await request(`/system/processes/${process.pid}/actions${query}`);
    await Promise.all(actions.approved_actions.map(({ action }) => request(
      `/system/processes/${process.pid}/consents/${action}${query}`,
      { method: "DELETE" },
    )));
  }

  async function changePage(nextPage) {
    if (page === "Live Statistics" && nextPage !== "Live Statistics") {
      try {
        await revokeSelectedApprovals();
      } catch (reason) {
        setError(reason.message);
      }
      if (selectedLiveProcess?.state === "stopped" && selectedLiveProcess.process_started_at) {
        stoppedIncidentInstances.current.delete(
          `${selectedLiveProcess.pid}:${selectedLiveProcess.process_started_at}`,
        );
      }
      setRecoveryIncident(null);
    }
    setPage(nextPage);
  }

  async function saveHostRestartConfig(event) {
      event.preventDefault();
      if (!selectedLiveProcess?.process_started_at) return;
      let args;
      try {
        args = JSON.parse(restartCommand.arguments);
        if (!Array.isArray(args) || args.some((item) => typeof item !== "string")) {
          throw new Error("Arguments must be a JSON array of strings.");
        }
      } catch (reason) {
        setError(reason.message || "Arguments must be a JSON array of strings.");
        return;
      }
      setHostActionBusy(true);
      setError("");
      try {
        await request(`/system/processes/${selectedLiveProcess.pid}/restart-config`, {
          method: "POST",
          body: JSON.stringify({
            process_started_at: selectedLiveProcess.process_started_at,
            executable: restartCommand.executable,
            arguments: args,
            working_directory: restartCommand.working_directory || null,
          }),
        });
        setNotice("Restart command saved locally for this process instance.");
        const query = `?process_started_at=${encodeURIComponent(selectedLiveProcess.process_started_at)}`;
        const actions = await request(`/system/processes/${selectedLiveProcess.pid}/actions${query}`);
        setHostActionContext((current) => ({ ...current, ...actions }));
      } catch (reason) {
        setError(reason.message);
      } finally {
        setHostActionBusy(false);
      }
    }

  async function submitHostActionFeedback(runId, helped, pid = selectedLiveProcess?.pid) {
      if (!pid || feedbackBusyRun != null) return;
      setFeedbackBusyRun(runId);
      try {
        await request(`/system/processes/${pid}/feedback`, {
          method: "POST",
          body: JSON.stringify({ action_run_id: runId, helped }),
        });
        setNotice("Your feedback was saved locally and added to online learning.");
        const [updatedLearning, updatedPending] = await Promise.all([
          request("/learning/status"),
          request("/learning/pending-feedback"),
        ]);
        setLearningStatus(updatedLearning);
        setPendingFeedback(updatedPending);
        if (selectedLiveProcess?.pid === pid) {
          const query = `?process_started_at=${encodeURIComponent(selectedLiveProcess.process_started_at)}`;
          const [actions, recommendation] = await Promise.all([
            request(`/system/processes/${pid}/actions${query}`),
            request(`/system/processes/${pid}/recommendation${query}`),
          ]);
          setHostActionContext({ ...actions, recommendation, error: "" });
        }
      } catch (reason) {
        setError(reason.message);
      } finally {
        setFeedbackBusyRun(null);
      }
    }
  async function revokeHostActionConsent(action) {
    if (!selectedLiveProcess?.process_started_at) return;
    try {
      const query = `?process_started_at=${encodeURIComponent(selectedLiveProcess.process_started_at)}`;
      await request(`/system/processes/${selectedLiveProcess.pid}/consents/${action}${query}`, {
        method: "DELETE",
      });
      setNotice(`Approval for ${action.replaceAll("_", " ")} was revoked.`);
      const actions = await request(`/system/processes/${selectedLiveProcess.pid}/actions${query}`);
      setHostActionContext((current) => ({ ...current, ...actions }));
    } catch (reason) {
      setError(reason.message);
    }
  }

  async function startSimulation(event) {
    event.preventDefault();
    if (!selectedLiveProcess) {
      setError("Select a process in Computer Processes before starting a simulation.");
      return;
    }
    const baseline = {
      pid: selectedLiveProcess.pid,
      name: selectedLiveProcess.name,
      cpu_percent: selectedLiveProcess.cpu_percent,
      memory_mb: selectedLiveProcess.memory_mb,
      memory_percent: selectedLiveProcess.memory_percent,
      growth_mb_s: selectedLiveProcess.growth_mb_s,
      process_started_at: selectedLiveProcess.process_started_at,
    };
    const workloadType = simulationFault;
    const memoryRate = Math.min(4, Math.max(0.1, Math.abs(baseline.growth_mb_s || 1)));
    await run(async () => {
      const created = await request("/processes", {
        method: "POST",
        body: JSON.stringify({
          workload_type: workloadType,
          name: `Sim PID ${baseline.pid} ${workloadType.replaceAll("_", " ")}`,
          policy: "preemptguard",
          duration_s: 120,
          fault_after_s: 8,
          memory_rate_mb_s: memoryRate,
          simulation: true,
          simulation_baseline: baseline,
        }),
      });
      if (!created.id) throw new Error("Simulation API did not return a worker ID.");
    }, "Isolated fault simulation started. The selected computer process was not changed.");
  }

  async function stopSimulation(processId) {
    await run(
      () => request(`/processes/${processId}/stop`, { method: "POST" }),
      "Simulation worker stopped.",
    );
  }

  async function resetStoredHistory() {
    if (!window.confirm(
      "Permanently delete simulation history, model evaluation data, and stored process feedback? "
      + "All workloads must be stopped first.",
    )) return;
    await run(async () => {
      await request("/history/reset?confirm=true", { method: "POST" });
      setLearningStatus(await request("/learning/status"));
    }, "Stored history cleared.");
  }

  const chosenSystemProcess = systemProcesses.find((item) => item.pid === selectedSystemPid);
  const filteredSystemProcesses = useMemo(() => {
    const query = systemProcessSearch.trim().toLocaleLowerCase();
    if (!query) return systemProcesses;
    return systemProcesses.filter((item) =>
      item.name.toLocaleLowerCase().includes(query)
      || String(item.pid).includes(query)
    );
  }, [systemProcessSearch, systemProcesses]);
  const riskChartRows = useMemo(() => systemProcesses
    .filter((item) => item.risk_score != null)
    .slice()
    .sort((left, right) => right.risk_score - left.risk_score)
    .slice(0, 20)
    .map((item) => ({
      ...item,
      chart_name: `${item.name.slice(0, 18)} · ${item.pid}`,
    })), [systemProcesses]);
  const simulationRows = useMemo(() => managedProcesses
    .filter((process) => process.config?.simulation === true)
    .map((process) => {
    const processId = process.id;
    const latestDecision = decisions.find((item) => item.process_id === processId);
    const recoveryAttempts = recoveries.filter((item) => item.process_id === processId);
    return {
      processId,
      workloadType: process.workload_type,
      baseline: process.config.simulation_baseline,
      startedAt: Date.parse(process.started_at),
      process,
      latestDecision,
      recoveryAttempts,
      metric: process?.metrics,
      prediction: process?.prediction,
    };
  }).filter((simulation) => simulation.baseline), [managedProcesses, decisions, recoveries]);
  const activeSimulationCount = simulationRows.filter(
    (item) => item.process?.managed && ["running", "recovering"].includes(item.process.status),
  ).length;

  async function selectSystemProcess(pid) {
    const process = systemProcesses.find((item) => item.pid === pid);
    if (selectedLiveProcess?.process_started_at) {
      try {
        await revokeSelectedApprovals(selectedLiveProcess);
      } catch (reason) {
        setError(reason.message);
        return;
      }
    }
    previousSelectedProcess.current = process;
    setHostActionContext(null);
    if (process?.state === "stopped") {
      stoppedIncidentInstances.current.add(`${process.pid}:${process.process_started_at}`);
    }
    setRecoveryIncident(process?.state === "stopped" ? {
      id: `${process.pid}:${process.process_started_at}:stopped:${Date.now()}`,
      pid: process.pid,
      process_started_at: process.process_started_at,
      reason: "Windows reports that the selected process is stopped or suspended.",
    } : null);
    setManualRecoveryAction("");
    setSelectedSystemPid(pid);
    setSelectedLiveProcess(process || null);
    setSelectedLiveSampledAt(systemProcessSampledAt);
    setLiveStatsRunning(true);
    setProcessHistoryByPid((history) => ({ ...history, [pid]: [] }));
    setPage("Live Statistics");
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" onClick={(event) => {
          event.preventDefault();
          changePage("Overview");
        }}>
          <span className="brand-icon">P</span><span>PreemptGuard<small>LOCAL PROCESS ADVISOR</small></span>
        </a>
        <p className="nav-label">MONITORING</p>
        <nav>{pages.map((item, index) => (
          <button className={page === item ? "nav-item active" : "nav-item"} key={item} onClick={() => changePage(item)}>
            <span className="nav-number">0{index + 1}</span>{item}
          </button>
        ))}</nav>
        <div className="sidebar-note"><i /> Local monitor active<small>Each recovery attempt needs your one-time approval in Live Statistics.</small></div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div><span className="eyebrow">OPERATING SYSTEMS · FAILURE RECOVERY</span><h1>{page}</h1></div>
          <div className="top-actions"><span className="live-label"><i /> LIVE LOCAL DATA</span><button className="button secondary" onClick={refresh}>Refresh</button></div>
        </header>
        {error && <div className="alert error" role="alert">{error}</div>}
        {notice && <div className="alert success" role="status">{notice}<button onClick={() => setNotice("")}>Dismiss</button></div>}
        {pendingFeedback.length > 0 && <div className="pending-feedback-list">
          <strong>Help PreemptGuard learn from a recent recovery</strong>
          {pendingFeedback.map((item) => <div key={item.action_run_id}>
            <span>{item.action.replaceAll("_", " ")} on {item.process_name} (PID {item.pid}) — did it help?</span>
            <div className="feedback-buttons">
              <button className="mini-button" disabled={feedbackBusyRun === item.action_run_id} onClick={() => submitHostActionFeedback(item.action_run_id, true, item.pid)}>Yes</button>
              <button className="mini-button" disabled={feedbackBusyRun === item.action_run_id} onClick={() => submitHostActionFeedback(item.action_run_id, false, item.pid)}>No</button>
              <button className="mini-button" disabled={feedbackBusyRun === item.action_run_id} onClick={() => submitHostActionFeedback(item.action_run_id, null, item.pid)}>Not sure</button>
            </div>
          </div>)}
        </div>}

        {page === "Overview" && <section>
          <p className="lede">Live resource monitoring for processes visible to this Windows account. Risk scores are heuristic indicators, not failure probabilities. Recovery uses labeled local outcomes, asks before every action, and never tries alternatives automatically.</p>
          <div className="metric-grid">
            <Metric label="Visible processes" value={systemProcesses.length} detail="Readable by this account" />
            <Metric label="Elevated resource use" value={systemProcesses.filter((item) => item.risk_score >= 0.3).length} detail="Heuristic triage signals" />
            <Metric label="High / critical risk" value={systemProcesses.filter((item) => item.risk_score >= 0.65).length} detail="Review process advice" />
            <Metric label="Limited access" value={systemProcesses.filter((item) => item.health === "Limited access").length} detail="Statistics restricted by Windows" />
          </div>
          <div className="panel chart-panel">
            <div className="panel-heading"><div><span className="eyebrow">TOP RESOURCE-PRESSURE SIGNALS</span><h2>Process risk score</h2></div><span className="unit-label">Heuristic score · not failure probability</span></div>
            <ProcessRiskChart items={riskChartRows} />
          </div>
        </section>}

        {page === "Computer Processes" && <section>
          <p className="lede">Choose a process by PID to open its live statistics and optional learned recovery controls. Real-process actions require process-specific approval; simulation workers remain separate.</p>
          <div className="panel process-picker-panel">
            <div className="panel-heading"><div><span className="eyebrow">PROCESS SELECTION</span><h2>Choose a PID</h2></div><span className="unit-label">{filteredSystemProcesses.length} processes</span></div>
            <label className="process-search">Filter by process name or PID<input type="search" value={systemProcessSearch} onChange={(event) => setSystemProcessSearch(event.target.value)} placeholder="e.g. chrome or 1234" /></label>
            <HostProcessTable items={filteredSystemProcesses} onSelect={selectSystemProcess} selectedPid={selectedSystemPid} refreshing={systemProcessRefreshing} />
          </div>
        </section>}

        {page === "Live Statistics" && <section>
          <p className="lede">Live measurements and graphs for the selected PID. New samples are collected every five seconds; graphs retain up to 60 samples while this page is open. Sudden changes trigger one recovery check; PreemptGuard asks before every action and never cycles through alternatives.</p>
          <div className="panel">
            <div className="panel-heading">
              <div><span className="eyebrow">SELECTED PROCESS · LIVE STATISTICS</span><h2>{selectedLiveProcess ? `${selectedLiveProcess.name} · PID ${selectedLiveProcess.pid}` : "No process selected"}</h2></div>
              <div className="live-stat-controls">
                {selectedLiveProcess && <><span className={liveStatsRunning ? "live-status running" : "live-status paused"}>{liveStatsRunning ? "LIVE" : "PAUSED"}</span><button className="button secondary" onClick={() => setLiveStatsRunning((running) => !running)}>{liveStatsRunning ? "Stop live stats" : "Resume live stats"}</button></>}
                {selectedLiveProcess && <RiskBadge category={selectedLiveProcess.risk_category} />}
              </div>
            </div>
            {selectedLiveProcess ? <SelectedProcessStats process={selectedLiveProcess} sampledAt={selectedLiveSampledAt} live={liveStatsRunning} /> : <Empty>Select a PID above to display its live statistics.</Empty>}
          </div>
          <div className="panel">
            <div className="panel-heading"><div><span className="eyebrow">SELECTED PID · SAMPLE HISTORY</span><h2>Live process graphs</h2></div><span className="unit-label">{liveStatsRunning ? "Up to 60 samples · refreshed every 5 seconds" : "Paused · showing collected samples"}</span></div>
            {selectedLiveProcess
              ? <SelectedProcessGraphs items={processHistoryByPid[selectedSystemPid] || []} />
              : <Empty>Select a PID above to display its live graphs.</Empty>}
          </div>
          {selectedLiveProcess && (
            recoveryIncident
            || ["High", "Critical"].includes(selectedLiveProcess.risk_category)
            || selectedLiveProcess.state === "stopped"
          ) && <div className="panel host-recovery-panel">
            <div className="panel-heading">
              <div><span className="eyebrow">{recoveryIncident ? "SUDDEN CHANGE DETECTED" : selectedLiveProcess.state === "stopped" ? "STOPPED PROCESS RECOVERY" : "HIGH-RISK RECOVERY"}</span><h2>Recovery recommendation</h2></div>
              <span className="unit-label">{learningStatus?.training_examples ?? 0} stored outcomes</span>
            </div>
            <p className="help-text">
              {recoveryIncident
                ? `${recoveryIncident.reason} PreemptGuard compares this sample with previously labeled outcomes. `
                  + "It will ask before every recovery and will not cycle through alternative methods."
                : selectedLiveProcess.state === "stopped"
                  ? "Windows reports that this process is stopped or suspended. Resume is the first recovery to try."
                  : "Learned recovery is offered only for High or Critical risk unless a sudden change is detected. "}
              Similar outcomes require at least five nearby cases with a weighted past success rate
              of 70% or more; this rate is not a probability or guarantee. Recovery applies only
              to the process selected here.
            </p>
            <div className="learned-recommendation">
              <strong>Recommendation:</strong>{" "}
              {hostActionContext?.recommendation?.recommendation
                ? hostActionContext.recommendation.recommendation.replaceAll("_", " ")
                : hostActionContext?.recommendation?.reason
                  || "Checking for a supported recovery method..."}
              {hostActionContext?.recommendation?.estimates?.map((item) => (
                <small key={item.action}>
                  {item.action.replaceAll("_", " ")}: {Math.round(item.estimated_success_rate * 100)}%
                  weighted past outcome rate from {item.similar_outcomes} similar cases (not a probability)
                </small>
              ))}
            </div>
            {hostActionContext?.error && <div className="alert error">{hostActionContext.error}</div>}
            {hostActionContext?.recommendation?.recommendation && <>
              <button
                  className="button secondary"
                  disabled={hostActionBusy}
                  onClick={() => runHostRecovery(hostActionContext.recommendation.recommendation)}
                >
                  Approve &amp; apply {hostActionContext.recommendation.recommendation.replaceAll("_", " ")}
                </button>
            </>}
            {recoveryIncident?.noMethod && !hostActionContext?.recommendation?.recommendation && (
              <div className="form-grid">
                <label>Choose one recovery method to try
                  <select value={manualRecoveryAction} onChange={(event) => setManualRecoveryAction(event.target.value)}>
                    <option value="">Select a method</option>
                    {selectedLiveProcess.state === "stopped" && <option value="resume">Resume process</option>}
                    <option value="lower_priority">Lower priority</option>
                    {hostActionContext?.restore_priority_available && <option value="restore_priority">Restore priority</option>}
                    {hostActionContext?.restart_configured && <option value="restart_configured">Configured restart</option>}
                    {hostActionContext?.runs?.some((item) => item.status === "failed") && <option value="retry">Retry one failed recovery</option>}
                    <option value="terminate">Terminate process</option>
                  </select>
                </label>
                <button
                  className={manualRecoveryAction === "terminate" ? "button danger" : "button secondary"}
                  disabled={!manualRecoveryAction || hostActionBusy}
                  onClick={() => runHostRecovery(manualRecoveryAction)}
                >Review and try selected method</button>
              </div>
            )}
            {hostActionContext?.recommendation?.setup_required_action === "restart_configured"
              && !hostActionContext?.restart_configured && (
                <form className="restart-config-form" onSubmit={saveHostRestartConfig}>
                  <h3>Configure the recommended restart method</h3>
                  <p className="help-text">Use an absolute executable path and JSON string-array arguments. No shell is used. Restart stops the selected process before launching the configured replacement.</p>
                  <label>Executable path<input required value={restartCommand.executable} onChange={(event) => setRestartCommand((current) => ({ ...current, executable: event.target.value }))} placeholder="C:\\Path\\To\\Application.exe" /></label>
                  <label>Arguments as JSON array<input value={restartCommand.arguments} onChange={(event) => setRestartCommand((current) => ({ ...current, arguments: event.target.value }))} placeholder='["--example"]' /></label>
                  <label>Working directory (optional)<input value={restartCommand.working_directory} onChange={(event) => setRestartCommand((current) => ({ ...current, working_directory: event.target.value }))} placeholder="C:\\Path\\To\\Application" /></label>
                  <button className="button secondary" disabled={hostActionBusy}>Save restart settings for this process instance</button>
                </form>
              )}
            {hostActionContext?.runs?.length > 0 && <div className="host-action-history">
              <h3>Recovery actions and outcomes</h3>
              {hostActionContext.runs.map((runItem) => <article key={runItem.id}>
                <div><b>{runItem.action.replaceAll("_", " ")}</b><small>{runItem.details?.message || runItem.details?.error || runItem.status} · {new Date(runItem.created_at * 1000).toLocaleString()}</small></div>
                {runItem.status === "requested" && !runItem.feedback_recorded
                  ? <div className="feedback-buttons">
                    <span>Did it help?</span>
                    <button className="mini-button" disabled={feedbackBusyRun === runItem.id} onClick={() => submitHostActionFeedback(runItem.id, true)}>Yes</button>
                    <button className="mini-button" disabled={feedbackBusyRun === runItem.id} onClick={() => submitHostActionFeedback(runItem.id, false)}>No</button>
                    <button className="mini-button" disabled={feedbackBusyRun === runItem.id} onClick={() => submitHostActionFeedback(runItem.id, null)}>Not sure</button>
                  </div>
                  : runItem.feedback_recorded && <span className="unit-label">Feedback recorded</span>}
              </article>)}
            </div>}
          </div>}
        </section>}

        {page === "Simulation" && <section>
          <p className="lede">Use the selected computer process as a read-only resource baseline, then inject a fault into a separate PreemptGuard-owned worker. The original process is never modified. Workers use the PreemptGuard recovery policy and are automatically bounded by their duration and memory limits.</p>
          {selectedLiveProcess ? <div className="panel">
            <div className="panel-heading"><div><span className="eyebrow">READ-ONLY BASELINE</span><h2>{selectedLiveProcess.name} · PID {selectedLiveProcess.pid}</h2></div><RiskBadge category={selectedLiveProcess.risk_category} /></div>
            <div className="metric-grid selected-process-metrics">
              <Metric label="CPU" value={selectedLiveProcess.cpu_percent == null ? "Unavailable" : `${selectedLiveProcess.cpu_percent.toFixed(1)}%`} detail="Selected process sample" />
              <Metric label="RSS memory" value={selectedLiveProcess.memory_mb == null ? "Unavailable" : `${selectedLiveProcess.memory_mb.toFixed(1)} MB`} detail="Used as simulation reference" />
              <Metric label="Memory growth" value={selectedLiveProcess.growth_mb_s == null ? "Warming up" : `${selectedLiveProcess.growth_mb_s.toFixed(2)} MB/s`} detail="Memory fault rate follows this, within configured bounds" />
            </div>
          </div> : <div className="panel"><Empty>Select a process in Computer Processes to use its current measurements as the simulation baseline.</Empty></div>}
          <form className="panel form-panel" onSubmit={startSimulation}>
            <div className="panel-heading"><div><span className="eyebrow">ISOLATED FAULT INJECTION</span><h2>Start a simulation process</h2></div></div>
            <div className="form-grid">
              <label>Fault scenario<select value={simulationFault} onChange={(event) => setSimulationFault(event.target.value)}>
                {[
                  ["healthy", "Healthy control"],
                  ["cpu", "High CPU"],
                  ["memory", "Memory growth"],
                  ["cpu_memory", "CPU and memory growth"],
                  ["unresponsive", "Unresponsive"],
                  ["crash", "Crash"],
                  ["intermittent", "Intermittent unresponsive"],
                  ["repeated_failure", "Repeated failure"],
                  ["temporary_io", "Temporary I/O failure"],
                ].map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select></label>
              <label>Recovery policy<input value="PreemptGuard (automatic)" readOnly /></label>
            </div>
            <p className="help-text">Each worker runs for up to 120 seconds. Crash, unresponsive, intermittent, repeated-failure, and temporary-I/O behavior starts after 8 seconds; CPU and memory-load scenarios start immediately. Memory growth follows the selected process's measured growth rate when available, capped at 4 MB/s. Up to three workers can run concurrently.</p>
            <button className="button" disabled={busy || !selectedLiveProcess || activeSimulationCount >= 3}>Start isolated simulation</button>
            {activeSimulationCount >= 3 && <p className="help-text">Stop or wait for an active worker before starting another. A maximum of three simulations may run at once.</p>}
          </form>
          <div className="panel">
            <div className="panel-heading"><div><span className="eyebrow">SIMULATED WORKERS</span><h2>Health, risk, and recovery outcomes</h2></div><span className="unit-label">{simulationRows.length} simulation(s) this session</span></div>
            <div className="simulation-llm-status">
              <span className={llmStatus?.available && llmStatus?.model_installed ? "live-status running" : "live-status paused"}>
                {llmStatus?.available && llmStatus?.model_installed ? "OLLAMA READY" : "OLLAMA UNAVAILABLE"}
              </span>
              <span>{llmStatus?.available
                ? llmStatus.model_installed
                  ? `Local model: ${llmStatus.configured_model}`
                  : `Pull the configured model (${llmStatus.configured_model}) in Ollama to enable analysis.`
                : llmStatus?.error || "Checking the local Ollama service…"}</span>
              <small>Only synthetic simulation metrics and local simulation history are sent to Ollama. Actual computer-process data stays local.</small>
            </div>
            <SimulationRunsTable items={simulationRows} busy={busy} llmStatus={llmStatus} onStop={stopSimulation} />
          </div>
        </section>}

        {page === "Model Statistics" && <section>
          <p className="lede">Measured evaluation from saved simulation runs and labeled real-process recovery feedback. Metrics are shown only when there is enough recorded evidence; this is a local case-based learner, not a neural-network model.</p>
          {learningStatus ? <>
            <div className="metric-grid model-metric-grid">
              <Metric
                label="Simulated risk-prediction precision"
                value={learningStatus.prediction_evaluation.precision == null
                  ? learningStatus.prediction_evaluation.labeled_prediction_samples
                    ? "No positive predictions"
                    : "Not measured"
                  : `${(learningStatus.prediction_evaluation.precision * 100).toFixed(1)}%`}
                detail={`${learningStatus.prediction_evaluation.true_positives} correct / ${learningStatus.prediction_evaluation.true_positives + learningStatus.prediction_evaluation.false_positives} predicted failures`}
              />
              <Metric
                label="Labeled simulation predictions"
                value={learningStatus.prediction_evaluation.labeled_prediction_samples}
                detail={`${learningStatus.prediction_evaluation.evaluated_runs} simulation runs evaluated`}
              />
              <Metric
                label="Mean simulated recovery time"
                value={learningStatus.recovery_performance.simulations.mean_duration_s == null
                  ? "Not measured"
                  : `${learningStatus.recovery_performance.simulations.mean_duration_s.toFixed(2)} s`}
                detail={`${learningStatus.recovery_performance.simulations.duration_sample_count} timed recovery attempts`}
              />
              <Metric
                label="Mean real-process action time"
                value={learningStatus.recovery_performance.actual_processes.mean_duration_s == null
                  ? "Not measured"
                  : `${learningStatus.recovery_performance.actual_processes.mean_duration_s.toFixed(3)} s`}
                detail="Time to issue/complete the OS action, not app restart readiness"
              />
            </div>

            <div className="panel">
              <div className="panel-heading">
                <div><span className="eyebrow">MODEL DETAILS</span><h2>{learningStatus.model}</h2></div>
                <span className="unit-label">Local SQLite · {learningStatus.storage}</span>
              </div>
              <p>{learningStatus.model_type}. {learningStatus.description}</p>
              <p className="help-text">The recovery selector compares the current process metrics with previous outcomes. It recommends only after at least five nearby outcomes and a weighted historical helpful rate of at least 70%. The risk predictor is rule-based; the precision shown below is evaluated separately on simulated, labeled data.</p>
              <div className="metric-grid model-source-grid">
                <Metric
                  label="Real-process training outcomes"
                  value={learningStatus.training_sources.actual_processes}
                  detail="User-labeled helpful / not helpful actions"
                />
                <Metric
                  label="Simulation training outcomes"
                  value={learningStatus.training_sources.simulated_recoveries}
                  detail="Verified healthy / failed recovery attempts"
                />
                <Metric
                  label="Total recovery training cases"
                  value={learningStatus.training_examples}
                  detail="Both sources used by the local selector"
                />
              </div>
            </div>

            <div className="panel">
              <div className="panel-heading">
                <div><span className="eyebrow">RISK PREDICTION EVALUATION</span><h2>Simulation-only measured precision</h2></div>
                <span className="unit-label">
                  Risk ≥ {learningStatus.prediction_evaluation.positive_threshold.toFixed(2)}
                  {learningStatus.prediction_evaluation.prediction_horizon_s == null
                    ? ""
                    : ` · ${learningStatus.prediction_evaluation.prediction_horizon_s}s horizon`}
                </span>
              </div>
              <p className="help-text">{learningStatus.prediction_evaluation.scope}. Only failures/healthy periods with enough recorded follow-up are included. Neighboring samples are correlated, not independent trials; this is not a guarantee of real-world accuracy.</p>
              <div className="metric-grid model-source-grid">
                <Metric label="Accuracy" value={learningStatus.prediction_evaluation.accuracy == null ? "Not measured" : `${(learningStatus.prediction_evaluation.accuracy * 100).toFixed(1)}%`} detail="Share of all labeled predictions correct" />
                <Metric label="True positives" value={learningStatus.prediction_evaluation.true_positives} detail="Failure correctly flagged" />
                <Metric label="False positives" value={learningStatus.prediction_evaluation.false_positives} detail="Risk flagged without labeled failure" />
                <Metric label="False negatives" value={learningStatus.prediction_evaluation.false_negatives} detail="Failure not flagged" />
                <Metric label="True negatives" value={learningStatus.prediction_evaluation.true_negatives} detail="Healthy period correctly not flagged" />
                <Metric label="Recall" value={learningStatus.prediction_evaluation.recall == null ? "Not measured" : `${(learningStatus.prediction_evaluation.recall * 100).toFixed(1)}%`} detail="Share of labeled failures detected" />
                <Metric label="F1 score" value={learningStatus.prediction_evaluation.f1 == null ? "Not measured" : `${(learningStatus.prediction_evaluation.f1 * 100).toFixed(1)}%`} detail="Combined precision and recall" />
              </div>
            </div>

            <div className="panel">
              <div className="panel-heading">
                <div><span className="eyebrow">RECOVERY PERFORMANCE</span><h2>Time and observed outcomes by method</h2></div>
              </div>
              <p className="help-text">Simulation time includes the recovery verification period. Real-process times measure the OS recovery action itself; effectiveness is counted only when an outcome is labeled.</p>
              {learningStatus.recovery_performance.by_action.length
                ? <div className="table-wrap"><table className="model-stats-table"><thead><tr><th>Method</th><th>Attempts</th><th>Labeled outcomes</th><th>Observed success</th><th>Mean time</th><th>Median time</th></tr></thead>
                  <tbody>{learningStatus.recovery_performance.by_action.map((item) => <tr key={item.action}>
                    <td>{item.action.replaceAll("_", " ")}</td>
                    <td>{item.attempts}</td>
                    <td>{item.labeled_outcomes}</td>
                    <td>{item.success_rate == null ? "Not measured" : `${(item.success_rate * 100).toFixed(1)}%`}</td>
                    <td>{item.mean_duration_s == null ? "—" : `${item.mean_duration_s.toFixed(2)} s`}</td>
                    <td>{item.median_duration_s == null ? "—" : `${item.median_duration_s.toFixed(2)} s`}</td>
                  </tr>)}</tbody></table></div>
                : <Empty>No timed recovery methods have been recorded yet.</Empty>}
              <p className="help-text">Real-process feedback: {learningStatus.actual_feedback.helpful} helpful and {learningStatus.actual_feedback.not_helpful} not helpful from {learningStatus.actual_feedback.labeled_outcomes} labeled outcomes
                {learningStatus.actual_feedback.helpfulness_rate == null
                  ? "."
                  : ` · ${(learningStatus.actual_feedback.helpfulness_rate * 100).toFixed(1)}% marked helpful.`}
              </p>
            </div>

            <div className="button-row">
              <button className="button danger" disabled={busy} onClick={resetStoredHistory}>Reset stored history</button>
            </div>
          </> : <Empty>Loading locally stored model statistics…</Empty>}
        </section>}
        <footer>PreemptGuard · local-only research prototype · learned recovery estimates are not guarantees.</footer>
      </main>
    </div>
  );
}

function HostProcessTable({ items, onSelect, selectedPid, refreshing }) {
  if (!items.length) return <Empty>{refreshing ? "Scanning visible processes…" : "No visible processes match this filter."}</Empty>;
  return <div className="table-wrap"><table className="pid-table"><thead><tr><th>PID</th><th>Process name</th><th>Selection</th></tr></thead>
    <tbody>{items.map((item) => <tr key={`${item.pid}-${item.name}`} className={selectedPid === item.pid ? "selected-row" : ""}>
      <td><strong className="pid-value">{item.pid}</strong></td>
      <td>{item.name}{item.managed_by_preemptguard && <small>PreemptGuard-owned</small>}</td>
      <td><button className="mini-button" aria-pressed={selectedPid === item.pid} onClick={() => onSelect(item.pid)}>{selectedPid === item.pid ? "Selected" : "View live stats"}</button></td>
    </tr>)}</tbody></table></div>;
}

function SimulationRunsTable({ items, busy, llmStatus, onStop }) {
  if (!items.length) return <Empty>No fault simulations have been started in this session.</Empty>;
  const faultLabels = {
    healthy: "Healthy control",
    cpu: "High CPU",
    memory: "Memory growth",
    cpu_memory: "CPU + memory growth",
    unresponsive: "Unresponsive",
    crash: "Crash",
    intermittent: "Intermittent unresponsive",
    repeated_failure: "Repeated failure",
    temporary_io: "Temporary I/O failure",
  };
  return <div className="table-wrap"><table><thead><tr>
    <th>Simulation / baseline PID</th><th>Injected fault</th><th>Worker health</th><th>Risk score</th><th>PreemptGuard response</th><th>Recovery result</th><th>Control</th>
  </tr></thead><tbody>{items.map((item) => <SimulationRunRow
    key={item.processId}
    item={item}
    faultLabel={faultLabels[item.workloadType] || item.workloadType}
    busy={busy}
    llmStatus={llmStatus}
    onStop={onStop}
  />)}</tbody></table></div>;
}

function SimulationRunRow({ item, faultLabel, busy, llmStatus, onStop }) {
  const [expanded, setExpanded] = useState(false);
  const [history, setHistory] = useState(null);
  const [detailError, setDetailError] = useState("");
  const process = item.process;
  const isActive = process?.managed && ["running", "recovering"].includes(process.status);
  const responsive = item.metric?.responsive;
  const health = responsive === 1 || responsive === true
    ? "Healthy / responsive"
    : process?.status === "failed" ? "Failed"
      : responsive === 0 || responsive === false ? "Unresponsive"
        : process?.status || "Starting";
  const risk = item.prediction?.risk_score;
  const latestRecovery = item.recoveryAttempts[0];

  useEffect(() => {
    if (!expanded) return undefined;
    let active = true;
    const loadHistory = async () => {
      try {
        const result = await request(`/processes/${item.processId}/history?limit=300`);
        if (active) {
          setHistory(result);
          setDetailError("");
        }
      } catch (reason) {
        if (active) setDetailError(reason.message);
      }
    };
    loadHistory();
    const timer = isActive ? window.setInterval(loadHistory, 2000) : null;
    return () => {
      active = false;
      if (timer) window.clearInterval(timer);
    };
  }, [expanded, isActive, item.processId]);

  return <>
    <tr>
      <td>{process?.name || `PID ${item.baseline.pid} simulation`}<small>Reference: {item.baseline.name} · PID {item.baseline.pid}</small></td>
      <td>{faultLabel}<small>Isolated worker · PID {process?.pid ?? "Starting"}</small></td>
      <td><span className={`health ${health === "Healthy / responsive" ? "good" : health === "Failed" || health === "Unresponsive" ? "bad" : ""}`}>{health}</span></td>
      <td>{risk == null ? "Waiting for prediction" : <><RiskBadge category={item.prediction.category} /><small>{(risk * 100).toFixed(0)}% risk score</small></>}</td>
      <td>{item.latestDecision
        ? <><b>{item.latestDecision.action}</b><small>{item.latestDecision.reason}{item.latestDecision.outcome ? ` · ${item.latestDecision.outcome}` : ""}</small></>
        : "Monitoring; no response recorded yet"}</td>
      <td>{latestRecovery
        ? <>{latestRecovery.status}<small>{latestRecovery.duration_s == null ? "Duration unavailable" : `${latestRecovery.duration_s.toFixed(2)}s`} · {latestRecovery.reason}</small></>
        : item.workloadType === "healthy" ? "Control run; recovery not expected" : "No recovery attempt yet"}</td>
      <td>
        <button className="mini-button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded ? "Hide details" : "View details"}
        </button>
        {isActive && <button className="mini-button" disabled={busy} onClick={() => onStop(item.processId)}>Stop worker</button>}
      </td>
    </tr>
    {expanded && <tr className="simulation-detail-row"><td colSpan={7}>
      {detailError && <div className="alert error" role="alert">{detailError}</div>}
      {history
        ? <SimulationRunDetails item={item} history={history} isActive={isActive} llmStatus={llmStatus} />
        : !detailError && <Empty>Loading simulation details…</Empty>}
    </td></tr>}
  </>;
}

function SimulationRunDetails({ item, history, isActive, llmStatus }) {
  const [analysis, setAnalysis] = useState(null);
  const [analysisError, setAnalysisError] = useState("");
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const measurements = [...history.measurements].sort((a, b) => a.timestamp - b.timestamp);
  const predictions = [...history.predictions].sort((a, b) => a.timestamp - b.timestamp);
  const events = [
    ...history.decisions.map((decision) => ({
      id: `decision-${decision.id}`,
      timestamp: decision.timestamp,
      type: `PreemptGuard: ${decision.action}`,
      detail: `${decision.reason}${decision.outcome ? ` · ${decision.outcome}` : ""}`,
    })),
    ...history.recoveries.map((recovery) => ({
      id: `recovery-${recovery.id}`,
      timestamp: recovery.timestamp,
      type: `Recovery: ${recovery.status}`,
      detail: `${recovery.reason} · ${recovery.duration_s == null ? "duration unavailable" : `${recovery.duration_s.toFixed(2)}s`}`,
    })),
  ].sort((a, b) => a.timestamp - b.timestamp);
  const delayedFault = ["unresponsive", "crash", "intermittent", "repeated_failure", "temporary_io"]
    .includes(item.workloadType);
  const startedAt = Number.isFinite(item.startedAt) ? item.startedAt : Date.parse(item.process.started_at);
  const faultStart = delayedFault ? startedAt + 8000 : startedAt;

  async function requestAnalysis() {
    setAnalysisBusy(true);
    setAnalysisError("");
    try {
      setAnalysis(await request(`/llm/simulations/${item.processId}/analysis`, { method: "POST" }));
    } catch (reason) {
      setAnalysisError(reason.message);
    } finally {
      setAnalysisBusy(false);
    }
  }

  return <div className="simulation-details">
    <div className="panel-heading">
      <div><span className="eyebrow">SIMULATION DETAIL</span><h2>{item.baseline.name} baseline · PID {item.baseline.pid}</h2></div>
      <span className={isActive ? "live-status running" : "live-status paused"}>{isActive ? "UPDATING LIVE" : "RUN COMPLETE"}</span>
    </div>
    <div className="simulation-detail-summary">
      <Metric label="Injected fault" value={item.workloadType.replaceAll("_", " ")} detail={`${delayedFault ? "Fault behavior begins after 8 seconds" : "Workload behavior begins immediately"} · worker PID ${item.process?.pid ?? "starting"}`} />
      <Metric label="Reference CPU" value={item.baseline.cpu_percent == null ? "Unavailable" : `${item.baseline.cpu_percent.toFixed(1)}%`} detail="Captured before simulation" />
      <Metric label="Reference RSS" value={item.baseline.memory_mb == null ? "Unavailable" : `${item.baseline.memory_mb.toFixed(1)} MB`} detail="Captured before simulation" />
      <Metric label="Measurements" value={measurements.length} detail="Recorded worker samples" />
      <Metric label="Risk predictions" value={predictions.length} detail="Recorded PreemptGuard predictions" />
      <Metric label="Fault start" value={new Date(faultStart).toLocaleTimeString()} detail="Relative to simulation start" />
    </div>
    <h3>Worker measurements and risk history</h3>
    {measurements.length || predictions.length
      ? <SimulationHistoryCharts measurements={measurements} predictions={predictions} />
      : <Empty>Waiting for the first worker measurement and prediction.</Empty>}
    <h3>What PreemptGuard has done</h3>
    {events.length
      ? <ol className="simulation-event-list">{events.map((event) => <li key={event.id}>
        <time>{new Date(event.timestamp * 1000).toLocaleTimeString()}</time><b>{event.type}</b><span>{event.detail}</span>
      </li>)}</ol>
      : <Empty>No policy action or recovery has been recorded yet; the worker is being monitored.</Empty>}
    <div className="simulation-analysis-heading">
      <div><h3>Local Ollama analysis</h3><p className="help-text">Uses synthetic worker samples and similar local simulation cases. This provides advice only; the PreemptGuard recovery policy remains in control.</p></div>
      <button className="button secondary" disabled={analysisBusy || !llmStatus?.available || !llmStatus?.model_installed} onClick={requestAnalysis}>
        {analysisBusy ? "Analyzing…" : analysis ? "Refresh analysis" : "Analyze simulation"}
      </button>
    </div>
    {analysisError && <div className="alert error" role="alert">{analysisError}</div>}
    {analysis && <div className="simulation-analysis-result">
      <p>{analysis.analysis}</p>
      <small>Model: {analysis.model} · Similar prior simulation cases: {analysis.similar_case_count}</small>
    </div>}
    <h3>Recorded measurements</h3>
    {measurements.length
      ? <div className="table-wrap"><table className="simulation-history-table"><thead><tr><th>Time</th><th>Worker status</th><th>Responsive</th><th>CPU</th><th>RSS memory</th><th>Growth</th><th>Heartbeat age</th></tr></thead><tbody>
        {measurements.slice().reverse().map((measurement) => <tr key={measurement.id}>
          <td>{new Date(measurement.timestamp * 1000).toLocaleTimeString()}</td>
          <td>{measurement.process_state}</td>
          <td>{measurement.responsive ? "Yes" : "No"}</td>
          <td>{measurement.cpu_percent == null ? "—" : `${measurement.cpu_percent.toFixed(1)}%`}</td>
          <td>{measurement.memory_mb == null ? "—" : `${measurement.memory_mb.toFixed(1)} MB`}</td>
          <td>{measurement.memory_growth_mb_s == null ? "—" : `${measurement.memory_growth_mb_s.toFixed(2)} MB/s`}</td>
          <td>{measurement.heartbeat_age_s == null ? "—" : `${measurement.heartbeat_age_s.toFixed(1)}s`}</td>
        </tr>)}
      </tbody></table></div>
      : <Empty>No measurements recorded.</Empty>}
  </div>;
}

function SimulationHistoryCharts({ measurements, predictions }) {
  const measurementsWithTime = measurements.map((item) => ({
    ...item,
    time: new Date(item.timestamp * 1000).toLocaleTimeString(),
  }));
  const predictionsWithTime = predictions.map((item) => ({
    ...item,
    time: new Date(item.timestamp * 1000).toLocaleTimeString(),
  }));
  return <div className="process-chart-grid">
    <article className="process-chart-card"><h3>Worker CPU</h3>
      <SimulationLineChart items={measurementsWithTime} dataKey="cpu_percent" color="#3577d4" unit="%" />
    </article>
    <article className="process-chart-card"><h3>Worker RSS memory</h3>
      <SimulationLineChart items={measurementsWithTime} dataKey="memory_mb" color="#1a9b80" unit=" MB" />
    </article>
    <article className="process-chart-card"><h3>Memory growth</h3>
      <SimulationLineChart items={measurementsWithTime} dataKey="memory_growth_mb_s" color="#8758b5" unit=" MB/s" />
    </article>
    <article className="process-chart-card"><h3>PreemptGuard risk score</h3>
      <SimulationLineChart items={predictionsWithTime} dataKey="risk_score" color="#d67836" unit="%" percent />
    </article>
  </div>;
}

function SimulationLineChart({ items, dataKey, color, unit, percent = false }) {
  const data = items.filter((item) => item[dataKey] != null);
  if (data.length < 2) return <Empty>{data.length ? "Collecting another sample…" : "No samples available."}</Empty>;
  return <div className="chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={data}>
    <CartesianGrid strokeDasharray="3 3" vertical={false} />
    <XAxis dataKey="time" minTickGap={24} />
    <YAxis
      domain={percent ? [0, 1] : ["auto", "auto"]}
      tickFormatter={(value) => percent ? `${Math.round(value * 100)}%` : `${value}${unit}`}
      width={58}
    />
    <Tooltip formatter={(value) => [percent ? `${(Number(value) * 100).toFixed(0)}%` : `${Number(value).toFixed(2)}${unit}`, dataKey]} />
    <Line dataKey={dataKey} stroke={color} dot={false} connectNulls />
  </LineChart></ResponsiveContainer></div>;
}

function SelectedProcessStats({ process, sampledAt, live }) {
  const value = (amount, suffix = "") => amount == null ? "Unavailable" : `${amount.toFixed(2)}${suffix}`;
  return <>
    <div className="metric-grid selected-process-metrics">
      <Metric label="CPU usage" value={value(process.cpu_percent, "%")} detail="Per-process CPU may exceed 100% across cores" />
      <Metric label="Resident memory" value={value(process.memory_mb, " MB")} detail="Current RSS" />
      <Metric label="System memory share" value={value(process.memory_percent, "%")} detail="Percentage of total RAM" />
      <Metric label="Memory growth" value={value(process.growth_mb_s, " MB/s")} detail="Since the previous sample" />
      <Metric label="Health" value={process.health} detail={`Process state: ${process.state}`} />
      <Metric label="Risk score" value={process.risk_score == null ? "Unavailable" : `${(process.risk_score * 100).toFixed(0)}%`} detail="Heuristic resource-pressure score, not failure probability" />
    </div>
    <p className="help-text">{live ? sampledAt ? `Last sample: ${new Date(sampledAt * 1000).toLocaleTimeString()}.` : "Waiting for the first process sample." : "Live updates are paused; displayed statistics are from the last sample."} Restricted processes may not expose every statistic. Pausing the display does not pause backend monitoring or any previously approved automatic recovery.</p>
  </>;
}

function SelectedProcessGraphs({ items }) {
  const charts = [
    { key: "cpu_percent", title: "CPU usage", color: "#3577d4", suffix: "%" },
    { key: "memory_mb", title: "Resident memory (RSS)", color: "#1a9b80", suffix: " MB" },
    { key: "growth_mb_s", title: "Memory growth", color: "#8758b5", suffix: " MB/s" },
    { key: "risk_score", title: "Heuristic risk score", color: "#d67836", suffix: "%", percent: true },
  ];
  return <div className="process-chart-grid">{charts.map((chart) => {
    const data = items.filter((item) => item[chart.key] != null);
    return <article className="process-chart-card" key={chart.key}>
      <h3>{chart.title}</h3>
      {data.length < 2
        ? <Empty>{data.length ? "Collecting another sample to draw this graph…" : "This statistic is unavailable for the selected process."}</Empty>
        : <div className="chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="time" minTickGap={24} />
          <YAxis
            domain={chart.percent ? [0, 1] : ["auto", "auto"]}
            tickFormatter={(value) => chart.percent ? `${Math.round(value * 100)}%` : `${value}${chart.suffix}`}
            width={58}
          />
          <Tooltip formatter={(value) => [
            chart.percent ? `${(Number(value) * 100).toFixed(0)}%` : `${Number(value).toFixed(2)}${chart.suffix}`,
            chart.title,
          ]} />
          <Line dataKey={chart.key} name={chart.title} stroke={chart.color} dot={false} connectNulls />
        </LineChart></ResponsiveContainer></div>}
    </article>;
  })}</div>;
}

function ProcessRiskChart({ items }) {
  if (!items.length) return <Empty>Risk scores are unavailable until process samples are collected.</Empty>;
  return <div className="chart risk-chart"><ResponsiveContainer width="100%" height="100%"><BarChart data={items} layout="vertical" margin={{ left: 12, right: 24 }}>
    <CartesianGrid strokeDasharray="3 3" horizontal={false} />
    <XAxis type="number" domain={[0, 1]} tickFormatter={(value) => `${Math.round(value * 100)}%`} />
    <YAxis type="category" dataKey="chart_name" width={180} tick={{ fontSize: 11 }} />
    <Tooltip formatter={(value) => [`${(Number(value) * 100).toFixed(0)}%`, "Heuristic risk score"]} />
    <Bar dataKey="risk_score" name="Heuristic risk score">
      {items.map((item) => <Cell key={item.pid} fill={item.risk_score >= 0.65 ? "#d04b52" : item.risk_score >= 0.3 ? "#d68a36" : "#3577d4"} />)}
    </Bar>
  </BarChart></ResponsiveContainer></div>;
}

function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

export default App;
