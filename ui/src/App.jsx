import { useEffect, useRef } from "react";
import DriveView from "./DriveView.jsx";
import Telemetry, { fixed, signed } from "./Telemetry.jsx";
import useSimulation from "./useSimulation.js";
import RemoteDecision from "./RemoteDecision.jsx";

const names = {
  pid: "PID controller",
  zero: "Zero steering",
  manual: "Manual",
  jev: "Jev v1 · acceleration tracking",
  "jev-v2": "Jev v2 · path recovery",
};

// Curated local segments, each verified to contain exactly 600 data frames.
const hardRoads = [
  { segment: "13301.csv", label: "Sharp hairpin" },
  { segment: "14783.csv", label: "Tight winding bends" },
  { segment: "09154.csv", label: "Hooked turn" },
  { segment: "19419.csv", label: "Winding hook" },
  { segment: "05094.csv", label: "Fast S-bends" },
];

function Wheel({ steering = 0 }) {
  return (
    <svg
      className="steering-wheel"
      viewBox="0 0 100 100"
      role="img"
      aria-label={`Steering command ${fixed(steering)}`}
    >
      <g transform={`rotate(${steering * 60} 50 50)`}>
        <circle cx="50" cy="50" r="38" />
        <path d="M15 40 43 47M85 40 57 47M50 57V87" />
        <circle cx="50" cy="50" r="9" />
        <path d="M50 12v8" className="wheel-marker" />
      </g>
    </svg>
  );
}

function Decision({ sample, controller }) {
  const terms = sample?.terms;
  return (
    <section className="decision">
      <div className="panel-title">
        <h2>Current step</h2>
        <span className="small-label">
          {sample ? `#${sample.step}` : "READY"}
        </span>
      </div>
      <div className="decision-flow">
        <div>
          <span>Target</span>
          <b className="text-green">{signed(sample?.target)}</b>
        </div>
        <i>−</i>
        <div>
          <span>Previous response</span>
          <b>{signed(sample?.input)}</b>
        </div>
        <i>=</i>
        <div>
          <span>Error</span>
          <b>{signed(sample?.error)}</b>
        </div>
        <i>→</i>
        <div className="decision-terms">
          {terms ? (
            <>
              <span>PID contribution</span>
              <div>
                <span>
                  P <b>{signed(terms.p, 3)}</b>
                </span>
                <span>
                  I <b>{signed(terms.i, 3)}</b>
                </span>
                <span>
                  D <b>{signed(terms.d, 3)}</b>
                </span>
              </div>
            </>
          ) : (
            <>
              <span>Controller</span>
              <b>{names[controller] || controller}</b>
            </>
          )}
        </div>
        <i>→</i>
        <div>
          <span>Requested</span>
          <b>{signed(sample?.raw, 3)}</b>
        </div>
        <i>→</i>
        <div>
          <span>
            Applied {Math.abs(sample?.raw || 0) > 2 && <em>CLIPPED</em>}
          </span>
          <b className="text-blue">{signed(sample?.steering, 3)}</b>
        </div>
      </div>
    </section>
  );
}

