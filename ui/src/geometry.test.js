import test from "node:test";
import assert from "node:assert/strict";
import {
  advancePose,
  buildRoute,
  lateralOffset,
  interpolatePose,
  origin,
  routeDistances,
} from "./geometry.js";

const close = (actual, expected) =>
  assert.ok(Math.abs(actual - expected) < 1e-8, `${actual} != ${expected}`);

test("road distance follows the polyline through turns", () => {
  assert.deepEqual(
    routeDistances([
      { x: 0, y: 0 },
      { x: 3, y: 4 },
      { x: 3, y: 12 },
    ]),
    [0, 5, 13],
  );
  assert.deepEqual(routeDistances([]), []);
});

test("road marking distance follows speed rather than sample count", () => {
  const route = buildRoute({ target: [0, 0, 0], speed: [20, 40, 80] }, 0.1);
  assert.deepEqual(routeDistances(route), [0, 2, 6, 14]);
});

test("zero lateral acceleration travels straight at the supplied speed", () => {
  const pose = advancePose(origin(), 20, 0, 0.1);
  assert.deepEqual(pose, { x: 0, y: 2, heading: 0 });
});

test("positive acceleration turns right on the expected circular arc", () => {
  const pose = advancePose(origin(), 10, 5, Math.PI);
  close(pose.heading, Math.PI / 2);
  close(pose.x, 20);
  close(pose.y, 20);
});

test("negative acceleration mirrors a right turn", () => {
  const right = advancePose(origin(), 20, 1, 2);
  const left = advancePose(origin(), 20, -1, 2);
  close(left.x, -right.x);
  close(left.y, right.y);
  close(left.heading, -right.heading);
});

test("a stationary vehicle stays finite and stationary", () => {
  assert.deepEqual(advancePose(origin(), 0, 5, 0.1), origin());
});

test("matching the target acceleration follows the same projected route", () => {
  const plan = { target: [0.3, 1, -0.8, 0.1], speed: [20, 21, 22, 23] };
  const route = buildRoute(plan, 0.1);
  let actual = origin();
  for (let index = 0; index < plan.target.length; index++) {
    actual = advancePose(actual, plan.speed[index], plan.target[index], 0.1);
    close(lateralOffset(actual, route[index + 1]), 0);
  }
  assert.equal(route.length, plan.target.length + 1);
});

test("offset uses the reference heading rather than world x", () => {
  close(
    lateralOffset({ x: 10, y: -3 }, { x: 0, y: 0, heading: Math.PI / 2 }),
    3,
  );
});

test("different steering responses create different ride projections", () => {
  let straight = origin(),
    turning = origin();
  for (let index = 0; index < 100; index++) {
    straight = advancePose(straight, 20, 0, 0.1);
    turning = advancePose(turning, 20, 1, 0.1);
  }
  assert.ok(lateralOffset(turning, straight) > 40);
});

test("render interpolation never extrapolates the model response", () => {
  const end = { x: 4, y: 8, heading: 0.2 };
  assert.deepEqual(interpolatePose(origin(), end, -1), origin());
  assert.deepEqual(interpolatePose(origin(), end, 2), end);
  assert.deepEqual(interpolatePose(origin(), end, 0.5), {
    x: 2,
    y: 4,
    heading: 0.1,
  });
});
