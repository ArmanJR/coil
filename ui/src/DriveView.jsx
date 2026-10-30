import { useEffect, useMemo, useRef, useState } from "react";
import { interpolatePose, origin, routeDistances } from "./geometry.js";

const BLUE = "#8ab8ff";
const GREEN = "#61d4ad";

function line(ctx, points, color, width, dash = [], dashOffset = 0) {
  if (points.length < 2) return;
  ctx.beginPath();
  points.forEach((point, index) =>
    index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y),
  );
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.setLineDash(dash);
  ctx.lineDashOffset = dashOffset;
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.lineDashOffset = 0;
}

function offsetRoute(route, offset) {
  return route.map((point) => ({
    ...point,
    x: point.x + Math.cos(point.heading) * offset,
    y: point.y - Math.sin(point.heading) * offset,
  }));
}

function car(ctx, point, angle, scale, ghost, steering) {
  ctx.save();
  ctx.translate(point.x, point.y);
  ctx.rotate(angle);
  const unit = Math.max(3, scale);
  ctx.scale(unit, unit);
  if (ghost) {
    ctx.fillStyle = "#61d4ad14";
    ctx.strokeStyle = "#61d4ad8a";
    ctx.lineWidth = 1 / unit;
    ctx.setLineDash([3 / unit, 4 / unit]);
    ctx.beginPath();
    ctx.roundRect(-0.95, -2.2, 1.9, 4.4, 0.35);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
    return;
  }
  // Tire angle illustrates the normalized steering command, not a measured
  // steering-wheel or road-wheel angle.
  for (const side of [-1, 1]) {
    ctx.save();
    ctx.translate(side * 1.05, -1.3);
    ctx.rotate(steering * 0.25);
    ctx.fillStyle = "#8f9daf";
    ctx.fillRect(-0.19, -0.55, 0.38, 1.1);
    ctx.restore();
    ctx.fillStyle = "#536172";
    ctx.fillRect(side * 1.05 - 0.19, 0.95, 0.38, 1);
  }
  ctx.shadowColor = "#000a";
  ctx.shadowBlur = 15;
  ctx.shadowOffsetY = 5;
  ctx.fillStyle = "#a4c5f5";
  ctx.beginPath();
  ctx.roundRect(-0.95, -2.2, 1.9, 4.4, 0.38);
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.shadowOffsetY = 0;
  ctx.fillStyle = "#243c58";
  ctx.beginPath();
  ctx.roundRect(-0.76, -1.17, 1.52, 2.35, 0.25);
  ctx.fill();
  ctx.fillStyle = "#4f6f91";
  ctx.fillRect(-0.64, -0.35, 1.28, 0.95);
  ctx.fillStyle = "#f0f5ff";
  ctx.fillRect(-0.78, -2, 0.4, 0.12);
  ctx.fillRect(0.38, -2, 0.4, 0.12);
  ctx.fillStyle = "#e87d7d";
  ctx.fillRect(-0.78, 1.95, 0.4, 0.12);
  ctx.fillRect(0.38, 1.95, 0.4, 0.12);
  ctx.restore();
}

