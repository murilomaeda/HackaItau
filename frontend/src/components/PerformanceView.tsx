import { Ban, CircleCheck, CornerUpLeft, PanelLeft, RefreshCw, ShieldCheck, TriangleAlert, Wrench } from 'lucide-react'
import { createElement, useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { DOMAIN_LABEL } from '../format'
import { agentIcon } from '../iconMap'
import { AGENT_META, toolLabel } from '../squad'
import type { AgentMetrics, Metrics } from '../types'
import { AsciiField } from './AsciiField'
import { BenchmarkPanel } from './BenchmarkPanel'

const FORBIDDEN_LABEL: Record<string, string> = {
  approve_credit: 'aprovar crédito',
  reject_credit: 'rejeitar crédito',
  choose_numeric_assumptions: 'escolher premissas numéricas',
  define_stress_scenarios: 'definir cenários de estresse',
  define_thresholds: 'definir limites da política',
  estimate_repayment_capacity: 'estimar capacidade de pagamento',
  invent_evidence: 'inventar evidências',
  mark_preferred_alternative: 'marcar uma alternativa como preferida',
  propose_structure: 'propor estrutura',
  rank_alternatives: 'ranquear alternativas',
  read_client_financials: 'ler dados financeiros brutos',
  recompute_metrics: 'recalcular métricas',
  rewrite_outputs: 'reescrever resultados de outros agentes',
}

const n = (v: number) => v.toLocaleString('pt-BR')
// "1 acerto", "3 acertos"
const pl = (v: number, one: string, many: string) => `${n(v)} ${v === 1 ? one : many}`
const compact = (v: number) =>
  v >= 1e6
    ? `${(v / 1e6).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} mi`
    : v >= 1e4
      ? `${(v / 1e3).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} mil`
      : n(v)

interface Props {
  sidebarOpen: boolean
  onOpenSidebar: () => void
}

// Desempenho dos agentes em todos os cases guardados. Tudo vem de /api/metrics (Event Log + resultados dos cases).
export function PerformanceView({ sidebarOpen, onOpenSidebar }: Props) {
  const [data, setData] = useState<Metrics | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [benchmarkRefresh, setBenchmarkRefresh] = useState(0)

  const fetchMetrics = useCallback(
    () =>
      api.metrics().then(
        (m) => {
          setData(m)
          setError(null)
          setLoading(false)
        },
        (e: Error) => {
          setError(e.message)
          setLoading(false)
        },
      ),
    [],
  )
  useEffect(() => {
    void fetchMetrics()
  }, [fetchMetrics])
  const reload = () => {
    setLoading(true)
    setBenchmarkRefresh((value) => value + 1)
    void fetchMetrics()
  }

  return (
    <main className="main">
      <header className="topbar">
        {!sidebarOpen && (
          <button type="button" className="icon-btn" onClick={onOpenSidebar} aria-label="Abrir barra lateral">
            <PanelLeft size={18} />
          </button>
        )}
        <p className="topbar-title">Desempenho dos agentes</p>
        <div className="topbar-actions">
          <button type="button" className="icon-btn with-label" onClick={reload} disabled={loading}>
            <RefreshCw size={16} className={loading ? 'spin' : undefined} />
            <span>Atualizar</span>
          </button>
        </div>
      </header>
      <AsciiField className="empty-field" />
      <div className="scroller">
        <div className="perf">
          {error && (
            <div className="callout danger">
              <TriangleAlert size={16} />
              <p>Não consegui carregar as métricas: {error}</p>
            </div>
          )}
          {data && data.cases === 0 && (
            <div className="perf-empty">
              <h1>Nenhuma análise ainda</h1>
              <p className="muted">Assim que você rodar a primeira análise numa conversa, os números aparecem aqui.</p>
            </div>
          )}
          {data && data.cases > 0 && (
            data.agents.every((a) => 'first_pass_pct' in a) ? <Dashboard m={data} /> : (
              <div className="callout warn">
                <TriangleAlert size={16} />
                <p>O servidor está com uma versão antiga. Reinicie o servidor e atualize esta tela.</p>
              </div>
            )
          )}
          <BenchmarkPanel refresh={benchmarkRefresh} />
        </div>
      </div>
    </main>
  )
}

function Dashboard({ m }: { m: Metrics }) {
  const ctx = m.context
  const calls = m.agents.reduce((s, a) => s + a.llm_calls, 0)
  return (
    <>
      <section className="perf-section">
        <h2>Uso</h2>
        <p className="muted small">Todas as análises guardadas. O comparativo com um agente único está no fim da página.</p>
      </section>

      <div className="kpis">
        <Kpi label="Análises" value={n(m.cases)} note={`${n(m.reports)} com relatório`} />
        <Kpi label="Chamadas ao modelo" value={n(calls)} note={pl(m.human_decisions, 'decisão humana', 'decisões humanas')} />
        <Kpi
          label="Tokens gastos"
          value={compact(m.tokens.input + m.tokens.output)}
          note={`${compact(m.tokens.input)} de entrada, ${compact(m.tokens.output)} de saída`}
        />
        <Kpi
          label="Entrada média por chamada"
          value={`${compact(ctx.per_call_squad)} tokens`}
          note="Estimado pelo tamanho do texto enviado"
        />
      </div>

      <section className="perf-section">
        <h2>Agentes</h2>
        <p className="muted small">
          A nota de cada agente é a parte das tarefas que ele entregou sem precisar refazer: o modelo acertou o formato
          na primeira resposta, a revisão não devolveu e o analista não pediu ajuste. Uma tarefa com mais de um desses
          problemas conta uma vez só. A nota diz se o trabalho passou pelos controles, não se a análise está certa.
        </p>
        <div className="agent-cards">
          {m.agents.map((a) => (
            <AgentCard key={a.agent_id} a={a} />
          ))}
        </div>
      </section>
    </>
  )
}

function Kpi({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="kpi">
      <p className="kpi-label">{label}</p>
      <p className="kpi-value">{value}</p>
      <p className="kpi-note">{note}</p>
    </div>
  )
}

function AgentCard({ a }: { a: AgentMetrics }) {
  const pct = a.first_pass_pct
  const level = pct === null ? 'none' : pct >= 80 ? 'good' : pct >= 50 ? 'warn' : 'bad'
  const meta = AGENT_META[a.agent_id]
  return (
    <article className="agent-card">
      <header>
        <span className="node">{createElement(agentIcon(a.agent_id), { size: 16 })}</span>
        <div>
          <h3>{meta?.name ?? a.name}</h3>
          <p className="muted small">
            {a.name}, versão {a.version}
          </p>
        </div>
      </header>
      <p className="agent-desc">{a.description}</p>

      {a.reviewer ? (
        <div className="accuracy">
          <div className="accuracy-head">
            <span>Problemas graves resolvidos no retrabalho</span>
            <strong>{a.reviewer.confirmation_pct === null ? 'sem dados' : `${Math.round(a.reviewer.confirmation_pct)}%`}</strong>
          </div>
          <Meter pct={a.reviewer.confirmation_pct} level="good" />
          <p className="small muted">
            Apontou {pl(a.reviewer.findings_raised, 'problema', 'problemas')}, {n(a.reviewer.material_findings)} graves, e
            devolveu {pl(a.reviewer.reworks_triggered, 'tarefa', 'tarefas')}. Dos{' '}
            {n(a.reviewer.checked_after_rework)} graves que tiveram uma revisão depois do retrabalho,{' '}
            {n(a.reviewer.confirmed_by_rework)} não apareceram mais.
          </p>
          <p className="small muted">Um problema que some pode ter sido corrigido ou só reescrito de outro jeito.</p>
        </div>
      ) : (
        <div className="accuracy">
          <div className="accuracy-head">
            <span>Entregues sem precisar refazer</span>
            <strong>{pct === null ? 'sem tarefas encerradas' : `${Math.round(pct)}%`}</strong>
          </div>
          <Meter pct={pct} level={level} />
          <ul className="outcomes">
            <li>
              <CircleCheck size={14} className="ok" />
              {n(a.hits)} de {pl(a.finished, 'tarefa', 'tarefas')} sem refazer
            </li>
            <li>
              <Wrench size={14} />
              {pl(a.validator_fixes, 'resposta refeita pelo modelo', 'respostas refeitas pelo modelo')}
            </li>
            <li>
              <CornerUpLeft size={14} className="warn" />
              {pl(a.reopened_by_review, 'devolvida pela revisão', 'devolvidas pela revisão')}
            </li>
            <li>
              <CornerUpLeft size={14} />
              {pl(a.adjusted_by_human, 'ajuste pedido pelo analista', 'ajustes pedidos pelo analista')}
            </li>
            <li>
              <TriangleAlert size={14} className="bad" />
              {pl(a.failed, 'falha', 'falhas')}
            </li>
          </ul>

        </div>
      )}

      <dl className="agent-stats">
        <div>
          <dt>Tarefas iniciadas</dt>
          <dd className="num">{n(a.runs)}{a.in_progress > 0 ? ` (${n(a.in_progress)} em andamento)` : ''}</dd>
        </div>
        <div>
          <dt>Tempo médio no modelo</dt>
          <dd className="num">{a.avg_latency_ms === null ? '—' : `${(a.avg_latency_ms / 1000).toFixed(1).replace('.', ',')} s`}</dd>
        </div>
        <div>
          <dt>Tokens enviados por chamada</dt>
          <dd className="num">{compact(a.avg_context_tokens)}</dd>
        </div>
        <div>
          <dt>Tokens gastos</dt>
          <dd className="num">{compact(a.tokens_in + a.tokens_out)}</dd>
        </div>
        <div>
          <dt>Consultas a dados</dt>
          <dd className="num">{n(a.tool_calls)}</dd>
        </div>
        <div>
          <dt>Chamadas ao modelo</dt>
          <dd className="num">{n(a.llm_calls)}</dd>
        </div>
      </dl>

      <details className="can-do">
        <summary>O que ele pode e não pode fazer</summary>
        <p className="small">
          <strong>Ferramentas:</strong> {a.tools.map(toolLabel).join('; ')}.
        </p>
        <p className="small">
          <strong>Dados que pode ler:</strong> {a.data_domains.map((d) => DOMAIN_LABEL[d] ?? d).join(', ')}.
        </p>
        <p className="small forbidden">
          <Ban size={13} />
          <span>
            <strong>Não pode:</strong>{' '}
            {a.forbidden_actions.map((f) => FORBIDDEN_LABEL[f] ?? f.replace(/_/g, ' ')).join(', ')}.
          </span>
        </p>
        {a.denied_calls > 0 && (
          <p className="small">
            <ShieldCheck size={13} /> {pl(a.denied_calls, 'acesso bloqueado', 'acessos bloqueados')} pelas permissões.
          </p>
        )}
      </details>
    </article>
  )
}

function Meter({ pct, level }: { pct: number | null; level: string }) {
  return (
    <div
      className={`meter ${level}`}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct ?? 0}
      aria-label="Taxa"
    >
      <span style={{ width: `${pct ?? 0}%` }} />
    </div>
  )
}
