import { api, logger } from "./api.js";

let session = null;
let settings = null;
let running = false;
let pending = null;
let timer = null;
let generation = 0;
let speed = 1;
let reloadNext = false;
let failed = false;

function pause() {
  running = false;
  generation += 1;
  clearTimeout(timer);
}

async function advance() {
  if (pending) return pending;
  if (!session || session.finished || failed) return;
  const id = session.id;
  const reload = reloadNext;
  reloadNext = false;
  self.postMessage({
    type: "pending",
    id,
    frame: session.step + 1,
    controller: settings.controller,
  });
  pending = (async () => {
    try {
      const result = await api("live/step", {
        ...settings,
        id,
        expectedStep: session.step,
        reload,
      });
      session = { ...session, ...result };
      self.postMessage({ type: "sample", id, result });
      if (result.finished) {
        pause();
        self.postMessage({ type: "running", value: false });
      }
    } catch (error) {
      failed = true;
      pause();
      logger.error("Live step failed", error);
      self.postMessage({ type: "error", message: error.message });
    }
  })();
  try {
    await pending;
  } finally {
    pending = null;
    self.postMessage({ type: "pending", id, frame: null });
  }
}

async function tick(token, deadline = performance.now()) {
  if (!running || token !== generation) return;
  await advance();
  if (running && token === generation) {
    const next = Math.max(deadline + 100 / speed, performance.now() - 100);
    timer = setTimeout(
      () => tick(token, next),
      Math.max(0, next - performance.now()),
    );
  }
}

self.onmessage = async ({ data }) => {
  switch (data.type) {
    case "bind":
      pause();
      session = data.session;
      settings = data.settings;
      failed = false;
      reloadNext = false;
      logger.info("Live simulation clock ready");
      break;
    case "settings":
      settings = data.settings;
      break;
    case "speed":
      speed = data.value;
      break;
    case "run":
      if (!session || session.finished || failed || running) return;
      running = true;
      logger.info("Simulation clock started", { speed });
      void tick(++generation);
      break;
    case "pause":
      pause();
      await pending;
      self.postMessage({ type: "paused", requestId: data.requestId });
      break;
    case "step":
      if (running || pending) return;
      await advance();
      break;
    case "reload":
      if (pending) return;
      reloadNext = true;
      if (!running) {
        await pending;
        await advance();
      }
      break;
    default:
      logger.error("Unknown worker command", data.type);
  }
};