function render(ctx, size, data, now, view, zoom, showGhost) {
  const { width, height, dpr } = size;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#15191f";
  ctx.fillRect(0, 0, width, height);
  if (!data?.route?.length) return;
  const { route, distances, samples, current, previous, receivedAt, speed } =
    data;
  const progress = (now - receivedAt) / (100 / speed);
  const actual = interpolatePose(
    previous?.pose || origin(),
    current?.pose || origin(),
    progress,
  );
  const reference = interpolatePose(
    previous?.reference || route[0],
    current?.reference || route[0],
    progress,
  );
  const index = samples.length;
  const dx = actual.x - reference.x;
  const dy = actual.y - reference.y;
  const lateral =
    dx * Math.cos(reference.heading) - dy * Math.sin(reference.heading);
  const longitudinal =
    dx * Math.sin(reference.heading) + dy * Math.cos(reference.heading);
  let scale = Math.min(
    12 * zoom,
    (width * 0.65) / (Math.abs(lateral) + 10),
    (height * 0.5) / (Math.abs(longitudinal) + 10),
  );
  let center = {
    x: (actual.x + reference.x) / 2,
    y: (actual.y + reference.y) / 2,
  };
  let cameraHeading = reference.heading;
  let anchorY = height * 0.69;
  const firstVisible = Math.max(0, index - 45);
  let markingOffset = distances[firstVisible];
  let visibleRoute = route.slice(
    firstVisible,
    Math.min(route.length, index + 110),
  );
  if (index < 45) {
    visibleRoute.unshift({ x: 0, y: -120, heading: 0 });
    markingOffset = -120;
  }
  if (view === "overview") {
    const points = [...route, ...samples.map((sample) => sample.pose)];
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const minX = Math.min(...xs),
      maxX = Math.max(...xs);
    const minY = Math.min(...ys),
      maxY = Math.max(...ys);
    center = { x: (minX + maxX) / 2, y: (minY + maxY) / 2 };
    scale = Math.min(
      (width - 90) / (maxX - minX + 20),
      (height - 100) / (maxY - minY + 20),
    );
    cameraHeading = 0;
    anchorY = height / 2;
    visibleRoute = route;
    markingOffset = 0;
  }
  const project = (point) => {
    const x = point.x - center.x;
    const y = point.y - center.y;
    return {
      x:
        width / 2 +
        (x * Math.cos(cameraHeading) - y * Math.sin(cameraHeading)) * scale,
      y:
        anchorY -
        (x * Math.sin(cameraHeading) + y * Math.cos(cameraHeading)) * scale,
    };
  };
  // World-space grid makes forward movement and lateral drift legible.
  const gridSize = Math.max(10, Math.ceil(6 / scale) * 10);
  const extent = Math.max(width, height) / scale;
  const startX = Math.floor((center.x - extent) / gridSize) * gridSize;
  const startY = Math.floor((center.y - extent) / gridSize) * gridSize;
  for (let x = startX; x < center.x + extent; x += gridSize) {
    line(
      ctx,
      [
        project({ x, y: center.y - extent }),
        project({ x, y: center.y + extent }),
      ],
      "#20262d",
      1,
    );
  }
  for (let y = startY; y < center.y + extent; y += gridSize) {
    line(
      ctx,
      [
        project({ x: center.x - extent, y }),
        project({ x: center.x + extent, y }),
      ],
      "#20262d",
      1,
    );
  }
  const road = visibleRoute.map(project);
  line(ctx, road, "#1b2128", Math.max(6, 11.2 * scale));
  line(ctx, road, "#303741", Math.max(4, 7.4 * scale));
  line(ctx, road, "#272e37", Math.max(3, 7.1 * scale));
  for (const side of [-3.6, 3.6]) {
    line(
      ctx,
      offsetRoute(visibleRoute, side).map(project),
      "#84919c",
      Math.max(0.7, 0.1 * scale),
    );
  }
  line(
    ctx,
    road,
    "#61d4ad8c",
    Math.max(1, 0.12 * scale),
    [5 * scale, 4 * scale],
    markingOffset * scale,
  );
  const trail = [
    origin(),
    ...samples.slice(0, -1).map((sample) => sample.pose),
    actual,
  ];
  line(
    ctx,
    trail.slice(view === "overview" ? 0 : -230).map(project),
    "#8ab8ff28",
    7,
  );
  line(ctx, trail.slice(view === "overview" ? 0 : -230).map(project), BLUE, 2);
  const point = project(actual);
  const referencePoint = project(reference);
  if (showGhost) {
    line(ctx, [referencePoint, point], "#ddad6466", 1, [3, 5]);
    car(ctx, referencePoint, reference.heading - cameraHeading, scale, true, 0);
  }
  car(
    ctx,
    point,
    actual.heading - cameraHeading,
    scale,
    false,
    current?.steering || 0,
  );
  if (view === "follow") {
    ctx.save();
    ctx.translate(point.x, point.y);
    ctx.rotate(actual.heading - cameraHeading);
    const force = (current?.actual || 0) * 18;
    if (Math.abs(force) > 3) {
      line(
        ctx,
        [
          { x: 0, y: 0 },
          { x: force, y: 0 },
        ],
        "#8ab8ffb0",
        2,
      );
      line(
        ctx,
        [
          { x: force - Math.sign(force) * 5, y: -4 },
          { x: force, y: 0 },
          { x: force - Math.sign(force) * 5, y: 4 },
        ],
        BLUE,
        2,
      );
    }
    ctx.restore();
  }
  ctx.font = "10px ui-monospace, monospace";
  ctx.textAlign = "left";
  ctx.fillStyle = "#9ba8b8";
  const barMeters = scale > 3 ? 10 : 100;
  const barWidth = barMeters * scale;
  line(
    ctx,
    [
      { x: 22, y: height - 27 },
      { x: 22 + barWidth, y: height - 27 },
    ],
    "#697482",
    1,
  );
  ctx.fillText(`${barMeters} m`, 22, height - 36);
  ctx.fillStyle = GREEN;
  ctx.beginPath();
  ctx.arc(referencePoint.x, referencePoint.y, 2, 0, Math.PI * 2);
  ctx.fill();
}

