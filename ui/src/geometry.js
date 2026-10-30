// A kinematic projection of the model's lateral acceleration, not a second
// physics engine. Positive acceleration/steering turns right.
export const origin = () => ({ x: 0, y: 0, heading: 0 });

export function advancePose(pose, speed, lateralAcceleration, dt) {
  const velocity = Math.max(0, speed);
  const yawRate = velocity > 0.1 ? lateralAcceleration / velocity : 0;
  const heading = pose.heading + yawRate * dt;
  if (Math.abs(yawRate) < 1e-8) {
    return {
      x: pose.x + Math.sin(pose.heading) * velocity * dt,
      y: pose.y + Math.cos(pose.heading) * velocity * dt,
      heading,
    };
  }
  return {
    x:
      pose.x +
      (velocity / yawRate) * (Math.cos(pose.heading) - Math.cos(heading)),
    y:
      pose.y +
      (velocity / yawRate) * (Math.sin(heading) - Math.sin(pose.heading)),
    heading,
  };
}

export function buildRoute(plan, dt) {
  const route = [origin()];
  plan.target.forEach((target, index) => {
    route.push(advancePose(route.at(-1), plan.speed[index], target, dt));
  });
  return route;
}

// Cumulative world-space distance keeps dashed markings fixed when the
// renderer crops the route or changes camera scale.
export function routeDistances(route) {
  const distances = [0];
  for (let index = 1; index < route.length; index++) {
    const previous = route[index - 1];
    const point = route[index];
    distances.push(
      distances[index - 1] +
        Math.hypot(point.x - previous.x, point.y - previous.y),
    );
  }
  return route.length ? distances : [];
}

export function lateralOffset(pose, reference) {
  return (
    (pose.x - reference.x) * Math.cos(reference.heading) -
    (pose.y - reference.y) * Math.sin(reference.heading)
  );
}

export function interpolatePose(from, to, amount) {
  const t = Math.min(1, Math.max(0, amount));
  return Object.fromEntries(
    ["x", "y", "heading"].map((key) => [
      key,
      from[key] + (to[key] - from[key]) * t,
    ]),
  );
}
