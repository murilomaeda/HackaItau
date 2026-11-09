import type {
  AssistReply,
  BenchmarkSummary,
  CaseEvent,
  CaseState,
  CreateCaseRequest,
  EvidenceItem,
  HealthResponse,
  Identity,
  HumanReviewRequest,
  LoginReply,
  InputRequest,
  Metrics,
  Report,
} from './types'

export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let code = 'http_error'
    let message = `${res.status} ${res.statusText}`
    try {
      const body = (await res.json()) as { detail?: { code?: string; message?: string } | string }
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail) {
        code = body.detail.code ?? code
        message = body.detail.message ?? message
      }
    } catch {
      /* corpo não-JSON */
    }
    throw new ApiError(res.status, code, message)
  }
  return (await res.json()) as T
}

// Token da sessão (session.ts define). Toda chamada autenticada leva o Bearer; um 401 encerra a sessão local.
let token: string | null = null
let onUnauthorized: (() => void) | null = null

export function setAuth(t: string | null, unauthorized: (() => void) | null = null) {
  token = t
  onUnauthorized = unauthorized
}

async function request(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(url, { ...init, headers })
  if (res.status === 401 && token) onUnauthorized?.()
  return res
}

const get = (url: string) => request(url)
const post = (url: string, body: unknown) =>
  request(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

export const api = {
  health: () => fetch('/api/health').then(json<HealthResponse>),
  identities: () => fetch('/api/identities').then(json<Identity[]>),
  login: (user_id: string, password: string) =>
    fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id, password }),
    }).then(json<LoginReply>),
  logout: () => post('/api/auth/logout', {}),
  createCase: (body: CreateCaseRequest) => post('/api/cases', body).then(json<CaseState>),
  getCase: (id: string) => get(`/api/cases/${id}`).then(json<CaseState>),
  events: (id: string, after = 0) => get(`/api/cases/${id}/events?after=${after}`).then(json<CaseEvent[]>),
  provideInput: (id: string, body: InputRequest) => post(`/api/cases/${id}/input`, body).then(json<CaseState>),
  run: (id: string) => post(`/api/cases/${id}/run`, {}).then(json<CaseState>),
  retry: (id: string) => post(`/api/cases/${id}/retry`, {}).then(json<CaseState>),
  reply: (id: string, text: string) => post(`/api/cases/${id}/reply`, { text }).then(json<CaseState>),
  attach: (id: string, body: { user_id: string; filename: string; content_type: string; data_base64: string }) =>
    post(`/api/cases/${id}/attachments`, body).then(json<CaseState>),
  assist: (body: { user_id: string; text: string; case_id?: string | null }) =>
    post('/api/assist', body).then(json<AssistReply>),
  metrics: () => get('/api/metrics').then(json<Metrics>),
  benchmark: () => get('/api/benchmarks/latest').then(json<{ benchmark: BenchmarkSummary | null }>),
  report: (id: string) => get(`/api/cases/${id}/report`).then(json<Report>),
  evidence: (id: string, evidenceId: string) =>
    get(`/api/cases/${id}/evidence/${encodeURIComponent(evidenceId)}`).then(json<EvidenceItem>),
  humanReview: (id: string, body: HumanReviewRequest) =>
    post(`/api/cases/${id}/human-review`, body).then(json<CaseState>),
}
