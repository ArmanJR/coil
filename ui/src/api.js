export const logger = {
  info: (message, details = {}) =>
    console.info(`[TinyPhysics] ${message}`, details),
  error: (message, error) => console.error(`[TinyPhysics] ${message}`, error),
};

export async function api(path, body) {
  const response = await fetch(
    `/api/${path}`,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const result = await response.json();
  if (!response.ok)
    throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
