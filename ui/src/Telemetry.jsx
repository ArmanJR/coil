import { useId } from "react";

export const fixed = (value, digits = 2) =>
  Number.isFinite(value) ? value.toFixed(digits) : "—";
export const signed = (value, digits = 2) =>
  Number.isFinite(value)
    ? `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`
    : "—";

function Chart({ samples, field, title, unit, target = false, limit = 2 }) {
  const clipId = useId();
  const values = samples.slice(-180);
  const last = values.at(-1);
  const max = Math.max(
    limit,
    ...values.map((sample) => Math.abs(sample[field])),
    ...(target ? values.map((sample) => Math.abs(sample.target)) : []),
  );
  const timeEnd = Math.max(18, last?.time || 0);
  const timeStart = timeEnd - 18;
  const x = (time) => 34 + ((time - timeStart) / 18) * 362;
  const y = (value) => 62 - (value / max) * 43;
  const path = (key) =>
    values
      .map(
        (sample, index) =>
          `${index ? "L" : "M"}${x(sample.time).toFixed(1)},${y(sample[key]).toFixed(1)}`,
      )
      .join(" ");
  return (
    <div className="telemetry-chart">
      <div className="chart-title">
        <h3>{title}</h3>
        <span>
          <b>{signed(last?.[field])}</b> {unit}
        </span>
      </div>
      <svg
        viewBox="0 0 410 126"
        role="img"
        aria-label={`${title} for the last 18 seconds`}
      >
        <defs>
          <clipPath id={clipId}>
            <rect x="34" y="15" width="362" height="92" />
          </clipPath>
        </defs>
        {[-1, 0, 1].map((tick) => (
          <g key={tick}>
            <line
              x1="34"
              x2="396"
              y1={y(tick * max)}
              y2={y(tick * max)}
              className="grid-line"
            />
            <text x="27" y={y(tick * max) + 3} textAnchor="end">
              {fixed(tick * max, 1)}
            </text>
          </g>
        ))}
        {[0, 6, 12, 18].map((tick) => (
          <text key={tick} x={x(timeStart + tick)} y="121" textAnchor="middle">
            {fixed(timeStart + tick, 0)}s
          </text>
        ))}
        <g clipPath={`url(#${clipId})`}>
          {target && <path d={path("target")} className="target-line" />}
          <path d={path(field)} className="response-line" />
          {last && (
            <circle
              cx={x(last.time)}
              cy={y(last[field])}
              r="2.7"
              fill="#8ab8ff"
            />
          )}
        </g>
      </svg>
    </div>
  );
}

export default function Telemetry({ samples, costs, finished }) {
  const current = samples.at(-1);
  return (
    <aside className="telemetry-panel">
      <div className="panel-title">
        <h2>Telemetry</h2>
        <span className="small-label">10 Hz</span>
      </div>
      <div className="telemetry-legend">
        <span>
          <i className="legend-blue" />
          Actual
        </span>
        <span>
          <i className="legend-green" />
          Target
        </span>
      </div>
      <Chart
        samples={samples}
        field="actual"
        title="Lateral acceleration"
        unit="m/s²"
        target
      />
      <Chart
        samples={samples}
        field="steering"
        title="Applied steering"
        unit=""
      />
      <Chart
        samples={samples}
        field="jerk"
        title="Lateral jerk"
        unit="m/s³"
        limit={5}
      />
      <div className="signals">
        <div>
          <span>Speed</span>
          <b>
            {fixed(current?.speed, 1)} <small>m/s</small>
          </b>
        </div>
        <div>
          <span>Road roll effect</span>
          <b>
            {signed(current?.roll)} <small>m/s²</small>
          </b>
        </div>
        <div>
          <span>Projected lateral offset</span>
          <b
            className={Math.abs(current?.offset || 0) > 1.8 ? "text-amber" : ""}
          >
            {signed(current?.offset)} <small>m</small>
          </b>
        </div>
      </div>
      <div className="score-heading">
        <h3>Score</h3>
        <span>{finished || current?.step >= 499 ? "FINAL" : "RUNNING"}</span>
      </div>
      <div className="scores">
        <div>
          <span>Tracking × 50</span>
          <b>{fixed(costs?.lataccel_cost * 50, 1)}</b>
        </div>
        <div>
          <span>Jerk</span>
          <b>{fixed(costs?.jerk_cost, 1)}</b>
        </div>
        <div className="score-total">
          <span>Total</span>
          <b>{fixed(costs?.total_cost, 1)}</b>
        </div>
      </div>
      <div className="score-note">
        First 40 s after warm-up · lower is better
      </div>
    </aside>
  );
}
