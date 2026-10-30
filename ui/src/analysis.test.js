import assert from "node:assert/strict";
import test from "node:test";
import {
  cohortKey,
  controllerStats,
  csvExport,
  filterRuns,
  leaderboard,
  mean,
  median,
  progress,
  roadKey,
} from "./analysis.js";

const run = (controller, segment, total_cost, extra = {}) => ({
  controller,
  segment,
  total_cost,
  source: "dataset",
  total_frames: 600,
  active_frames: 500,
  scored_frames: 400,
  simulated_seconds: 50,
  completed_at_utc: "2026-10-30T05:00:00+00:00",
  api_cost_usd: 0,
  path_rmse_m: null,
  path_frames: null,
  ...extra,
});

test("missing values stay unknown while zero remains a valid score", () => {
  assert.equal(mean([null, undefined, NaN]), null);
  assert.equal(mean([null, 0, 4]), 2);
  assert.equal(median([4, 0, 100, 2]), 3);
  assert.equal(median([]), null);
});

test("leaderboard excludes easy roads that competitors have not driven", () => {
  const result = leaderboard(
    [
      run("a", "hard", 100),
      run("b", "hard", 90),
      ...Array.from({ length: 20 }, () => run("a", "easy", 1)),
    ],
    ["a", "b"],
  );
  assert.equal(result.roads, 1);
  assert.deepEqual(
    result.entries.map((r) => [r.controller, r.value, r.runs]),
    [
      ["b", 90, 1],
      ["a", 100, 1],
    ],
  );
});

test("unique-run counts do not overweight a road and median resists outliers", () => {
  const rows = [
    run("a", "one", 10),
    run("a", "one", 20),
    run("a", "one", 900),
    run("a", "two", 100),
    run("b", "one", 30),
    run("b", "two", 80),
  ];
  const result = leaderboard(rows, ["a", "b"]);
  assert.equal(result.entries.find((r) => r.controller === "a").value, 60);
  assert.equal(result.entries.find((r) => r.controller === "b").value, 55);
});

test("matching lengths are averaged within roads before roads are weighted", () => {
  const rows = [
    run("a", "one", 10),
    run("b", "one", 20),
    run("a", "one", 30, { total_frames: 700 }),
    run("b", "one", 40, { total_frames: 700 }),
    run("a", "two", 100),
    run("b", "two", 110),
  ];
  const result = leaderboard(rows, ["a", "b"]);
  assert.equal(result.cohorts, 3);
  assert.equal(result.roads, 2);
  assert.equal(result.entries[0].value, 60);
});

test("different sources and scoring windows do not form a comparison", () => {
  for (const extra of [
    { source: "demo" },
    { scored_frames: 300 },
    { active_frames: 400 },
    { simulated_seconds: 40 },
    { total_frames: 500 },
  ]) {
    const result = leaderboard(
      [run("a", "same", 1), run("b", "same", 2, extra)],
      ["a", "b"],
    );
    assert.equal(result.roads, 0);
    assert.ok(result.entries.every((r) => r.value === null && r.rank === null));
  }
});

test("missing path data cannot win and incomplete path coverage is excluded", () => {
  const rows = [
    run("a", "one", 1),
    run("b", "one", 2, { path_rmse_m: 4, path_frames: 500 }),
  ];
  assert.equal(leaderboard(rows, ["a", "b"], "path_rmse_m").roads, 0);
  rows[0].path_rmse_m = 0;
  rows[0].path_frames = 400;
  assert.equal(leaderboard(rows, ["a", "b"], "path_rmse_m").roads, 0);
  rows[0].path_frames = 500;
  assert.equal(
    leaderboard(rows, ["a", "b"], "path_rmse_m").entries[0].value,
    0,
  );
});

test("mixed-controller rides are retained in history but excluded from rankings", () => {
  const rows = [run("a", "one", 10), run("mixed", "one", 0)];
  assert.equal(controllerStats(rows).length, 2);
  assert.deepEqual(
    leaderboard(rows, ["a", "mixed"]).entries.map((r) => r.controller),
    ["a"],
  );
});

test("zero-score ties share a rank; empty selections cannot declare winners", () => {
  const rows = [run("a", "one", 0), run("b", "one", 0), run("c", "one", 1)];
  assert.deepEqual(
    leaderboard(rows, ["a", "b", "c"]).entries.map((r) => r.rank),
    [1, 1, 3],
  );
  assert.deepEqual(leaderboard(rows, []).entries, []);
  assert.equal(leaderboard([], ["a"]).entries[0].rank, null);
});

test("history filters combine source, road, controller, and inclusive UTC dates", () => {
  const wanted = run("a", "one", 1);
  const rows = [
    wanted,
    run("b", "one", 2),
    run("a", "one", 3, { source: "demo" }),
    run("a", "two", 4),
    run("a", "one", 5, { completed_at_utc: "2026-10-29T23:59:59+00:00" }),
  ];
  assert.deepEqual(
    filterRuns(rows, {
      source: "dataset",
      controller: "a",
      road: roadKey(wanted),
      since: "2026-10-30",
      until: "2026-10-30",
    }),
    [wanted],
  );
});

test("overview separates unknown costs and finds the latest run chronologically", () => {
  const latest = run("a", "one", 30, { api_cost_usd: null });
  const earlier = run("a", "one", 10, {
    api_cost_usd: 0.1,
    completed_at_utc: "2026-10-29T00:00:00+00:00",
    path_rmse_m: 0,
  });
  const result = controllerStats([latest, earlier])[0];
  assert.equal(result.latest, latest);
  assert.equal(result.best, 10);
  assert.equal(result.cost, 0.1);
  assert.equal(result.unknownCost, 1);
  assert.equal(result.pathRuns, 1);
});

test("progress orders runs without filling missing observations with zero", () => {
  const rows = [
    run("a", "one", null),
    run("b", "two", 4),
    run("a", "one", 3, { completed_at_utc: "2026-10-28T00:00:00+00:00" }),
  ];
  assert.deepEqual(progress(rows, roadKey(rows[0]), "total_cost"), [rows[2]]);
  assert.notEqual(
    cohortKey(rows[0]),
    cohortKey({ ...rows[0], active_frames: 400 }),
  );
});

test("CSV exports quote history and neutralize spreadsheet formulas in strings", () => {
  const csv = csvExport([
    {
      controller: "=HYPERLINK(1)",
      metric: -2,
      absent: null,
      history: [{ label: 'a,"b"' }],
    },
  ]);
  assert.ok(csv.includes('"\'=HYPERLINK(1)"'));
  assert.ok(csv.includes('"-2",""'));
  assert.ok(csv.includes('""label""'));
  assert.equal(csvExport([]), "");
});
