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

export interface SameModel {
  squadPasses: number
  squadRuns: number
  generalistPasses: number
  generalistRuns: number
  models: string[] // nomes das configurações rodadas nas duas arquiteturas
}

// Mesmo modelo nas duas arquiteturas: soma as configurações em que as duas rodaram. Isola o efeito da arquitetura.
export function sameModel(data: BenchmarkSummary): SameModel | null {
  const out: SameModel = { squadPasses: 0, squadRuns: 0, generalistPasses: 0, generalistRuns: 0, models: [] }
  for (const g of data.groups.filter((x) => x.architecture === 'squad')) {
    const other = counterpart(data, g)
    if (!other || !g.runs || !other.runs) continue
    out.squadPasses += g.automatic_passes
    out.squadRuns += g.runs
    out.generalistPasses += other.automatic_passes
    out.generalistRuns += other.runs
    out.models.push(configName(g))
  }
  return out.models.length ? out : null
}

export interface Headline {
  squad: Group
  generalist: Group // o generalista com modelo de raciocínio que mais acertou
  priciest: Group | null // o generalista mais caro por análise, para dar a faixa
  costRatio: number | null // quantas vezes o generalista custou mais por análise
  timeRatio: number // quantas vezes o generalista demorou mais
}

// A alternativa à squad é "usar um modelo maior". Compara a squad mais barata que acerta pelo menos tanto quanto o
// melhor generalista com modelo de raciocínio (ou, sem modelos de raciocínio, quanto o melhor generalista).
export function headline(data: BenchmarkSummary): Headline | null {
  const priced = data.groups.filter((g) => g.runs > 0 && cost(g) !== null)
  const generalists = priced.filter((g) => g.architecture === 'generalist')
  const reasoning = generalists.filter((g) => g.reasoning_effort)
  const pool = reasoning.length ? reasoning : generalists
  const byQuality = (a: Group, b: Group) => passRate(b) - passRate(a) || costPerRun(a)! - costPerRun(b)!
  const generalist = [...pool].sort(byQuality)[0]
  if (!generalist) return null
  const squad = priced
    .filter((g) => g.architecture === 'squad' && passRate(g) >= passRate(generalist))
    .sort((a, b) => costPerRun(a)! - costPerRun(b)!)[0]
  if (!squad) return null
  const sc = costPerRun(squad)!
  const priciest = [...pool].sort((a, b) => costPerRun(b)! - costPerRun(a)!)[0] ?? null
  return {
    squad,
    generalist,
    priciest: priciest && priciest !== generalist ? priciest : null,
    costRatio: sc > 0 ? costPerRun(generalist)! / sc : null,
    timeRatio: squad.mean_latency_seconds > 0 ? generalist.mean_latency_seconds / squad.mean_latency_seconds : 0,
  }
}

// ordem dos gráficos: quem mais acerta primeiro; no empate, o mais barato
export const byPassRate = (a: Group, b: Group) =>
  passRate(b) - passRate(a) || (costPerRun(a) ?? Infinity) - (costPerRun(b) ?? Infinity)

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
