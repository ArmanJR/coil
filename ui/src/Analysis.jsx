import { useEffect, useMemo, useRef, useState } from "react";
import { api, logger } from "./api.js";
import {
  cohortKey,
  controllerStats,
  csvExport,
  filterRuns,
  finite,
  leaderboard,
  progress,
  roadKey,
} from "./analysis.js";
import "./analysis.css";

const metrics = {
  total_cost: {
    label: "Challenge score",
    unit: "",
    note: "Original acceleration target, scored frames only. Lower is better.",
  },
  tracking_rmse: {
    label: "Tracking RMSE",
    unit: "m/s²",
    note: "Acceleration tracking error over the scored window. Lower is better.",
  },
  jerk_rms: {
    label: "Jerk RMS",
    unit: "m/s³",
    note: "Lateral acceleration smoothness over the scored window. Lower is better.",
  },
  path_rmse_m: {
    label: "Path RMSE",
    unit: "m",
    note: "Full-ride kinematic projection, including unscored frames. Lower is better.",
  },
  max_path_offset_m: {
    label: "Maximum path offset",
    unit: "m",
    note: "Largest absolute projected offset over the full ride. Lower is better.",
  },
};
const colors = [
  "#8ab8ff",
  "#61d4ad",
  "#f2bb72",
  "#d59bff",
  "#f58c9c",
  "#6ed4e3",
];
const fmt = (value, digits = 2) =>
  finite(value)
    ? value.toLocaleString(undefined, { maximumFractionDigits: digits })
    : "—";
const date = (value) => new Date(value).toLocaleString();
const roadLabel = (r) =>
  `${r.source === "demo" ? "Demo" : "Dataset"} · ${r.segment}`;
const cohortLabel = (r) =>
  `${roadLabel(r)} · ${fmt(r.total_frames, 0)} frames / ${fmt(r.scored_frames, 0)} scored`;

function Metric({ label, value, note }) {
  return (
    <div className="analysis-metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </div>
  );
}

function ProgressChart({ rows, metric, selected, onSelect }) {
  if (!rows.length)
    return (
      <p className="analysis-empty">
        No saved measurements for this road and metric.
      </p>
    );
  const names = [...new Set(rows.map((r) => r.controller))].sort();
  const highest = Math.max(...rows.map((r) => r[metric]), 0.001) * 1.12;
  const x = (i) =>
    rows.length === 1 ? 450 : 65 + (i / (rows.length - 1)) * 795;
  const y = (value) => 225 - (value / highest) * 195;
  return (
    <>
      <div className="analysis-chart-wrap">
        <svg
          viewBox="0 0 900 270"
          className="analysis-chart"
          role="group"
          aria-label={`${metrics[metric].label} by completion order. Select a run to inspect it.`}
        >
          {[0, 0.25, 0.5, 0.75, 1].map((fraction) => (
            <g key={fraction}>
              <line
                x1="65"
                x2="860"
                y1={y(highest * fraction)}
                y2={y(highest * fraction)}
                className="chart-grid"
              />
              <text x="55" y={y(highest * fraction) + 4} textAnchor="end">
                {fmt(highest * fraction, 1)}
              </text>
            </g>
          ))}
          {names.map((name, ci) => (
            <polyline
              key={name}
              fill="none"
              stroke={colors[ci % colors.length]}
              strokeWidth="2"
              points={rows
                .map((r, i) =>
                  r.controller === name ? `${x(i)},${y(r[metric])}` : null,
                )
                .filter(Boolean)
                .join(" ")}
            />
          ))}
          {rows.map((run, i) => (
            <circle
              key={run.run_fingerprint}
              cx={x(i)}
              cy={y(run[metric])}
              r={selected === run.run_fingerprint ? 8 : 5}
              fill={colors[names.indexOf(run.controller) % colors.length]}
              stroke="#101216"
              strokeWidth="2"
              tabIndex="0"
              role="button"
              aria-label={`${run.controller}, ${date(run.completed_at_utc)}, ${metrics[metric].label} ${fmt(run[metric])}. Inspect run.`}
              onClick={() => onSelect(run)}
              onKeyDown={(event) => {
                if (["Enter", " "].includes(event.key)) {
                  event.preventDefault();
                  onSelect(run);
                }
              }}
            >
              <title>
                {run.controller} · {date(run.completed_at_utc)} ·{" "}
                {fmt(run[metric])} {metrics[metric].unit}
              </title>
            </circle>
          ))}
          <text x="65" y="253">
            First: {date(rows[0].completed_at_utc)}
          </text>
          {rows.length > 1 && (
            <text x="860" y="253" textAnchor="end">
              Latest: {date(rows.at(-1).completed_at_utc)}
            </text>
          )}
        </svg>
      </div>
      <div className="analysis-legend">
        {names.map((name, i) => {
          const own = rows.filter((r) => r.controller === name);
          const first = own[0][metric],
            latest = own.at(-1)[metric];
          const change =
            first > 0 && own.length > 1
              ? ((latest - first) / first) * 100
              : null;
          return (
            <span key={name}>
              <i style={{ background: colors[i % colors.length] }} />
              {name}
              <small>
                {own.length === 1
                  ? "1 run · no trend yet"
                  : `${own.length} runs · ${change === null ? "change unavailable" : `${fmt(Math.abs(change), 1)}% ${change <= 0 ? "lower" : "higher"} vs first`}`}
              </small>
            </span>
          );
        })}
      </div>
      <p className="analysis-caption">
        Completion order, equally spaced. Lines connect runs of the same
        controller on the same road and run length; they do not imply continuous
        improvement. Select a point for details.
      </p>
    </>
  );
}

