// Prueba de humo: carga ligera después de cada despliegue.
// 10 iteraciones por segundo durante 30 s, con 2 peticiones cada una (600 en
// total). Staging y prod corren seguidos desde la misma IP, y juntos quedan por
// debajo del límite por IP del WAF (2.000 en 5 minutos).
//
//   k6 run -e BASE_URL=http://localhost:8000 -e API_KEY=<llave> cicd/load-tests/smoke.js
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const REPO = __ENV.REPO || 'londono652/nelua-api';
// Encabezado de autenticación: AUTH_HEADER ("X-API-Key: ..." o
// "Authorization: Bearer ...") o, para pruebas locales, API_KEY.
function authHeaders() {
  const raw = __ENV.AUTH_HEADER || `X-API-Key: ${__ENV.API_KEY || ''}`;
  const index = raw.indexOf(':');
  return { [raw.slice(0, index).trim()]: raw.slice(index + 1).trim() };
}
const params = { headers: authHeaders() };

export const options = {
  scenarios: {
    smoke: {
      executor: 'constant-arrival-rate',
      rate: 10,
      timeUnit: '1s',
      duration: '30s',
      preAllocatedVUs: 10,
      maxVUs: 20,
    },
  },
  // Si no se cumplen, k6 termina con error y el pipeline ejecuta el rollback.
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<300'],
    checks: ['rate>0.99'],
  },
};

export default function () {
  const responses = http.batch([
    ['GET', `${BASE_URL}/v1/repos/${REPO}/deploys`, null, params],
    ['GET', `${BASE_URL}/v1/deployments`, null, params],
  ]);

  check(responses[0], { 'deploys responde 200': (r) => r.status === 200 });
  check(responses[1], { 'deployments responde 200': (r) => r.status === 200 });
}
