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

## Development

Keep the Python server running and use `npm --prefix ui run dev` for the Vite development UI. Rebuild to update the UI served on port 8000.

```sh
npm --prefix ui test
npm --prefix ui run lint
npm --prefix ui run build
uv run --python 3.11 --with-requirements requirements.txt --no-project \
  python -m unittest discover -s ui/tests -v
```