const detailGroups = [
  [
    "Challenge performance · scored window",
    [
      ["total_cost", "Total score", ""],
      ["lataccel_cost", "Acceleration cost", ""],
      ["jerk_cost", "Jerk cost", ""],
      ["tracking_rmse", "Tracking RMSE", "m/s²"],
      ["max_tracking_error", "Maximum tracking error", "m/s²"],
      ["jerk_rms", "Jerk RMS", "m/s³"],
    ],
  ],
  [
    "Projected path · full ride",
    [
      ["path_frames", "Measured frames", ""],
      ["path_rmse_m", "Path RMSE", "m"],
      ["max_path_offset_m", "Maximum absolute offset", "m"],
      ["final_path_offset_m", "Final signed offset", "m"],
      ["max_heading_error_deg", "Maximum heading error", "°"],
      ["final_heading_error_deg", "Final signed heading error", "°"],
    ],
  ],
  [
    "Execution & coverage",
    [
      ["total_frames", "Total frames", ""],
      ["active_frames", "Active frames", ""],
      ["scored_frames", "Scored frames", ""],
      ["simulated_seconds", "Simulated ride", "s"],
      ["elapsed_seconds", "Wall time, including pauses", "s"],
      ["processing_seconds", "Processing time", "s"],
      ["mean_step_ms", "Mean step latency", "ms"],
      ["max_step_ms", "Maximum step latency", "ms"],
      ["max_abs_steering", "Maximum absolute steering", "units"],
    ],
  ],
  [
    "Remote decisions",
    [
      ["api_requests", "Paid API requests", ""],
      ["cache_hits", "Cache hits", ""],
      ["api_cost_usd", "Reported API cost", "USD"],
    ],
  ],
];

