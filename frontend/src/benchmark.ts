// Leitura do comparativo squad × generalista (GET /api/benchmarks/latest), usada na tela de entrada
// (versão completa) e em Desempenho dos agentes (resumo). Nada aqui chama o LLM.
import { useEffect, useState } from 'react'
import { api } from './api'
import type { BenchmarkSummary } from './types'

export type Group = BenchmarkSummary['groups'][number]

export const decimal = (v: number, digits = 1) => v.toLocaleString('pt-BR', { maximumFractionDigits: digits })
export const usd = (v: number | null, digits = 3) =>
  v === null ? 'indisponível' : `US$ ${v.toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
export const cost = (g: Group) => (g.usage_complete ? g.total_cost_usd : null)

const MODEL_NAME: Array<[RegExp, string]> = [
  [/^gpt-4\.1-mini(?:-|$)/, 'GPT-4.1 mini'],
  [/^gpt-4\.1(?:-|$)/, 'GPT-4.1'],
  [/^gpt-5\.2(?:-|$)/, 'GPT-5.2'],
  [/^gpt-5\.4(?:-|$)/, 'GPT-5.4'],
]
const EFFORT: Record<string, string> = { low: 'raciocínio baixo', medium: 'raciocínio médio', high: 'raciocínio alto' }

// ordem das linhas: do modelo menor ao maior; desconhecidos no fim
export const modelRank = (g: Group) => {
  const i = MODEL_NAME.findIndex(([re]) => re.test(g.model))
  return i < 0 ? MODEL_NAME.length : i
}
export const modelName = (g: Group) => MODEL_NAME.find(([re]) => re.test(g.model))?.[1] ?? g.model
export const effortName = (g: Group) => (g.reasoning_effort ? EFFORT[g.reasoning_effort] ?? g.reasoning_effort : null)
export const configName = (g: Group) => [modelName(g), effortName(g)].filter(Boolean).join(' · ')
// versão curta para rótulos de gráfico: "GPT-5.4 · alto"
export const shortConfig = (g: Group) =>
  [modelName(g), g.reasoning_effort ? effortName(g)!.replace('raciocínio ', '') : null].filter(Boolean).join(' · ')
export const archName = (g: Group) => (g.architecture === 'squad' ? 'Squad' : 'Generalista')

// o outro lado com o mesmo modelo e esforço: a comparação que isola a arquitetura
export const counterpart = (data: BenchmarkSummary, g: Group) =>
  data.groups.find(
    (o) => o.architecture !== g.architecture && o.model === g.model && (o.reasoning_effort ?? null) === (g.reasoning_effort ?? null),
  ) ?? null

// comparações só valem com o benchmark completo e a mesma amostra dos dois lados
// as comparações são por execução (taxa, custo e tempo médios), então lotes de tamanhos diferentes valem
export const comparable = (_data: BenchmarkSummary, a: Group, b: Group) => a.runs > 0 && b.runs > 0

export const passRate = (g: Group) => (g.runs ? g.automatic_passes / g.runs : 0)
export const pct = (v: number) => `${Math.round(100 * v)}%`
// custo médio por execução: compara configurações com quantidades de execuções diferentes
export const costPerRun = (g: Group) => (cost(g) === null || !g.runs ? null : cost(g)! / g.runs)
export const casesOf = (g: Group) => g.cases ?? g.runs
export const repetitionsOf = (g: Group) => g.repetitions ?? 1

export interface Headline {
  squad: Group
  generalist: Group
  matched: boolean // o generalista chegou à mesma taxa de acerto da squad
  costRatio: number | null // quantas vezes o generalista custou mais por execução
  timeRatio: number // quantas vezes o generalista demorou mais
}

// A squad mais barata entre as de maior acerto, contra o generalista mais barato que chega ao mesmo acerto.
// Se nenhum generalista chega lá, compara com o generalista de maior acerto (e o texto diz que ele ficou abaixo).
export function headline(data: BenchmarkSummary): Headline | null {
  const priced = data.groups.filter((g) => g.runs > 0 && cost(g) !== null)
  const byQuality = (a: Group, b: Group) => passRate(b) - passRate(a) || costPerRun(a)! - costPerRun(b)!
  const squad = priced.filter((g) => g.architecture === 'squad').sort(byQuality)[0]
  if (!squad) return null
  const generalists = priced.filter((g) => g.architecture === 'generalist')
  const matching = generalists.filter((g) => passRate(g) >= passRate(squad)).sort((a, b) => costPerRun(a)! - costPerRun(b)!)
  const generalist = matching[0] ?? generalists.sort(byQuality)[0]
  if (!generalist || !comparable(data, squad, generalist)) return null
  const sc = costPerRun(squad)!
  return {
    squad,
    generalist,
    matched: matching.length > 0,
    costRatio: sc > 0 ? costPerRun(generalist)! / sc : null,
    timeRatio: squad.mean_latency_seconds > 0 ? generalist.mean_latency_seconds / squad.mean_latency_seconds : 0,
  }
}

export function useBenchmark(refresh = 0) {
  const [data, setData] = useState<BenchmarkSummary | null>(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let active = true
    api.benchmark().then(
      (r) => {
        if (!active) return
        setData(r.benchmark)
        setError(false)
        setLoading(false)
      },
      () => {
        if (!active) return
        setError(true)
        setLoading(false)
      },
    )
    return () => {
      active = false
    }
  }, [refresh])
  return { data, error, loading }
}
