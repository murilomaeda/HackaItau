// "Comparativo" da tela de entrada: squad × agente generalista, com todas as configurações executadas.
// Os números vêm do benchmark publicado ou local (GET /api/benchmarks/latest); nada é fixo no código.
// Sem benchmark (ou sem servidor) a seção não aparece.
import {
  archName,
  byPassRate,
  casesOf,
  shortConfig,
  configName,
  cost,
  costPerRun,
  decimal,
  headline,
  modelRank,
  passRate,
  pct,
  repetitionsOf,
  sameModel,
  useBenchmark,
  usd,
  type Group,
} from '../benchmark'
import type { BenchmarkSummary } from '../types'

export function Comparison() {
  const { data } = useBenchmark()
  if (!data || !data.groups.length) return null
  return <Body data={data} />
}

// uma linha por modelo (e esforço de raciocínio), com a squad e o generalista lado a lado
function rows(data: BenchmarkSummary) {
  const keyOf = (g: Group) => `${g.model}|${g.reasoning_effort ?? ''}`
  const map = new Map<string, { squad?: Group; generalist?: Group; rank: number }>()
  for (const g of data.groups) {
    const row = map.get(keyOf(g)) ?? { rank: modelRank(g) }
    row[g.architecture] = g
    map.set(keyOf(g), row)
  }
  return [...map.values()].sort((a, b) => a.rank - b.rank)
}

function Body({ data }: { data: BenchmarkSummary }) {
  const h = headline(data)
  const same = sameModel(data)
  const first = data.groups[0]
  const cases = casesOf(first)
  const reps = repetitionsOf(first)
  const sample = reps > 1 ? `${cases} casos, ${reps} vezes cada` : `${cases} casos`
  const ranked = [...data.groups].sort(byPassRate)
  const priced = ranked.filter((g) => cost(g) !== null)
  const noCost = data.groups.filter((g) => cost(g) === null)
  const maxCost = Math.max(...priced.map((g) => costPerRun(g)!), 1e-9)
  const maxTime = Math.max(...ranked.map((g) => g.mean_latency_seconds), 1e-9)
  const sameSquad = same ? same.squadPasses / same.squadRuns : 0
  const sameGen = same ? same.generalistPasses / same.generalistRuns : 0
  const squadMisses = same ? same.squadRuns - same.squadPasses : 0
  const genMisses = same ? same.generalistRuns - same.generalistPasses : 0
  const priciestRatio = h?.priciest && costPerRun(h.squad) ? costPerRun(h.priciest)! / costPerRun(h.squad)! : null

  return (
    <section id="comparativo" className="cmp" aria-labelledby="cmp-title">
      <header className="cmp-intro">
        <p className="how-kicker">comparativo</p>
        <h2 id="cmp-title">
          {h && passRate(h.squad) > passRate(h.generalist)
            ? 'Acerta mais que um modelo maior, por uma fração do custo.'
            : 'Squad e agente generalista, lado a lado.'}
        </h2>
        <p className="cmp-lede">
          Rodamos {sample}, em situações fictícias de crédito agro, de duas formas: com a squad de especialistas e com um
          único agente generalista que faz elegibilidade, risco e estruturação numa só resposta. Os dois recebem os mesmos
          dados, cálculos, verificações e revisão; muda só a arquitetura e o modelo.
        </p>
      </header>

      {(h || same) && (
        <div className="cmp-figures">
          {same && (
            <Figure
              big={pct(sameSquad)}
              bigSub={`× ${pct(sameGen)}`}
              label="de acerto com o mesmo modelo"
              sub={`Com ${same.models.join(' e ')}, a squad errou ${squadMisses} de ${same.squadRuns} análises; o generalista, ${genMisses} de ${same.generalistRuns}. Mesmos dados e verificações: muda só a arquitetura.`}
            />
          )}
          {h && h.costRatio !== null && (
            <Figure
              big={`${decimal(h.costRatio, 0)}×`}
              label={`mais barato que um modelo de ${h.generalist.reasoning_effort ? 'raciocínio' : 'maior porte'}`}
              sub={`A squad com ${configName(h.squad)} acertou ${pct(passRate(h.squad))} a ${usd(costPerRun(h.squad), 4)} por análise. O generalista com ${configName(h.generalist)}, ${pct(passRate(h.generalist))} a ${usd(costPerRun(h.generalist), 4)}${priciestRatio ? `; com ${configName(h.priciest!)}, a diferença chega a ${decimal(priciestRatio, 0)}×` : ''}.`}
            />
          )}
          {h && (
            <Figure
              big={`${decimal(h.timeRatio)}×`}
              label="mais rápido por análise"
              sub={`${decimal(h.squad.mean_latency_seconds, 0)} s em média na squad com ${configName(h.squad)}, contra ${decimal(h.generalist.mean_latency_seconds, 0)} s no generalista com ${configName(h.generalist)}.`}
            />
          )}
        </div>
      )}

      <div className="cmp-block">
        <h3>Análises aprovadas, por modelo</h3>
        <p className="cmp-note">Parte das análises que passou em todas as verificações automáticas.</p>
        <div className="cmp-matrix" role="table" aria-label="Análises aprovadas por modelo e arquitetura">
          <div className="cmp-mrow head" role="row">
            <span role="columnheader">Modelo</span>
            <span role="columnheader">Squad</span>
            <span role="columnheader">Generalista</span>
          </div>
          {rows(data).map((r) => {
            const any = (r.squad ?? r.generalist)!
            return (
              <div className="cmp-mrow" role="row" key={`${any.model}|${any.reasoning_effort ?? ''}`}>
                <span role="rowheader" className="cmp-model">
                  {configName(any)}
                </span>
                <Cell g={r.squad} />
                <Cell g={r.generalist} />
              </div>
            )
          })}
        </div>
      </div>

      <div className="cmp-block">
        <div className="cmp-charts">
          <Bars
            title="Acerto"
            hint="maior é melhor"
            groups={ranked}
            width={(g) => 100 * passRate(g)}
            value={(g) => pct(passRate(g))}
          />
          <Bars
            title="Custo por análise"
            hint="menor é melhor"
            groups={priced}
            width={(g) => (100 * costPerRun(g)!) / maxCost}
            value={(g) => usd(costPerRun(g), 3)}
          />
          <Bars
            title="Tempo por análise"
            hint="menor é melhor"
            groups={ranked}
            width={(g) => (100 * g.mean_latency_seconds) / maxTime}
            value={(g) => `${decimal(g.mean_latency_seconds, 0)} s`}
          />
        </div>
        <p className="cmp-legend" aria-hidden="true">
          <span>
            <i className="is-squad" /> squad
          </span>
          <span>
            <i className="is-generalist" /> generalista
          </span>
          <span>· em ordem de acerto · ✓ acertou todas as análises</span>
        </p>
        {noCost.length > 0 && (
          <p className="cmp-note">
            Sem custo total: {noCost.map((g) => `${archName(g).toLowerCase()} com ${configName(g)}`).join(', ')} (o provedor
            não informou o consumo de todas as chamadas; o custo fica indisponível, não zero).
          </p>
        )}
      </div>

      <div className="cmp-block">
        <h3>Como ler</h3>
        <ul className="cmp-caveats">
          <li>
            As verificações automáticas medem regras: bloqueio esperado, premissas numéricas, limites de produtos,
            referências e riscos tratados. Não medem acurácia factual; a revisão humana às cegas
            {data.quality_status === 'reviewed' ? ' já foi feita.' : ' ainda está pendente.'}
          </li>
          <li>
            Com o mesmo modelo pequeno, o generalista custa menos por análise, porque faz uma chamada só; a squad faz
            quatro, menores. A diferença aparece no acerto, e em comparação com modelos maiores.
          </li>
          <li>
            {sample} por configuração. Diferenças de poucos pontos entre configurações não são conclusivas.
            {data.status === 'complete' ? '' : ' Os lotes com modelos de raciocínio foram interrompidos antes do fim; os números deles são de menos execuções.'}
          </li>
          <li>
            O custo soma todas as chamadas, inclusive correções e retrabalho, com preços de API de referência. Não é fatura
            e não inclui infraestrutura.
          </li>
        </ul>
        <p className="cmp-meta">
          {data.published_snapshot ? 'benchmark publicado' : 'execução local'} · {new Date(data.created_at).toLocaleDateString('pt-BR')}
          {data.status === 'complete' ? '' : ' · parcial'} · {data.run_id}
        </p>
      </div>
    </section>
  )
}