export default function App() {
  const sim = useSimulation();
  const current = sim.samples.at(-1);
  const manual = sim.settings.controller === "manual";
  const remote =
    sim.catalog?.controllerModes?.[sim.settings.controller] === "remote";
  const controllerChoice = useRef("pid");
  const keys = useRef(new Set());
  const actions = useRef(sim);
  useEffect(() => {
    actions.current = sim;
  });
  useEffect(() => {
    const editable = (target) =>
      target instanceof Element &&
      target.closest("input, select, textarea, [contenteditable=true]");
    const down = (event) => {
      if (editable(event.target)) return;
      if (event.code === "Space" && !event.repeat) {
        event.preventDefault();
        actions.current.toggle();
      }
      if (actions.current.controls.current.controller !== "manual") return;
      if (["ArrowLeft", "ArrowRight", "KeyA", "KeyD"].includes(event.code)) {
        event.preventDefault();
        keys.current.add(event.code);
      }
    };
    const up = (event) => {
      if (!keys.current.has(event.code)) return;
      keys.current.delete(event.code);
      if (!keys.current.size) actions.current.changeSettings({ steering: 0 });
    };
    const clear = () => {
      keys.current.clear();
      if (actions.current.controls.current.controller === "manual")
        actions.current.changeSettings({ steering: 0 });
    };
    const timer = setInterval(() => {
      if (
        !keys.current.size ||
        actions.current.controls.current.controller !== "manual"
      )
        return;
      const left = keys.current.has("ArrowLeft") || keys.current.has("KeyA");
      const right = keys.current.has("ArrowRight") || keys.current.has("KeyD");
      const next =
        actions.current.controls.current.steering +
        (Number(right) - Number(left)) * 0.08;
      actions.current.changeSettings({
        steering: Math.min(2, Math.max(-2, next)),
      });
    }, 50);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", clear);
    return () => {
      clearInterval(timer);
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", clear);
    };
  }, []);

  function selectDriver(next) {
    keys.current.clear();
    const controller = next === "manual" ? "manual" : controllerChoice.current;
    if (sim.catalog?.controllerModes?.[controller] === "remote")
      void sim.pause();
    sim.changeSettings({ controller, steering: 0 });
    sim.record(`${names[controller] || controller} selected`);
  }

  function selectInput(value) {
    const [source, ...parts] = value.split(":");
    const next = {
      ...sim.settings,
      source,
      ...(source === "demo"
        ? { scenario: parts.join(":") }
        : { segment: parts.join(":") }),
    };
    void sim.start(next);
  }

  const inputValue = `${sim.settings.source}:${sim.settings.source === "demo" ? sim.settings.scenario : sim.settings.segment}`;
  const hardRoadValue =
    sim.settings.source === "dataset" &&
    hardRoads.some(({ segment }) => segment === sim.settings.segment)
      ? sim.settings.segment
      : "";
  const time = current?.time || 0;
  const duration = sim.session
    ? (sim.session.totalSteps - sim.session.controlStart) * sim.session.dt
    : 50;
  const blocked = sim.busy || !sim.session || !!sim.error;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          <svg viewBox="0 0 28 28" aria-hidden="true">
            <path d="M2 16h6l5-10 6 16 4-6h3" />
          </svg>
          <h1>TinyPhysics</h1>
          <span className="header-divider" />
          <span>Simulation workspace</span>
        </div>
        <div className="model-status">
          <span className="connection-dot" />
          ONNX / CPU
          <span className="header-divider" />
          <span>Δt 100 ms</span>
        </div>
      </header>
      <div className="toolbar">
        <div className="input-select">
          <label htmlFor="input-source">INPUT</label>
          <select
            id="input-source"
            value={inputValue}
            disabled={sim.busy || !sim.catalog}
            onChange={(event) => selectInput(event.target.value)}
          >
            <optgroup label="Synthetic inputs">
              {Object.entries(
                sim.catalog?.scenarios || {
                  sweeping: { label: "Sweeping bends" },
                },
              ).map(([key, value]) => (
                <option key={key} value={`demo:${key}`}>
                  {value.label}
                </option>
              ))}
            </optgroup>
            {!!sim.catalog?.segments.length && (
              <optgroup label="Local dataset">
                {sim.catalog.segments.map((name) => (
                  <option key={name} value={`dataset:${name}`}>
                    {name}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <span className="source-badge">
            {sim.settings.source === "demo" ? "SYNTHETIC" : "DATASET"}
          </span>
        </div>
        <div className="input-select">
          <label htmlFor="hard-road">HARD ROADS</label>
          <select
            id="hard-road"
            value={hardRoadValue}
            disabled={sim.busy || !sim.catalog}
            onChange={(event) => selectInput(`dataset:${event.target.value}`)}
          >
            <option value="" disabled>
              Pick a hard road
            </option>
            {hardRoads.map(({ segment, label }) => {
              const available = sim.catalog?.segments.includes(segment);
              return (
                <option key={segment} value={segment} disabled={!available}>
                  {segment.slice(0, -4)} · {label}
                  {!available ? " (unavailable)" : ""}
                </option>
              );
            })}
          </select>
        </div>
        <div className="run-controls">
          <button
            className={`run-button ${sim.running ? "is-running" : ""}`}
            onClick={sim.toggle}
            disabled={blocked || sim.session?.finished}
          >
            <span>{sim.running ? "Ⅱ" : "▶"}</span>
            {sim.running ? "Pause" : "Run"}
            <kbd>Space</kbd>
          </button>
          <button
            className="icon-button"
            title="Advance one 100 ms step"
            aria-label="Step simulation"
            onClick={() => void sim.step()}
            disabled={
              blocked ||
              sim.running ||
              !!sim.pendingFrame ||
              sim.session?.finished
            }
          >
            ▷|
          </button>
          <button
            className="icon-button"
            title="Reset the simulation"
            aria-label="Reset simulation"
            onClick={() => void sim.start()}
            disabled={sim.busy || !!sim.pendingFrame}
          >
            ↺
          </button>
          <span className="toolbar-divider" />
          <label className="sr-only" htmlFor="simulation-speed">
            Simulation speed
          </label>
          <select
            id="simulation-speed"
            value={sim.speed}
            onChange={(event) => sim.changeSpeed(Number(event.target.value))}
          >
            <option value="0.5">0.5×</option>
            <option value="1">1×</option>
            <option value="2">2×</option>
            <option value="3">3×</option>
            <option value="5">5×</option>
          </select>
          <span className="run-time">
            <b>{fixed(time, 1)}</b>
            <span>/ {fixed(duration, 1)} s</span>
          </span>
        </div>
      </div>
      {sim.error && (
        <div className="error-banner" role="alert">
          {sim.error}
          <button onClick={() => void sim.start()} disabled={sim.busy}>
            Reset run
          </button>
        </div>
      )}
      <main className="workspace">
        <aside className="controls-panel">
          <div className="panel-title">
            <h2>Driver</h2>
            <span className="small-label">LIVE INPUT</span>
          </div>
          <div className="driver-tabs" role="group" aria-label="Driving mode">
            <button
              className={!manual ? "selected" : ""}
              onClick={() => selectDriver("controller")}
              disabled={sim.busy || !!sim.pendingFrame}
            >
              Controller
            </button>
            <button
              className={manual ? "selected" : ""}
              onClick={() => selectDriver("manual")}
              disabled={sim.busy || !!sim.pendingFrame}
            >
              Manual
            </button>
          </div>
          {!manual && (
            <div className="controller-picker">
              <label htmlFor="controller-select">Controller</label>
              <select
                id="controller-select"
                value={sim.settings.controller}
                disabled={sim.busy || !!sim.pendingFrame}
                onChange={(event) => {
                  if (
                    sim.catalog?.controllerModes?.[event.target.value] ===
                    "remote"
                  )
                    void sim.pause();
                  controllerChoice.current = event.target.value;
                  sim.changeSettings({ controller: event.target.value });
                  sim.record(`${event.target.value} selected`);
                }}
              >
                {(sim.catalog?.controllers || ["pid", "zero"]).map((name) => (
                  <option key={name} value={name}>
                    {names[name] || name}
                  </option>
                ))}
              </select>
              <div className="code-source">
                <code>controllers/{sim.settings.controller}.py</code>
                <button
                  onClick={sim.reloadCode}
                  disabled={blocked || !!sim.pendingFrame}
                  title="Reload the local Python module and reset controller memory. When paused, also advances one step. Current PID sliders still apply."
                >
                  ↻ Reload
                </button>
              </div>
            </div>
          )}
          <div className="wheel-section">
            <Wheel
              steering={manual ? sim.settings.steering : current?.steering || 0}
            />
            <div className="steering-readout">
              <b>
                {signed(
                  manual ? sim.settings.steering : current?.steering || 0,
                  3,
                )}
              </b>
              <span>{manual ? "STEERING INPUT" : "APPLIED STEERING"}</span>
            </div>
          </div>
          {remote && (
            <RemoteDecision
              sample={
                current?.controller === sim.settings.controller ? current : null
              }
              pending={sim.pendingFrame}
              session={sim.session}
              running={sim.running}
              error={sim.error}
            />
          )}
          {manual ? (
            <div className="manual-control">
              <label className="sr-only" htmlFor="manual-steering">
                Manual steering
              </label>
              <input
                id="manual-steering"
                type="range"
                min="-2"
                max="2"
                step="0.01"
                value={sim.settings.steering}
                disabled={sim.busy}
                onChange={(event) =>
                  sim.changeSettings({ steering: Number(event.target.value) })
                }
              />
              <div className="range-labels">
                <span>−2 / LEFT</span>
                <button onClick={() => sim.changeSettings({ steering: 0 })}>
                  Center
                </button>
                <span>RIGHT / +2</span>
              </div>
              <div className="steer-buttons">
                <button
                  aria-label="Hold to steer left"
                  onPointerDown={(event) => {
                    event.currentTarget.setPointerCapture(event.pointerId);
                    keys.current.add("ArrowLeft");
                  }}
                  onPointerUp={() => {
                    keys.current.clear();
                    sim.changeSettings({ steering: 0 });
                  }}
                  onPointerCancel={() => {
                    keys.current.clear();
                    sim.changeSettings({ steering: 0 });
                  }}
                >
                  ←
                </button>
                <span>
                  Hold <kbd>A</kbd> / <kbd>D</kbd>
                  <br />
                  or arrow keys
                </span>
                <button
                  aria-label="Hold to steer right"
                  onPointerDown={(event) => {
                    event.currentTarget.setPointerCapture(event.pointerId);
                    keys.current.add("ArrowRight");
                  }}
                  onPointerUp={() => {
                    keys.current.clear();
                    sim.changeSettings({ steering: 0 });
                  }}
                  onPointerCancel={() => {
                    keys.current.clear();
                    sim.changeSettings({ steering: 0 });
                  }}
                >
                  →
                </button>
              </div>
            </div>
          ) : sim.settings.controller === "pid" ? (
            <div className="pid-settings">
              <div className="section-label">
                <h3>PID gains</h3>
                <button
                  onClick={() => {
                    sim.changeSettings({ gains: sim.catalog.defaultGains });
                    sim.record("PID gains reset");
                  }}
                  disabled={!sim.catalog}
                >
                  Reset
                </button>
              </div>
              {["p", "i", "d"].map((key) => (
                <div className="gain" key={key}>
                  <label htmlFor={`gain-${key}`}>
                    {key.toUpperCase()}{" "}
                    <span>
                      {
                        { p: "Proportional", i: "Integral", d: "Derivative" }[
                          key
                        ]
                      }
                    </span>
                  </label>
                  <output>{fixed(sim.settings.gains[key], 3)}</output>
                  <input
                    id={`gain-${key}`}
                    aria-label={`${key.toUpperCase()} gain`}
                    type="range"
                    min="-1"
                    max="1"
                    step="0.001"
                    value={sim.settings.gains[key]}
                    disabled={sim.busy}
                    onChange={(event) =>
                      sim.changeSettings({
                        gains: {
                          ...sim.settings.gains,
                          [key]: Number(event.target.value),
                        },
                      })
                    }
                  />
                </div>
              ))}
            </div>
          ) : !remote ? (
            <div className="controller-info">
              {sim.settings.controller === "zero"
                ? "Constant zero steering."
                : "Running your local Python controller."}
            </div>
          ) : null}
          <div className="live-hint">
            <span className="connection-dot" />
            {sim.running
              ? "Inputs apply on the next step"
              : "Run or step to apply inputs"}
          </div>
          <div className="session-events">
            <h3>Session</h3>
            {sim.events.map((event, index) => (
              <div key={`${event.time}-${index}`}>
                <time>{fixed(event.time, 1)}s</time>
                <span>{event.message}</span>
              </div>
            ))}
          </div>
        </aside>
        <div className="center-workspace">
          <div className="drive-container">
            <DriveView
              route={sim.route}
              samples={sim.samples}
              running={sim.running}
              speed={sim.speed}
            />
            <div className="drive-hud">
              <div>
                <span>SPEED</span>
                <b>
                  {fixed(
                    (current?.speed ?? sim.session?.plan.speed[0]) * 3.6,
                    0,
                  )}
                  <small>km/h</small>
                </b>
              </div>
              <div>
                <span>LATERAL ACCEL</span>
                <b>
                  {signed(
                    current?.actual ?? sim.session?.initialAcceleration ?? 0,
                  )}
                  <small>m/s²</small>
                </b>
              </div>
              <div>
                <span>OFFSET</span>
                <b
                  className={
                    Math.abs(current?.offset || 0) > 1.8 ? "text-amber" : ""
                  }
                >
                  {signed(current?.offset || 0)}
                  <small>m</small>
                </b>
              </div>
            </div>
            {sim.busy && (
              <div className="loading-layer">
                <span className="spinner" />
                Initializing model context…
              </div>
            )}
            {sim.session?.finished && (
              <div className="finished-layer">
                <strong>Segment complete</strong>
                <button onClick={() => void sim.start()}>↺ Reset</button>
              </div>
            )}
          </div>
          <div
            className="progress-track"
            role="progressbar"
            aria-label="Segment progress"
            aria-valuenow={Math.round((time / duration) * 100)}
            aria-valuemin="0"
            aria-valuemax="100"
          >
            <i style={{ width: `${(time / duration) * 100}%` }} />
          </div>
          <Decision
            sample={current}
            controller={current?.controller || sim.settings.controller}
          />
        </div>
        <Telemetry
          samples={sim.samples}
          costs={sim.session?.costs}
          pathMetrics={sim.session?.pathMetrics}
          finished={sim.session?.finished}
        />
      </main>
      <footer className="statusbar">
        <span>
          <i className={`live-dot ${sim.running ? "active" : ""}`} />
          {sim.busy
            ? "Initializing"
            : sim.error
              ? "Error"
              : sim.session?.finished
                ? "Complete"
                : sim.running
                  ? "Running"
                  : "Paused"}
        </span>
        <span>
          Step {sim.session?.step ?? "—"}
          <span className="footer-separator">/</span>
          {sim.session?.totalSteps ?? 600}
        </span>
        <span>10 s warm-up complete</span>
        <span className="status-right">
          {sim.settings.source === "demo"
            ? "Synthetic inputs · actual ONNX response"
            : "Dataset inputs · actual ONNX response"}
        </span>
      </footer>
    </div>
  );
}