function RunDetails({ run, onClose, detailRef }) {
  if (!run) return null;
  return (
    <section
      className="analysis-panel run-details"
      ref={detailRef}
      tabIndex="-1"
      aria-label="Selected run details"
    >
      <div className="analysis-panel-heading">
        <div>
          <p className="eyebrow">RUN DETAILS</p>
          <h3>
            {run.controller} <span>· {roadLabel(run)}</span>
          </h3>
          <p>{date(run.completed_at_utc)}</p>
        </div>
        <button onClick={onClose}>Close details</button>
      </div>
      <div className="analysis-detail-grid">
        {detailGroups.map(([title, fields]) => (
          <div key={title}>
            <h4>{title}</h4>
            <dl>
              {fields.map(([key, label, unit]) => (
                <div key={key}>
                  <dt>{label}</dt>
                  <dd>
                    {fmt(run[key], key === "api_cost_usd" ? 6 : 3)}
                    {finite(run[key]) && unit && ` ${unit}`}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>
      <h4>Controller history & settings</h4>
      {run.controller_history.length ? (
        <ol className="analysis-controller-history">
          {run.controller_history.map((entry, index) => (
            <li key={index}>
              <strong>{entry.settings.controller}</strong> · frames{" "}
              {entry.start_frame}–{entry.end_frame}
              <code>{JSON.stringify(entry.settings)}</code>
            </li>
          ))}
        </ol>
      ) : (
        <p className="analysis-caption">
          No controller settings were recorded.
        </p>
      )}
      <details>
        <summary>Run identifiers</summary>
        <dl className="analysis-identifiers">
          <dt>Run ID</dt>
          <dd>{run.run_id}</dd>
          <dt>Fingerprint</dt>
          <dd>{run.run_fingerprint}</dd>
        </dl>
      </details>
      <p className="analysis-caption">
        Signed path errors: positive is right, negative is left. Missing values
        are shown as —. These ledgers store summaries, not frame traces. Source
        code revisions and dataset content hashes were not recorded; comparisons
        assume the same road file and scoring implementation.
      </p>
    </section>
  );
}

export default function Analysis({ pendingFrame, refreshKey }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [filters, setFilters] = useState({
    source: "",
    controller: "",
    road: "",
    since: "",
    until: "",
  });
  const [metric, setMetric] = useState("total_cost");
  const [included, setIncluded] = useState(null);
  const [chartCohort, setChartCohort] = useState("");
  const [sort, setSort] = useState("newest");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState(null);
  const detailRef = useRef(null);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    api("analysis")
      .then((result) => {
        if (!active) return;
        setData(result);
        logger.info("Analysis loaded", {
          runs: result.runs.length,
          warnings: result.warnings.length,
        });
      })
      .catch((failure) => {
        if (!active) return;
        setError(failure.message);
        logger.error("Analysis load failed", failure);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [reload, refreshKey]);
  const runs = data?.runs || [];
  const filtered = useMemo(
    () => filterRuns(data?.runs || [], filters),
    [data, filters],
  );
  const summaries = useMemo(() => controllerStats(filtered), [filtered]);
  const controllerNames = summaries
    .map((s) => s.controller)
    .filter((c) => c !== "mixed");
  const compared =
    included === null
      ? controllerNames
      : controllerNames.filter((c) => included.includes(c));
  const board = leaderboard(filtered, compared, metric);
  const roads = [...new Map(runs.map((r) => [roadKey(r), r])).entries()].sort(
    (a, b) => roadLabel(a[1]).localeCompare(roadLabel(b[1])),
  );
  const cohorts = [
    ...new Map(filtered.map((r) => [cohortKey(r), r])).entries(),
  ];
  const effectiveCohort = cohorts.some(([key]) => key === chartCohort)
    ? chartCohort
    : cohorts.at(-1)?.[0];
  const cohortRows = filtered.filter((r) => cohortKey(r) === effectiveCohort);
  const plotRows = cohortRows.length
    ? progress(cohortRows, roadKey(cohortRows[0]), metric)
    : [];
  const sorted = [...filtered].sort((a, b) =>
    sort === "score"
      ? (a.total_cost ?? Infinity) - (b.total_cost ?? Infinity)
      : (sort === "oldest" ? 1 : -1) *
        a.completed_at_utc.localeCompare(b.completed_at_utc),
  );
  const pageCount = Math.max(1, Math.ceil(sorted.length / 25));
  const currentPage = Math.min(page, pageCount - 1);
  const visible = sorted.slice(currentPage * 25, (currentPage + 1) * 25);
  const totalCost = summaries.reduce((sum, s) => sum + s.cost, 0);
  const unknownCost = summaries.reduce((sum, s) => sum + s.unknownCost, 0);
  const selectedRun = runs.find((r) => r.run_fingerprint === selected);
  const updateFilter = (key, value) => {
    setFilters((previous) => ({ ...previous, [key]: value }));
    setPage(0);
  };
  const selectRun = (run) => {
    setSelected(run.run_fingerprint);
    logger.info("Analysis run selected", {
      controller: run.controller,
      runId: run.run_id,
    });
    requestAnimationFrame(() => {
      detailRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
      detailRef.current?.focus({ preventScroll: true });
    });
  };
  const exportRuns = () => {
    const url = URL.createObjectURL(
      new Blob([csvExport(sorted)], { type: "text/csv;charset=utf-8" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "controller-run-analysis.csv";
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    logger.info("Analysis exported", { runs: sorted.length });
  };
  return (
    <main className="analysis-page">
      <div className="analysis-heading">
        <div>
          <p className="eyebrow">CONTROLLER EVALUATION</p>
          <h2>Performance & progress</h2>
          <p>
            Compare outcomes, inspect every saved run, and see what improves.
          </p>
        </div>
        <div className="analysis-actions">
          <button
            onClick={() => setReload((value) => value + 1)}
            disabled={loading}
          >
            {loading ? "Loading…" : "Refresh results"}
          </button>
          <button onClick={exportRuns} disabled={!filtered.length}>
            Export filtered CSV
          </button>
        </div>
      </div>
      {pendingFrame && (
        <p role="status" className="analysis-notice">
          Simulation is pausing after frame {pendingFrame.frame}. Its pending
          request may still finish; no further frames will be requested.
        </p>
      )}
      {error && (
        <p role="alert" className="analysis-notice error">
          {error}
          {data && " Showing the previous snapshot; refresh to retry."}
        </p>
      )}
      {data?.warnings.map((warning) => (
        <p role="status" className="analysis-notice" key={warning}>
          {warning}
        </p>
      ))}
      <section className="analysis-filters" aria-label="Filter run history">
        <label>
          Source
          <select
            value={filters.source}
            onChange={(e) => {
              setFilters((f) => ({ ...f, source: e.target.value, road: "" }));
              setPage(0);
            }}
          >
            <option value="">All sources</option>
            <option value="dataset">Dataset roads</option>
            <option value="demo">Synthetic demos</option>
          </select>
        </label>
        <label>
          Road
          <select
            value={filters.road}
            onChange={(e) => updateFilter("road", e.target.value)}
          >
            <option value="">All roads</option>
            {roads
              .filter(([, r]) => !filters.source || filters.source === r.source)
              .map(([key, r]) => (
                <option key={key} value={key}>
                  {roadLabel(r)}
                </option>
              ))}
          </select>
        </label>
        <label>
          Controller
          <select
            value={filters.controller}
            onChange={(e) => updateFilter("controller", e.target.value)}
          >
            <option value="">All controllers</option>
            {[...new Set(runs.map((r) => r.controller))].sort().map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
        <label>
          From (UTC)
          <input
            type="date"
            value={filters.since}
            onChange={(e) => updateFilter("since", e.target.value)}
          />
        </label>
        <label>
          Through (UTC)
          <input
            type="date"
            value={filters.until}
            onChange={(e) => updateFilter("until", e.target.value)}
          />
        </label>
        <button
          onClick={() => {
            setFilters({
              source: "",
              controller: "",
              road: "",
              since: "",
              until: "",
            });
            setIncluded(null);
            setPage(0);
          }}
        >
          Clear filters
        </button>
      </section>
      <div className="analysis-stats">
        <Metric
          label="Unique completed runs"
          value={fmt(filtered.length, 0)}
          note={`${runs.length} saved across all filters`}
        />
        <Metric
          label="Controllers / roads"
          value={`${summaries.length} / ${new Set(filtered.map(roadKey)).size}`}
          note="Coverage in the current selection"
        />
        <Metric
          label="Runs with path metrics"
          value={`${filtered.filter((r) => finite(r.path_rmse_m)).length} / ${filtered.length}`}
          note="Missing history stays unknown"
        />
        <Metric
          label="Recorded API spend"
          value={`$${fmt(totalCost, 6)}`}
          note={
            unknownCost
              ? `Known subtotal · ${unknownCost} runs have unknown cost`
              : "Saved runs only; not total account spend"
          }
        />
      </div>
      {!loading && !runs.length && !error ? (
        <section className="analysis-panel analysis-empty">
          <h3>No completed runs yet</h3>
          <p>
            Finish a simulation with any controller, then return here or
            refresh. Exact reruns share one saved result.
          </p>
        </section>
      ) : (
        <>
          <section className="analysis-panel">
            <div className="analysis-panel-heading">
              <div>
                <p className="eyebrow">SHARED-ROAD COMPARISON</p>
                <h3>Controller leaderboard</h3>
              </div>
              <label className="analysis-inline-label">
                Rank by
                <select
                  value={metric}
                  onChange={(e) => setMetric(e.target.value)}
                >
                  {Object.entries(metrics).map(([key, item]) => (
                    <option key={key} value={key}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div
              className="analysis-compare"
              role="group"
              aria-label="Controllers to compare"
            >
              {controllerNames.map((name) => (
                <label key={name}>
                  <input
                    type="checkbox"
                    checked={compared.includes(name)}
                    onChange={(e) =>
                      setIncluded(
                        e.target.checked
                          ? [...compared, name]
                          : compared.filter((c) => c !== name),
                      )
                    }
                  />
                  {name}
                </label>
              ))}
            </div>
            <p className="analysis-caption">
              {metrics[metric].note} Ranked on{" "}
              <strong>
                {board.roads} shared {board.roads === 1 ? "road" : "roads"}
              </strong>{" "}
              and {board.cohorts} matching run-length groups.{" "}
              {compared.length === 1 &&
                "Only one controller selected; this is not a comparative ranking."}
            </p>
            {board.roads > 0 ? (
              <div className="analysis-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Rank</th>
                      <th>Controller</th>
                      <th>Matched {metrics[metric].label.toLowerCase()}</th>
                      <th>Gap to leader</th>
                      <th>Compared runs</th>
                      <th>Shared roads</th>
                    </tr>
                  </thead>
                  <tbody>
                    {board.entries.map((entry) => (
                      <tr key={entry.controller}>
                        <td>
                          <span
                            className={`rank-badge ${entry.rank === 1 ? "first" : ""}`}
                          >
                            {entry.rank}
                          </span>
                        </td>
                        <th scope="row">{entry.controller}</th>
                        <td>
                          <strong>
                            {fmt(entry.value, 3)} {metrics[metric].unit}
                          </strong>
                        </td>
                        <td>
                          {entry.rank === 1
                            ? "Leader"
                            : board.entries[0].value > 0
                              ? `+${fmt((entry.value / board.entries[0].value - 1) * 100, 1)}%`
                              : `+${fmt(entry.value, 3)}`}
                        </td>
                        <td>{entry.runs}</td>
                        <td>{entry.roads}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="analysis-empty">
                {!compared.length
                  ? "Select at least one controller to rank."
                  : "No shared roads with matching lengths and recorded metrics. Adjust the comparison or complete runs on the same roads."}
              </p>
            )}
            <details className="analysis-method">
              <summary>How the ranking works</summary>
              <p>
                For each selected controller, take the median metric across its
                unique runs on each common road and run length. Average matching
                lengths within a road, then average roads equally. Only roads
                and lengths covered by every selected controller qualify.
                Mixed-controller rides stay in history but cannot rank as a
                single controller. Path ranking requires metrics covering all
                active frames.
              </p>
              <p>
                Scores compare original acceleration targets; path metrics
                measure the UI projection. These are different objectives.
                Settings variants are grouped under their controller name;
                inspect run details for gains and reloads. Small samples and a
                single road are not evidence of general driving quality.
              </p>
            </details>
          </section>
          <section className="analysis-panel">
            <div className="analysis-panel-heading">
              <div>
                <p className="eyebrow">CHANGE OVER TIME</p>
                <h3>{metrics[metric].label} history</h3>
              </div>
              <label className="analysis-inline-label">
                Road & length
                <select
                  value={effectiveCohort || ""}
                  disabled={!cohorts.length}
                  onChange={(e) => setChartCohort(e.target.value)}
                >
                  {!cohorts.length && <option value="">No runs</option>}
                  {cohorts.map(([key, r]) => (
                    <option key={key} value={key}>
                      {cohortLabel(r)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <ProgressChart
              rows={plotRows}
              metric={metric}
              selected={selected}
              onSelect={selectRun}
            />
          </section>
          <section className="analysis-panel">
            <div className="analysis-panel-heading">
              <div>
                <p className="eyebrow">COVERAGE & OPERATING COST</p>
                <h3>Controller overview</h3>
              </div>
            </div>
            <div className="analysis-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Controller</th>
                    <th>Runs / roads</th>
                    <th>Best score</th>
                    <th>Median score</th>
                    <th>Latest score</th>
                    <th>Mean step</th>
                    <th>Recorded API cost</th>
                    <th>Path coverage</th>
                  </tr>
                </thead>
                <tbody>
                  {summaries.map((s) => (
                    <tr key={s.controller}>
                      <th scope="row">
                        <button
                          className="analysis-link"
                          onClick={() =>
                            updateFilter("controller", s.controller)
                          }
                        >
                          {s.controller}
                        </button>
                      </th>
                      <td>
                        {s.runs} / {s.roads}
                      </td>
                      <td>{fmt(s.best)}</td>
                      <td>{fmt(s.median)}</td>
                      <td>
                        <button
                          className="analysis-link"
                          onClick={() => selectRun(s.latest)}
                        >
                          {fmt(s.latest.total_cost)}
                        </button>
                      </td>
                      <td>{fmt(s.meanStep, 1)} ms</td>
                      <td>
                        ${fmt(s.cost, 6)}
                        {s.unknownCost > 0 && " + unknown"}
                      </td>
                      <td>
                        {s.pathRuns} / {s.runs}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="analysis-caption">
              Overview statistics use all filtered runs and may cover different
              roads and settings. Use the shared-road leaderboard for
              comparison. Mean step is the mean of each run’s average processing
              latency; it includes cache replay.
            </p>
          </section>
          <section className="analysis-panel">
            <div className="analysis-panel-heading">
              <div>
                <p className="eyebrow">SAVED OUTCOMES</p>
                <h3>
                  Run history <span>· {filtered.length}</span>
                </h3>
              </div>
              <label className="analysis-inline-label">
                Sort
                <select
                  value={sort}
                  onChange={(e) => {
                    setSort(e.target.value);
                    setPage(0);
                  }}
                >
                  <option value="newest">Newest first</option>
                  <option value="oldest">Oldest first</option>
                  <option value="score">Best score first</option>
                </select>
              </label>
            </div>
            <div className="analysis-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Completed</th>
                    <th>Controller</th>
                    <th>Road</th>
                    <th>Score ↓</th>
                    <th>Path RMSE</th>
                    <th>Mean step</th>
                    <th>API / cache</th>
                    <th>API cost</th>
                    <th>Inspect</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((run) => (
                    <tr
                      key={run.run_fingerprint}
                      className={
                        selected === run.run_fingerprint ? "selected" : ""
                      }
                    >
                      <td>{date(run.completed_at_utc)}</td>
                      <th scope="row">{run.controller}</th>
                      <td>{roadLabel(run)}</td>
                      <td>{fmt(run.total_cost)}</td>
                      <td>
                        {fmt(run.path_rmse_m)}
                        {finite(run.path_rmse_m) && " m"}
                      </td>
                      <td>{fmt(run.mean_step_ms, 1)} ms</td>
                      <td>
                        {fmt(run.api_requests, 0)} / {fmt(run.cache_hits, 0)}
                      </td>
                      <td>
                        {finite(run.api_cost_usd)
                          ? `$${fmt(run.api_cost_usd, 6)}`
                          : "Unknown"}
                      </td>
                      <td>
                        <button
                          aria-label={`Inspect ${run.controller} run completed ${date(run.completed_at_utc)}`}
                          onClick={() => selectRun(run)}
                        >
                          Details
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!visible.length && (
              <p className="analysis-empty">No runs match these filters.</p>
            )}
            <div className="analysis-pagination">
              <span>
                Page {currentPage + 1} of {pageCount} · 25 per page
              </span>
              <button
                disabled={currentPage === 0}
                onClick={() => setPage(currentPage - 1)}
              >
                Previous
              </button>
              <button
                disabled={currentPage + 1 >= pageCount}
                onClick={() => setPage(currentPage + 1)}
              >
                Next
              </button>
            </div>
          </section>
          <RunDetails
            run={selectedRun}
            detailRef={detailRef}
            onClose={() => setSelected(null)}
          />
        </>
      )}
      <footer className="analysis-footnote">
        Only unique completed runs are saved. Failed or abandoned attempts are
        absent; exact reruns keep the first run’s timing and cost. Recorded
        spend excludes those unsaved attempts.{" "}
        {data && `Snapshot: ${date(data.loaded_at_utc)}.`} Refresh to pick up
        runs from other sessions.
      </footer>
    </main>
  );
}
