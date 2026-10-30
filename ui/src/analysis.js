// Aggregation uses saved outcomes only. Unknown metrics never become zero.
export const finite = (value) =>
  typeof value === "number" && Number.isFinite(value);
export const mean = (values) => {
  const known = values.filter(finite);
  return known.length
    ? known.reduce((sum, value) => sum + value, 0) / known.length
    : null;
};
export function median(values) {
  const sorted = values.filter(finite).sort((a, b) => a - b);
  const n = sorted.length;
  return n
    ? (sorted[Math.floor((n - 1) / 2)] + sorted[Math.floor(n / 2)]) / 2
    : null;
}
export const roadKey = (run) => JSON.stringify([run.source, run.segment]);
export const cohortKey = (run) =>
  JSON.stringify([
    run.source,
    run.segment,
    run.total_frames,
    run.active_frames,
    run.scored_frames,
    run.simulated_seconds,
  ]);
export function filterRuns(runs, filters) {
  return runs.filter(
    (run) =>
      (!filters.source || run.source === filters.source) &&
      (!filters.controller || run.controller === filters.controller) &&
      (!filters.road || roadKey(run) === filters.road) &&
      (!filters.since || run.completed_at_utc.slice(0, 10) >= filters.since) &&
      (!filters.until || run.completed_at_utc.slice(0, 10) <= filters.until),
  );
}
export function controllerStats(runs) {
  return [...new Set(runs.map((r) => r.controller))]
    .sort()
    .map((controller) => {
      const rows = runs.filter((r) => r.controller === controller);
      const scores = rows.map((r) => r.total_cost).filter(finite);
      const chronological = [...rows].sort((a, b) =>
        a.completed_at_utc.localeCompare(b.completed_at_utc),
      );
      return {
        controller,
        runs: rows.length,
        roads: new Set(rows.map(roadKey)).size,
        best: scores.length ? Math.min(...scores) : null,
        median: median(scores),
        latest: chronological.at(-1),
        pathRuns: rows.filter((r) => finite(r.path_rmse_m)).length,
        meanStep: mean(rows.map((r) => r.mean_step_ms)),
        cost: rows.reduce((sum, r) => sum + (r.api_cost_usd ?? 0), 0),
        unknownCost: rows.filter((r) => !finite(r.api_cost_usd)).length,
      };
    });
}
export function leaderboard(runs, selected, metric = "total_cost") {
  const controllers = selected.filter((name) => name !== "mixed");
  const groups = new Map();
  for (const run of runs) {
    if (
      !controllers.includes(run.controller) ||
      !finite(run[metric]) ||
      ![
        run.total_frames,
        run.active_frames,
        run.scored_frames,
        run.simulated_seconds,
      ].every(finite) ||
      run.scored_frames <= 0 ||
      (metric.includes("path") && run.path_frames !== run.active_frames)
    )
      continue;
    const key = cohortKey(run);
    if (!groups.has(key)) groups.set(key, new Map());
    const group = groups.get(key);
    if (!group.has(run.controller)) group.set(run.controller, []);
    group.get(run.controller).push(run);
  }
  const shared = [...groups.entries()].filter(([, group]) =>
    controllers.every((c) => group.has(c)),
  );
  const entries = controllers
    .map((controller) => {
      // Average comparable lengths within a road, then weight every road equally.
      const roadScores = new Map();
      let count = 0;
      for (const [, group] of shared) {
        const rows = group.get(controller);
        const key = roadKey(rows[0]);
        if (!roadScores.has(key)) roadScores.set(key, []);
        roadScores.get(key).push(median(rows.map((r) => r[metric])));
        count += rows.length;
      }
      return {
        controller,
        value: mean([...roadScores.values()].map(mean)),
        runs: count,
        roads: roadScores.size,
      };
    })
    .sort(
      (a, b) =>
        (a.value ?? Infinity) - (b.value ?? Infinity) ||
        a.controller.localeCompare(b.controller),
    );
  let previous = null;
  let rank = 0;
  entries.forEach((entry, index) => {
    if (entry.value === null) {
      entry.rank = null;
      return;
    }
    if (previous === null || Math.abs(entry.value - previous) > 1e-9)
      rank = index + 1;
    entry.rank = rank;
    previous = entry.value;
  });
  return { entries, cohorts: shared.length, roads: entries[0]?.roads ?? 0 };
}
export function progress(runs, road, metric) {
  return runs
    .filter((r) => roadKey(r) === road && finite(r[metric]))
    .sort((a, b) => a.completed_at_utc.localeCompare(b.completed_at_utc));
}
export function csvExport(runs) {
  if (!runs.length) return "";
  const fields = Object.keys(runs[0]);
  const cell = (value) => {
    let text =
      value == null
        ? ""
        : typeof value === "object"
          ? JSON.stringify(value)
          : String(value);
    // Prevent spreadsheet formula evaluation for untrusted text fields.
    if (typeof value === "string" && /^[=+@\-\t\r\n]/.test(text))
      text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  return [
    fields.map(cell).join(","),
    ...runs.map((r) => fields.map((f) => cell(r[f])).join(",")),
  ].join("\r\n");
}
