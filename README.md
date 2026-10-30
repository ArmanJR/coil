# coil

unusual self driving cars

A workspace for experimenting with steering controllers and seeing their effect on a simulated ride. Built on [comma.ai's Controls Challenge v2](https://github.com/commaai/controls_challenge), using its TinyPhysics model, controller interface, and scoring.

## Run

Requires Node.js 20.19+ or 22.12+ and [uv](https://docs.astral.sh/uv/). Run from the repository root; uv manages Python 3.11 and the simulator dependencies.

```sh
npm --prefix ui ci
npm --prefix ui run build
uv run ui/server.py
```

Open **http://127.0.0.1:8000**. Use `--port 8001` for another port or `--verbose` for per-step logs.

Three synthetic scenarios work without downloading data. For dataset roads, extract the [official dataset archive](https://huggingface.co/datasets/commaai/commaSteeringControl/resolve/main/data/SYNTHETIC_V0.zip) so the CSV files sit directly in `data/`, then refresh the UI. The UI does not download data automatically.

## UI

- **Input / Hard roads:** select a scenario, local CSV, or one of five curated 600-frame roads. Changing roads resets the run; missing files are disabled.
- **Playback:** Run/Pause with `Space`, single-step, reset, or choose 0.5×, 1×, 2×, 3×, or 5×. The timer starts after the 10-second warm-up. Switching tabs pauses playback.
- **Controller:** choose PID, zero steering, or your own controller. PID gains apply on the next step. **Manual** steering uses the slider, A/D, arrow keys, or on-screen buttons; held controls center on release.
- **Reload:** apply controller code edits while preserving the physics history. Controller memory resets; a paused reload also advances one step. For PID, UI gains override the source defaults.
- **Driving view:** follow the car or view the full route. Blue shows your projected path; green shows the target. Telemetry tracks steering, lateral acceleration, jerk, error, and PID contributions.

## Simulation and controllers

TinyPhysics is an autoregressive model trained to mimic a bicycle-model simulator with realistic driving noise, with an architecture similar to [comma.ai's ML Controls Sim](https://blog.comma.ai/096release/#ml-controls-sim). Every 100 ms, it predicts lateral acceleration from steering, speed, forward acceleration, road roll, and prior lateral acceleration.

The synthetic dataset is based on [comma-steering-control](https://github.com/commaai/comma-steering-control), using car and road states from [openpilot](https://github.com/commaai/openpilot) users. The UI's built-in scenarios are demonstrations, not benchmark results.

The displayed ride is a **kinematic projection** of speed and lateral acceleration (`yaw_rate = lateral_acceleration / speed`). TinyPhysics predicts lateral acceleration, not position; road width and wheel angle are illustrative. Controllers receive acceleration feedback, and projected lateral offset is not scored.

Add `controllers/<name>.py` with a `Controller()` class implementing `update(target_lataccel, current_lataccel, state, future_plan)` and returning a steering command. See [the PID controller](controllers/pid.py) for an example. Refresh the UI to discover a new controller; use Reload for subsequent edits.

## Jev controllers

`controllers/jev.py` uses `typesafe/jev-1.14` through OpenRouter's
[Decisions API](https://openrouter.ai/docs/guides/community/jev-tutorial).
Put `OPENROUTER_API_KEY=...` in the project-root `.env` (git-ignored), or set it
in the server environment. The key stays server-side; `python-dotenv` loads it.

Rebuild the UI and restart the server, then select **Jev v1 · acceleration tracking**
or **Jev v2 · path recovery**. Both use the Jev 1.14 model.
Selecting Jev pauses playback. **Step** requests one decision; **Run** requests
continuously, waiting for each answer before advancing simulation time by 100 ms.
Pause allows the pending frame to finish. The decision panel below **Applied Steering**
in the left sidebar shows frame inputs,
candidate commands, choice, probabilities, confidence, latency, and API-reported
cost since controller load. Reloading or switching controllers resets its memory
and telemetry. Selecting alone makes no request; a paused Reload advances one frame.

The frozen v1 (`jev`) chooses from up to ten distinct steering commands: hold the previous command,
use the existing PID controller's proposal, or adjust either by `±0.025` or `±0.1`.
Both anchors and all candidates are bounded to `[-2, 2]`; commands that coincide
after clipping/rounding are offered only once. PID can propose a larger correction
than the local adjustments, but Jev still makes the final choice. These are simulator
units, not degrees. Inputs include vehicle state, eight recent observations, and
upcoming targets (the next second at 100 ms resolution, then at 500 ms intervals).
This experimental controller has not been shown to outperform PID. Confidence
describes the choice distribution, not driving accuracy.

Each request also includes calculated tracking error, signed error rate, change
in absolute error, whether the error is growing/shrinking/steady, observed lateral
jerk, and accumulated signed error integrated over 100 ms frames. Derivatives are
`null` until a previous observation exists. The PID proposal uses the unchanged
baseline gains and per-frame PID formula, with memory updated during warm-up;
the time-integrated error signal is separately expressed in m/s. The UI's frame
inputs expose these signals and the PID proposal. Failed decisions do not advance
PID memory. Reset/reload starts fresh memory. Changed prompts and candidates cause
cache misses automatically; older cached responses remain intact. These changes
have offline correctness tests; driving evaluation so far is limited to road 13301.

Responses are cached persistently in `cache/jev.sqlite3` (git-ignored). Identical
requests reuse validated responses without contacting OpenRouter, including
after a server restart. The key includes the model, prompt, frame inputs, recent
history, and candidate commands. Changing those inputs causes cache misses;
rerunning the same road with the same simulation state replays cached decisions.
The UI distinguishes cache hits from API requests and counts cached decisions as
zero new cost. Cached frames work without an API key; uncached frames still need
one. Only successful, validated answers are saved, and cache read/write failures
stop the run instead of silently making more paid requests. Set `JEV_CACHE_PATH`
in the server environment to choose another database. Stop simulations before
deleting that file to deliberately start a fresh cache. Earlier runs from before
this feature were not saved and cannot be replayed from cache.

Warm-up makes no paid calls. A complete 600-frame segment makes **500 decisions**,
of which 400 are scored; only cache misses are billed. API latency can make playback slower than
the selected speed. Requests have a 15-second timeout and no automatic retries.
API failures or malformed answers stop the run without substituting an action;
reset before continuing. Offline tests mock all paid calls.

For a CLI rollout, use `--controller jev`; uncached frames are billed. The optional
`prepare_step(step_index, previous_steering, active)` hook supplies the previous
applied command and identifies warm-up without changing existing controllers'
`update` behavior. Other remote controllers can expose `decision_mode = "remote"`
and a `decision` dictionary matching Jev's telemetry to use the same UI panel.

### Minimal controller versioning

`controllers/jev.py` is the frozen v1 baseline, with its behavior and historical
cache identity preserved. Its source documents the known position-feedback and
PID handover limitations. New behavior lives in `controllers/jev-v2.py`; select
`jev-v2` in the UI or pass `--controller jev-v2` on the CLI. Future behavior changes
should get a new versioned controller instead of silently changing these baselines.

V2 gets projected lateral offset and heading error from the simulator's geometry,
which matches the UI and survives switching/reloading controllers. It adds
`-0.08 × offset - 0.8 × speed × sin(heading error)` to the original acceleration
target, limited to ±0.8 m/s² and a 0.05 m/s² correction change per frame. Recovery
is disabled when speed is at most 0.1 m/s. The corrected target is bounded to the
model's ±5 m/s² range. The reference path always uses the original target.

On the first active frame, the PID integral is initialized to match the previous
applied steering. All candidate commands are limited to ±0.15 from the previous
command per frame, and PID integral back-calculation prevents saturation windup.
Jev still chooses among hold, PID, and nearby candidates. The recovery target and
correction are visible in the decision panel; heading error and full-ride path
RMSE are shown in telemetry. These are initial, untuned control settings, not a
demonstrated improvement in Jev driving quality.

The saved road 13301 run exposed a v2 turn-exit regression: its score was 3332.79
versus v1's 975.17 (lower is better), with a maximum projected offset of 55.52 m.
An exact cached replay showed late steering unwind and occasional further left
commands as the road straightened. The fixed steering-rate limit slowed recovery,
and the bounded path correction took too long to remove the resulting heading
error. V2 is retained as an experimental baseline; these issues are not yet fixed.

V2 includes `jev-v2` explicitly in its request and cache key, so v1 cache entries
remain intact and are not reused for v2. The existing `runs.csv` records v2 under
its own name and fingerprint, without deleting or relabeling old Jev results.
The official score still measures the original acceleration target over 40 seconds;
path recovery has a different objective and may trade off that score.

## Evaluation

Lower scores are better. Scoring uses samples 100–499: the first 40 seconds after warm-up.

```text
lataccel_cost = 100 × mean((actual − target)²)
jerk_cost     = 100 × mean((diff(actual) / 0.1)²)
total_cost    = 50 × lataccel_cost + jerk_cost
```

Compare controllers on local dataset segments and generate `report.html`:

```sh
uv run --python 3.11 --with-requirements requirements.txt --no-project \
  python eval.py --model_path models/tinyphysics.onnx --data_path data \
  --num_segs 100 --test_controller pid --baseline_controller zero
```

For a single rollout, use `tinyphysics.py` with `--model_path`, `--data_path data/13301.csv`, and `--controller pid`; add `--debug` for plots.

## Run results

Every unique completed ride appends one row to **`results/runs.csv`** (git-ignored).
This includes live UI runs with any controller or manual steering, CLI rollouts,
and each controller/baseline run from batch evaluation. Reset or abandoned runs
are not recorded; saving happens at the end of the entire segment, not just the
40-second scoring window. Exact reruns, including cached Jev replays, are skipped.
A persistent SHA-256 fingerprint includes road identity and inputs, controller
history/settings, steering and acceleration trajectories, and scoring parameters.
Timing, timestamps, and API/cache statistics do not affect identity; the first
row's performance statistics are retained. Equal scores alone do not make runs
duplicates. Duplicate checks and appends share a lock across batch processes.

Rows include a unique run ID, UTC completion time, road/scenario, controller,
frame counts, all three score components, scored-window tracking RMSE and maximum
error, jerk RMS, and maximum steering over the active ride. Timing includes total
elapsed time from the first active frame (including pauses), processing time
(excluding pauses and playback delays), and mean/maximum processing time per frame.
Jev API request counts, cache hits, and new API cost are summed over the whole ride,
including controller switches/reloads. Unknown API cost is left blank.

Controller changes are labeled `mixed`; the JSON `controller_history` column
records frame ranges, controller names, and available numeric PID gains. Gain
changes and reloads start new history entries. Concurrent batch processes append
under a file lock. Persistence errors are reported rather than silently ignored.
Set `RUN_RESULTS_PATH` in the process environment to use another CSV path.

Full-ride projected path RMSE, maximum/final lateral offset, and maximum/final
heading error are also saved to **`results/path_metrics.csv`**, joined to score
rows by `run_fingerprint`. This includes the unscored final ten seconds. The
sidecar keeps the historical `runs.csv` schema and rows unchanged; old runs gain
path metrics if replayed. With `RUN_RESULTS_PATH`, it lives beside that CSV.

## Development

Keep the Python server running and use `npm --prefix ui run dev` for the Vite development UI. Rebuild to update the UI served on port 8000.

```sh
npm --prefix ui test
npm --prefix ui run lint
npm --prefix ui run build
uv run --python 3.11 --with-requirements requirements.txt --no-project \
  python -m unittest discover -s ui/tests -v
```
