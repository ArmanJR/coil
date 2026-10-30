import { fixed, signed } from "./Telemetry.jsx";

export default function RemoteDecision({
  sample,
  pending,
  session,
  running,
  error,
}) {
  const decision = sample?.decision;
  const completed = session ? session.step - session.controlStart + 1 : 0;
  const total = session ? session.totalSteps - session.controlStart : 0;
  return (
    <section className="remote-decision" aria-label="Remote frame decisions">
      <div className="panel-title">
        <h2>Frame-by-frame decisions</h2>
        <span className="small-label">OPENROUTER</span>
      </div>
      <p className="remote-status" role="status">
        {pending ? (
          <>
            <span className="spinner" /> Waiting for {pending.controller} ·
            frame #{pending.frame}
          </>
        ) : error ? (
          "Decision stopped · reset to continue"
        ) : session?.finished ? (
          "Segment complete"
        ) : (
          "Ready for the next frame"
        )}
      </p>
      <p className="remote-note">
        Identical frames reuse cached decisions. Uncached frames make one paid
        request. Each answer advances simulation time by 100 ms.{" "}
        {running
          ? "Pause stops after the pending answer."
          : "Step asks once. Run asks continuously."}
      </p>
      <div className="remote-metrics">
        <div>
          <span>Active frames</span>
          <b>
            {completed} / {total}
          </b>
        </div>
        <div>
          <span>API requests since load</span>
          <b>{decision?.requests ?? 0}</b>
        </div>
        <div>
          <span>Cache hits since load</span>
          <b>{decision?.cacheHits ?? 0}</b>
        </div>
        <div>
          <span>Last latency</span>
          <b>{decision ? `${fixed(decision.latencyMs, 0)} ms` : "—"}</b>
        </div>
        <div>
          <span>Reported cost since load</span>
          <b>
            {decision?.totalCost != null
              ? `$${fixed(decision.totalCost, 6)}`
              : "—"}
          </b>
        </div>
      </div>
      {decision && (
        <>
          <div className="remote-result">
            <strong>
              {decision.choice.replaceAll("_", " ")} →{" "}
              {signed(sample.steering, 3)}
            </strong>
            <span>
              {decision.cacheHit ? "Cached · no API charge" : "OpenRouter"} ·{" "}
              Confidence {fixed(decision.confidence * 100, 0)}% · frame #
              {sample.step}
            </span>
          </div>
          <p className="remote-note">
            Confidence describes the choice distribution, not driving accuracy.
            Steering is in simulator units, not degrees.
          </p>
          {decision.inputs.path_recovery && (
            <div className="remote-metrics">
              <div>
                <span>Path correction</span>
                <b>
                  {signed(decision.inputs.path_recovery.correction_mps2, 3)}{" "}
                  m/s²
                </b>
              </div>
              <div>
                <span>Recovery acceleration target</span>
                <b>
                  {signed(
                    decision.inputs.path_recovery.corrected_target_lataccel,
                    3,
                  )}{" "}
                  m/s²
                </b>
              </div>
            </div>
          )}
          <details>
            <summary>Decision alternatives &amp; frame inputs</summary>
            <table>
              <thead>
                <tr>
                  <th>Option</th>
                  <th>Command</th>
                  <th>Probability</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(decision.probabilities).map(
                  ([name, probability]) => (
                    <tr
                      key={name}
                      className={name === decision.choice ? "chosen" : ""}
                    >
                      <td>{name.replaceAll("_", " ")}</td>
                      <td>{signed(decision.candidates[name], 3)}</td>
                      <td>{fixed(probability * 100, 0)}%</td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
            <pre>{JSON.stringify(decision.inputs, null, 2)}</pre>
          </details>
        </>
      )}
    </section>
  );
}