export default function DriveView({ route, samples, running, speed }) {
  const canvasRef = useRef(null);
  const dataRef = useRef(null);
  const controlsRef = useRef({ view: "follow", zoom: 1, ghost: true });
  const [view, setView] = useState("follow");
  const [zoom, setZoom] = useState(1);
  const [ghost, setGhost] = useState(true);
  const distances = useMemo(() => routeDistances(route), [route]);
  useEffect(() => {
    const current = samples.at(-1);
    const previousData = dataRef.current;
    dataRef.current = {
      route,
      distances,
      speed,
      samples,
      current,
      previous: samples.at(-2),
      receivedAt:
        previousData?.current === current && previousData?.route === route
          ? previousData.receivedAt
          : performance.now(),
    };
  }, [route, distances, samples, speed]);
  useEffect(() => {
    controlsRef.current = { view, zoom, ghost };
  }, [view, zoom, ghost]);
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d", { alpha: false });
    let size = { width: 1, height: 1, dpr: 1 };
    let animation;
    let lastPaint = 0;
    let lastData = null;
    let lastControls = null;
    let resized = true;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      size = { width, height, dpr };
      resized = true;
    });
    observer.observe(canvas);
    const frame = (now) => {
      const data = dataRef.current;
      const controls = controlsRef.current;
      // Paint interpolated motion at up to 30 fps; paused views need no redraw.
      const moving = data && now - data.receivedAt < 100 / data.speed + 50;
      if (
        now - lastPaint >= 30 &&
        (resized || moving || data !== lastData || controls !== lastControls)
      ) {
        const { view: mode, zoom: factor, ghost: show } = controls;
        render(ctx, size, data, now, mode, factor, show);
        lastPaint = now;
        lastData = data;
        lastControls = controls;
        resized = false;
      }
      animation = requestAnimationFrame(frame);
    };
    animation = requestAnimationFrame(frame);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(animation);
    };
  }, []);
  return (
    <section className="drive-view" aria-label="Live driving visualization">
      <canvas
        ref={canvasRef}
        aria-label="Projected car motion from live model acceleration; green is the target path, blue is your drive"
      />
      <div className="view-top">
        <div className="view-title">
          <span className={`live-dot ${running ? "active" : ""}`} />
          {running ? "LIVE" : "PAUSED"}
          <span className="view-divider" />
          DRIVING VIEW
        </div>
        <div className="view-switch">
          <button
            className={view === "follow" ? "selected" : ""}
            onClick={() => setView("follow")}
          >
            Follow
          </button>
          <button
            className={view === "overview" ? "selected" : ""}
            onClick={() => setView("overview")}
          >
            Full route
          </button>
        </div>
      </div>
      <div className="view-legend">
        <span>
          <i className="legend-blue" />
          Your drive
        </span>
        <span>
          <i className="legend-green" />
          Target path
        </span>
        <label>
          <input
            type="checkbox"
            checked={ghost}
            onChange={(event) => setGhost(event.target.checked)}
          />
          Target ghost
        </label>
      </div>
      <div className="zoom-controls">
        <button
          disabled={view !== "follow"}
          onClick={() => setZoom((value) => Math.min(2.5, value + 0.25))}
          aria-label="Zoom in"
        >
          +
        </button>
        <button
          disabled={view !== "follow"}
          onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))}
          aria-label="Zoom out"
        >
          −
        </button>
      </div>
      <div
        className="projection-note"
        title="Position is a kinematic projection: yaw rate = lateral acceleration / speed. The ONNX model predicts acceleration, not vehicle position. Road width is illustrative."
      >
        Kinematic projection <span>ⓘ</span>
      </div>
    </section>
  );
}
