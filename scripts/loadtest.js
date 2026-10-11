// k6 load test: submit jobs through the public API and wait for each to finish.
//
//   RELAY_URL=http://localhost:8000 RELAY_KEY=rl_... k6 run scripts/loadtest.js
//   k6 run -e RELAY_URL=... -e RELAY_KEY=... -e RATE=100 -e DURATION=2m scripts/loadtest.js
//
// Each iteration submits an echo job with a fresh Idempotency-Key, then polls until
// the job reaches a terminal state. Thresholds fail the run if submission latency or
// end-to-end completion regresses.

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE = __ENV.RELAY_URL || "http://localhost:8000";
const KEY = __ENV.RELAY_KEY;
const RATE = Number(__ENV.RATE || 50);
const DURATION = __ENV.DURATION || "60s";
const SLEEP = Number(__ENV.SLEEP_SECONDS || 0.05);

const submitLatency = new Trend("submit_latency_ms", true);
const completeLatency = new Trend("complete_latency_ms", true);
const completed = new Counter("jobs_completed");
const failed = new Counter("jobs_failed");

export const options = {
  scenarios: {
    steady: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: Math.max(10, RATE),
      maxVUs: RATE * 4,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    submit_latency_ms: ["p(95)<300"],
    complete_latency_ms: ["p(95)<10000"],
    checks: ["rate>0.99"],
  },
};

const headers = {
  Authorization: `Bearer ${KEY}`,
  "Content-Type": "application/json",
};

function idempotencyKey() {
  return `k6-${Date.now()}-${__VU}-${__ITER}-${Math.random().toString(36).slice(2)}`;
}

export default function () {
  const started = Date.now();
  const submit = http.post(
    `${BASE}/v1/jobs`,
    JSON.stringify({ kind: "echo", input: { sleep_seconds: SLEEP } }),
    { headers: { ...headers, "Idempotency-Key": idempotencyKey() } }
  );
  submitLatency.add(submit.timings.duration);
  check(submit, { "submitted (202)": (r) => r.status === 202 });
  if (submit.status !== 202) return;

  const id = submit.json("id");
  let status = "queued";
  for (let i = 0; i < 300 && status !== "succeeded" && status !== "failed"; i++) {
    sleep(0.2);
    const poll = http.get(`${BASE}/v1/jobs/${id}`, { headers });
    status = poll.json("status");
  }
  completeLatency.add(Date.now() - started);
  if (status === "succeeded") completed.add(1);
  else failed.add(1);
  check(status, { "completed successfully": (s) => s === "succeeded" });
}
