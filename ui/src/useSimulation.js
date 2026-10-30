import { useEffect, useRef, useState } from "react";
import { api, logger } from "./api.js";
import { advancePose, buildRoute, lateralOffset, origin } from "./geometry.js";

export const defaults = {
  source: "demo",
  scenario: "sweeping",
  segment: "",
  controller: "pid",
  gains: { p: 0.195, i: 0.1, d: -0.053 },
  steering: 0,
};

export default function useSimulation() {
  const [catalog, setCatalog] = useState(null);
  const [settings, setSettings] = useState(defaults);
  const [session, setSession] = useState(null);
  const [samples, setSamples] = useState([]);
  const [route, setRoute] = useState([]);
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [speed, setSpeed] = useState(1);
  const [events, setEvents] = useState([]);
  const [pendingFrame, setPendingFrame] = useState(null);
  const controls = useRef(defaults);
  const live = useRef(null);
  const history = useRef([]);
  const road = useRef([]);
  const playing = useRef(false);
  const worker = useRef(null);
  const pendingPause = useRef(new Map());
  const nextRequest = useRef(0);
  const paint = useRef(null);
  const alive = useRef(true);

  function record(message) {
    const time = history.current.at(-1)?.time || 0;
    setEvents((items) => [{ time, message }, ...items].slice(0, 5));
    logger.info(message, { time });
  }

  function pause(waitForStep = false) {
    playing.current = false;
    if (alive.current) setRunning(false);
    const requestId = ++nextRequest.current;
    const promise =
      waitForStep && worker.current
        ? new Promise((resolve) => pendingPause.current.set(requestId, resolve))
        : Promise.resolve();
    worker.current?.postMessage({ type: "pause", requestId });
    return promise;
  }

  function changeSettings(update) {
    controls.current = { ...controls.current, ...update };
    setSettings(controls.current);
    worker.current?.postMessage({
      type: "settings",
      settings: controls.current,
    });
  }

  async function start(input = controls.current) {
    setBusy(true);
    setError("");
    await pause(true);
    try {
      const response = await api("live/start", {
        ...input,
        previousId: live.current?.id,
      });
      if (!alive.current) {
        await api("live/close", { id: response.id });
        return;
      }
      if (paint.current !== null) cancelAnimationFrame(paint.current);
      paint.current = null;
      live.current = response;
      road.current = buildRoute(response.plan, response.dt);
      history.current = [];
      changeSettings({ ...input, steering: 0 });
      worker.current.postMessage({
        type: "bind",
        session: response,
        settings: controls.current,
      });
      setSession(response);
      setRoute(road.current);
      setSamples([]);
      setEvents([{ time: 0, message: "Ready · warm-up complete" }]);
      logger.info("Live session ready", {
        controller: input.controller,
        source: input.source,
        ...(input.source === "dataset"
          ? { segment: input.segment }
          : { scenario: input.scenario }),
        totalSteps: response.totalSteps,
      });
    } catch (failure) {
      logger.error("Could not start simulation", failure);
      if (alive.current) setError(failure.message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  function step() {
    if (!busy && live.current && !error)
      worker.current?.postMessage({ type: "step" });
  }

  function toggle() {
    if (playing.current) {
      void pause();
      return;
    }
    if (!live.current || live.current.finished || error || busy) return;
    playing.current = true;
    setRunning(true);
    worker.current?.postMessage({ type: "run" });
  }

  function changeSpeed(value) {
    setSpeed(value);
    worker.current?.postMessage({ type: "speed", value });
    logger.info("Simulation rate changed", { speed: value });
  }

  function reloadCode() {
    record("Reload requested · controller memory resets");
    worker.current?.postMessage({ type: "reload" });
  }

  useEffect(() => {
    alive.current = true;
    const clock = new Worker(
      new URL("./simulation.worker.js", import.meta.url),
      { type: "module" },
    );
    worker.current = clock;
    clock.onmessage = ({ data }) => {
      if (!alive.current) return;
      if (data.type === "paused") {
        pendingPause.current.get(data.requestId)?.();
        pendingPause.current.delete(data.requestId);
      } else if (data.type === "pending" && live.current?.id === data.id) {
        setPendingFrame(data.frame === null ? null : data);
      } else if (data.type === "running") {
        playing.current = data.value;
        setRunning(data.value);
      } else if (data.type === "error") {
        playing.current = false;
        setRunning(false);
        setError(`${data.message} Reset to resume safely.`);
      } else if (data.type === "sample" && live.current?.id === data.id) {
        const response = data.result;
        if (response.sample) {
          const previous = history.current.at(-1)?.pose || origin();
          const pose = advancePose(
            previous,
            response.sample.speed,
            response.sample.actual,
            response.dt,
          );
          const reference = road.current[history.current.length + 1];
          const sample = {
            ...response.sample,
            pose,
            reference,
            offset: lateralOffset(pose, reference),
          };
          history.current = [...history.current, sample];
        }
        live.current = { ...live.current, ...response };
        // Preserve every physics sample, but coalesce UI updates when rendering
        // falls behind. The simulation clock runs independently in the worker.
        if (paint.current === null) {
          paint.current = requestAnimationFrame(() => {
            paint.current = null;
            if (!alive.current) return;
            setSamples(history.current);
            setSession(live.current);
          });
        }
        if (response.finished) record("Segment complete");
      }
    };
    clock.onerror = (failure) => {
      logger.error("Simulation worker failed", failure.message);
      playing.current = false;
      setRunning(false);
      setBusy(false);
      setError("Simulation clock failed. Reload this page to reconnect.");
      for (const resolve of pendingPause.current.values()) resolve();
      pendingPause.current.clear();
    };
    api("config")
      .then(async (result) => {
        if (!alive.current) return;
        setCatalog(result);
        await start({ ...defaults, gains: result.defaultGains });
      })
      .catch((failure) => {
        logger.error("Simulator connection failed", failure);
        if (alive.current) {
          setError("Cannot reach the Python server. Run uv run ui/server.py.");
          setBusy(false);
        }
      });
    const hidden = () => {
      if (document.hidden) void pause();
    };
    const close = () => {
      void pause();
      if (live.current?.id) {
        fetch("/api/live/close", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: live.current.id }),
          keepalive: true,
        }).catch((failure) => logger.error("Session cleanup failed", failure));
      }
    };
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", close);
    return () => {
      alive.current = false;
      close();
      clock.terminate();
      worker.current = null;
      if (paint.current !== null) cancelAnimationFrame(paint.current);
      for (const resolve of pendingPause.current.values()) resolve();
      pendingPause.current.clear();
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", close);
    };
    // Own one worker/connection for this component's lifetime. Async handlers
    // access current simulation state via refs, rather than render closures.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    catalog,
    settings,
    controls,
    session,
    samples,
    route,
    running,
    busy,
    error,
    speed,
    events,
    pendingFrame,
    start,
    step,
    toggle,
    pause,
    changeSettings,
    changeSpeed,
    reloadCode,
    record,
  };
}
