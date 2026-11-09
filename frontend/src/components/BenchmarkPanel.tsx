// Resumo do comparativo em Desempenho dos agentes: só as comparações diretas.
// O comparativo completo (todas as configurações, custos e ressalvas) fica na tela de entrada.
import { casesOf, comparable, configName, costPerRun, counterpart, decimal, headline, passRate, pct, repetitionsOf, useBenchmark, usd } from '../benchmark'
import type { BenchmarkSummary } from '../types'

export function BenchmarkPanel({ refresh }: { refresh: number }) {
  const { data, error, loading } = useBenchmark(refresh)
  return (
    <section className="perf-section bench-mini" aria-label="Comparativo com agente generalista">
      <h2>Comparativo com agente generalista</h2>
      {loading ? (
        <p className="muted small">Carregando comparativo…</p>
      ) : error ? (
        <p className="muted small">Não foi possível carregar o comparativo.</p>
      ) : !data ? (
        <p className="muted small">Os resultados aparecerão aqui após a execução do benchmark.</p>
      ) : (
        <Summary data={data} />
      )}
    </section>
  )
}

function Summary({ data }: { data: BenchmarkSummary }) {
  const h = headline(data)
  const same = h && counterpart(data, h.squad)
  const g = data.groups[0]
  const sample = g ? `${casesOf(g)} casos${repetitionsOf(g) > 1 ? `, ${repetitionsOf(g)} vezes cada` : ''}` : ''
  return (
    <>
      <p className="muted small">
        {data.published_snapshot ? 'Benchmark publicado' : 'Execuções reais'} · {new Date(data.created_at).toLocaleDateString('pt-BR')}
        {sample ? ` · ${sample}, por configuração` : ''}
        {data.status === 'complete' ? '' : ' · resultado parcial'} · verificações automáticas, revisão humana pendente.
      </p>
      {!h ? (
        <p className="muted small">Ainda não há resultados com custo conhecido dos dois lados.</p>
      ) : (
        <div className="bench-mini-row">
          <Stat
            label={h.matched ? 'Custo por análise, com o mesmo acerto' : 'Custo por análise'}
            squad={`${usd(costPerRun(h.squad), 4)} · ${pct(passRate(h.squad))}`}
            generalist={`${usd(costPerRun(h.generalist), 4)} · ${pct(passRate(h.generalist))}`}
            note={`${configName(h.squad)} × ${configName(h.generalist)}${h.costRatio ? ` · ${decimal(h.costRatio)}× menor` : ''}`}
          />
          <Stat
            label="Tempo médio por análise"
            squad={`${decimal(h.squad.mean_latency_seconds, 0)} s`}
            generalist={`${decimal(h.generalist.mean_latency_seconds, 0)} s`}
            note={`mesmas configurações · ${decimal(h.timeRatio)}× mais rápido`}
          />
          {same && comparable(data, h.squad, same) && (
            <Stat
              label={`Mesmo modelo (${configName(h.squad)})`}
              squad={pct(passRate(h.squad))}
              generalist={pct(passRate(same))}
              note="análises aprovadas nas verificações automáticas"
            />
          )}
        </div>
      )}
      <p className="muted small">Todas as configurações e ressalvas estão na tela de entrada, na seção Comparativo.</p>
    </>
  )
}

function Stat({ label, squad, generalist, note }: { label: string; squad: string; generalist: string; note: string }) {
  return (
    <div className="bench-mini-stat">
      <p className="bench-mini-label">{label}</p>
      <dl>
        <div>
          <dt>Squad</dt>
          <dd>{squad}</dd>
        </div>
        <div>
          <dt>Generalista</dt>
          <dd>{generalist}</dd>
        </div>
      </dl>
      <p className="bench-mini-note">{note}</p>
    </div>
  )
}
