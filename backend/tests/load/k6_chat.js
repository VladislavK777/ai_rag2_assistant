// k6 нагрузочный сценарий чата (SSE).
// Запуск: k6 run backend/tests/load/k6_chat.js
// Профили задаются переменной PROFILE: warmup | light | target | stress

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.TOKEN; // JWT тестового пользователя
const WORKSPACE = __ENV.WORKSPACE_ID || "00000000-0000-0000-0000-000000000010";

const profiles = {
  warmup: { executor: "constant-vus", vus: 1, duration: "1m" },
  light: { executor: "constant-vus", vus: 5, duration: "5m" },
  target: { executor: "constant-vus", vus: 10, duration: "10m" },
  stress: { executor: "ramping-vus",
    stages: [
      { duration: "2m", target: 20 },
      { duration: "5m", target: 20 },
      { duration: "2m", target: 40 },
      { duration: "5m", target: 40 },
    ],
  },
};

export const options = {
  scenarios: {
    chat: profiles[__ENV.PROFILE || "target"],
  },
  thresholds: {
    "http_req_duration{endpoint:chat}": ["p(95)<15000"],
    checks: ["rate>0.99"],
  },
};

const QUESTIONS = [
  "Каков регламент оформления отпуска?",
  "Какие условия договора с ООО Ромашка?",
  "Найди порядок согласования командировок",
  "Какая политика информационной безопасности?",
];

export default function () {
  const payload = JSON.stringify({
    message: QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)],
    session_id: null,
    workspace_id: WORKSPACE,
  });
  const params = {
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${TOKEN}`,
    },
    tags: { endpoint: "chat" },
    timeout: "120s",
  };

  const res = http.post(`${BASE_URL}/api/v1/chat/stream`, payload, params);
  check(res, {
    "status 200": (r) => r.status === 200,
    "SSE-поток получен": (r) => r.body && r.body.includes("data:"),
  });
  sleep(3);
}
