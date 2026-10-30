import test from "node:test";
import assert from "node:assert/strict";
import { Worker } from "node:worker_threads";

// Run the actual browser worker with a Node message bridge and a deterministic
// HTTP fixture. Real ONNX and session semantics are covered by Python tests.
async function harness(t, id = "test") {
  const worker = new Worker(
    `
    const { parentPort, workerData } = require('node:worker_threads');
    globalThis.self = { postMessage: message => parentPort.postMessage(message) };
    const steps = new Map();
    globalThis.fetch = async (_url, options) => {
      const request = JSON.parse(options.body);
      await new Promise(resolve => setTimeout(resolve, 8));
      const previous = steps.get(request.id) ?? 99;
      if (request.expectedStep !== previous || request.steering === 99) {
        return new Response(JSON.stringify({error:'Fixture rejected step'}), {status:400});
      }
      const step = previous + 1;
      steps.set(request.id, step);
      return new Response(JSON.stringify({step, finished:request.id==='short' && step===103, sample:{steering:request.steering}, received:request}));
    };
    parentPort.on('message', data => self.onmessage({data}));
    import(workerData.module).then(() => parentPort.postMessage({type:'ready'}));
  `,
    {
      eval: true,
      workerData: {
        module: new URL("./simulation.worker.js", import.meta.url).href,
      },
    },
  );
  const messages = [];
  worker.on("message", (message) => messages.push(message));
  t.after(() => worker.terminate());
  const wait = (predicate) =>
    new Promise((resolve, reject) => {
      const found = messages.find(predicate);
      if (found) {
        resolve(found);
        return;
      }
      const timer = setTimeout(() => {
        worker.off("message", onMessage);
        reject(new Error("Worker response timed out"));
      }, 3000);
      const onMessage = (message) => {
        if (predicate(message)) {
          clearTimeout(timer);
          worker.off("message", onMessage);
          resolve(message);
        }
      };
      worker.on("message", onMessage);
    });
  await wait((message) => message.type === "ready");
  worker.postMessage({
    type: "bind",
    session: { id, step: 99, finished: false },
    settings: { controller: "manual", steering: 0 },
  });
  return {
    worker,
    messages,
    wait,
    samples: () => messages.filter((message) => message.type === "sample"),
  };
}

test("worker applies input changes to the next inference step", async (t) => {
  const h = await harness(t);
  h.worker.postMessage({
    type: "settings",
    settings: { controller: "manual", steering: 1.25 },
  });
  h.worker.postMessage({ type: "step" });
  const first = await h.wait((message) => message.type === "sample");
  assert.equal(first.result.sample.steering, 1.25);
  h.worker.postMessage({
    type: "settings",
    settings: { controller: "manual", steering: -0.5 },
  });
  h.worker.postMessage({ type: "step" });
  const second = await h.wait(
    (message) => message.type === "sample" && message.result.step === 101,
  );
  assert.equal(second.result.sample.steering, -0.5);
});

test("pause acknowledges the in-flight step and then stays stopped", async (t) => {
  const h = await harness(t);
  h.worker.postMessage({ type: "run" });
  await h.wait((message) => message.type === "sample");
  h.worker.postMessage({ type: "pause", requestId: 1 });
  await h.wait(
    (message) => message.type === "paused" && message.requestId === 1,
  );
  const count = h.samples().length;
  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.equal(h.samples().length, count);
});

test("rapid pause/resume does not create duplicate simulation clocks", async (t) => {
  const h = await harness(t);
  h.worker.postMessage({ type: "run" });
  h.worker.postMessage({ type: "pause", requestId: 1 });
  h.worker.postMessage({ type: "run" });
  h.worker.postMessage({ type: "pause", requestId: 2 });
  h.worker.postMessage({ type: "run" });
  await new Promise((resolve) => setTimeout(resolve, 550));
  h.worker.postMessage({ type: "pause", requestId: 3 });
  await h.wait(
    (message) => message.type === "paused" && message.requestId === 3,
  );
  assert.ok(h.samples().length >= 4 && h.samples().length <= 7);
  assert.equal(
    new Set(h.samples().map((message) => message.result.step)).size,
    h.samples().length,
  );
});

test("reload applies once and advances the paused simulation", async (t) => {
  const h = await harness(t);
  h.worker.postMessage({ type: "reload" });
  const first = await h.wait((message) => message.type === "sample");
  assert.equal(first.result.received.reload, true);
  h.worker.postMessage({ type: "step" });
  const second = await h.wait(
    (message) => message.type === "sample" && message.result.step === 101,
  );
  assert.equal(second.result.received.reload, false);
});

test("completed segments stop their clock", async (t) => {
  const h = await harness(t, "short");
  h.worker.postMessage({ type: "run" });
  await h.wait(
    (message) => message.type === "running" && message.value === false,
  );
  assert.equal(h.samples().length, 4);
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal(h.samples().length, 4);
});

test("an HTTP failure stops the clock and surfaces the server error", async (t) => {
  const h = await harness(t);
  h.worker.postMessage({
    type: "settings",
    settings: { controller: "manual", steering: 99 },
  });
  h.worker.postMessage({ type: "run" });
  const failure = await h.wait((message) => message.type === "error");
  assert.equal(failure.message, "Fixture rejected step");
  assert.equal(h.samples().length, 0);
});