function Figure({ big, bigSub, label, sub }: { big: string; bigSub?: string; label: string; sub: string }) {
  return (
    <div className="cmp-figure">
      <p className="cmp-big">
        {big}
        {bigSub && <span>{bigSub}</span>}
      </p>
      <p className="cmp-label">{label}</p>
      <p className="cmp-sub">{sub}</p>
    </div>
  )
}

function Cell({ g }: { g?: Group }) {
  if (!g) {
    return (
      <span role="cell" className="cmp-cell is-none">
        não testado
      </span>
    )
  }
  const c = cost(g)
  return (
    <span role="cell" className={`cmp-cell is-${g.architecture}`}>
      <span className="cmp-score">{pct(passRate(g))}</span>
      <span className="cmp-meter" aria-hidden="true">
        <span style={{ width: `${100 * passRate(g)}%` }} />
      </span>
      <span className="cmp-cell-sub">
        {g.automatic_passes} de {g.runs} · {c === null ? 'custo indisponível' : `${usd(costPerRun(g), 3)} por análise`} ·{' '}
        {decimal(g.mean_latency_seconds, 0)} s
        {g.errors > 0 && ` · ${g.errors} ${g.errors === 1 ? 'falha operacional' : 'falhas operacionais'}`}
      </span>
    </span>
  )
}

function Bars({
  title,
  hint,
  groups,
  width,
  value,
}: {
  title: string
  hint: string
  groups: Group[]
  width: (g: Group) => number
  value: (g: Group) => string
}) {
  return (
    <figure className="cmp-chart">
      <figcaption>
        {title} <span>{hint}</span>
      </figcaption>
      {groups.map((g) => {
        const name = `${archName(g)} · ${shortConfig(g)}`
        return (
          <div className="cmp-bar-row" key={`${g.architecture}|${g.model}|${g.reasoning_effort ?? ''}`} title={`${name}: ${value(g)} · ${g.automatic_passes} de ${g.runs} análises aprovadas`}>
            <span className="cmp-bar-label">
              {name}
              {g.automatic_passes === g.runs && <b aria-label="acertou todas as análises"> ✓</b>}
            </span>
            <span className="cmp-bar-track">
              <span className={`cmp-bar is-${g.architecture}`} style={{ width: `${Math.max(width(g), 0.6)}%` }} />
            </span>
            <span className="cmp-bar-value">{value(g)}</span>
          </div>
        )
      })}
    </figure>
  )
}
