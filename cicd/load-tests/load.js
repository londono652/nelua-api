// Prueba de carga: sube por etapas hasta TARGET_RPS y sostiene la carga.
//
//   Local (contra el contenedor):
//     k6 run -e TARGET_RPS=200 -e RAMP=10s -e HOLD=20s -e API_KEY=<llave> cicd/load-tests/load.js
//
//   Real (10.000 RPS): se ejecuta dentro del clúster, repartida en varios pods,
//   con cicd/load-tests/run-in-cluster.sh.
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const TARGET_RPS = parseInt(__ENV.TARGET_RPS || '10000', 10);
const RAMP = __ENV.RAMP || '2m';
const HOLD = __ENV.HOLD || '5m';
const HEADERS = { 'X-API-Key': __ENV.API_KEY || '' };

// Mezcla de tráfico parecida al uso real: el panorama es lo más consultado.
const MIX = [
  { name: 'summary', path: '/v1/summary', weight: 0.5 },
  { name: 'deployments', path: '/v1/deployments', weight: 0.25 },
  { name: 'alerts', path: '/v1/alerts', weight: 0.2 },
  { name: 'budget', path: '/v1/budget', weight: 0.05 },
];

function pick() {
  let roll = Math.random();
  for (const item of MIX) {
    if (roll < item.weight) return item;
    roll -= item.weight;
  }
  return MIX[0];
}

export const options = {
  // No se guarda el cuerpo de las respuestas: el generador rinde más.
  discardResponseBodies: true,
  scenarios: {
    load: {
      // "arrival rate": k6 mantiene las peticiones por segundo pedidas aunque
      // la API responda más lento (modelo abierto, como el tráfico real).
      executor: 'ramping-arrival-rate',
      startRate: Math.ceil(TARGET_RPS / 10),
      timeUnit: '1s',
      preAllocatedVUs: Math.ceil(TARGET_RPS / 10),
      maxVUs: Math.ceil(TARGET_RPS / 2),
      stages: [
        { target: Math.ceil(TARGET_RPS * 0.25), duration: RAMP },
        { target: Math.ceil(TARGET_RPS * 0.5), duration: RAMP },
        { target: TARGET_RPS, duration: RAMP },
        { target: TARGET_RPS, duration: HOLD },
        { target: 0, duration: '30s' },
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<300', 'p(99)<800'],
    checks: ['rate>0.99'],
  },
};

export default function () {
  const target = pick();
  // La etiqueta "name" agrupa las métricas por endpoint.
  const response = http.get(`${BASE_URL}${target.path}`, {
    headers: HEADERS,
    tags: { name: target.name },
  });
  check(response, { 'responde 200': (r) => r.status === 200 });
}
