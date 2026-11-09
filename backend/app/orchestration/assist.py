"""Assistente de entrada do Orquestrador: lê cada mensagem do analista e decide o que ela é.

- demanda de crédito agro → abre um case (squad);
- dúvida sobre política, documentação ou produtos → responde pela base interna, citando as fontes (KB-*);
- pergunta sobre o case aberto → responde a partir do relatório já consolidado (nenhum dado novo é consultado);
- pergunta sobre a squad, o preço de referência ou as garantias do catálogo → responde direto (`info`);
- pergunta sobre dado que a demonstração não tem → diz isso (`no_data`), sem citar política que não responde;
- resposta/ajuste para o case aberto → o frontend encaminha para /reply ou /human-review;
- saudação ou "o que você faz" → explica as capacidades; fora de crédito agro → diz o escopo.

Pergunta nunca vira observação do case: só afirmações e dados reconhecidos vão para a squad como contexto.

Determinístico (sem LLM): a classificação não autoriza nada e a resposta só usa o que o perfil do usuário pode ler
(base de conhecimento, referência de mercado, catálogo; só campos públicos) ou o relatório que ele já recebeu.
Nenhuma resposta aqui lê dados de cliente: isso só acontece dentro de um case, pelo Gateway.
"""

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

from app.core.crops import COMMON_CROPS, product_supports_crop
from app.core.schemas.case import CaseState, CaseStatus
from app.core.schemas.context import UserIdentity
from app.core.schemas.outputs import InterpretedDemand
from app.core.schemas.report import Report
from app.data.repository import DataRepository, KnowledgeRetriever
from app.orchestration.clarify import context_from
from app.orchestration.interpreter import heuristic_interpret

AssistKind = Literal[
    "credit_demand",
    "case_reply",
    "adjustment",
    "case_question",
    "policy_answer",
    "capabilities",
    "out_of_scope",
    "clarify",
    "info",
    "no_data",
    "run_squad",
]


class Citation(BaseModel):
    id: str
    title: str
    excerpt: str


class AssistRequest(BaseModel):
    user_id: str
    text: str
    case_id: str | None = None


class AssistReply(BaseModel):
    kind: AssistKind
    message: str = ""
    bullets: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


_GREETING = re.compile(r"^\s*(oi|ol[aá]|bom dia|boa tarde|boa noite|e a[ií]|hello|hi)\b", re.IGNORECASE)
_CAPABILITIES = re.compile(
    r"o que (voc[eê] )?(consegue|pode|sabe|faz)|como (voc[eê] )?funciona|quem [eé] voc[eê]|capacidade|"
    r"em que (voc[eê] )?pode|como (voc[eê] )?(me )?ajuda",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(
    r"^\s*(qual|quais|como|quando|quanto|quantos|quantas|o que|por que|porque|existe|h[aá] |onde|precisa|preciso de qu)",
    re.IGNORECASE,
)
_REQUEST_START = re.compile(r"^\s*(pode|poderia|daria|d[aá] (pra|para)|consegue|voc[eê] pode)", re.IGNORECASE)
_IMPERATIVE = re.compile(r"^\s*(analis|avali|estrutur|mont|fa[cç]a|rode|execut|simul|compar)", re.IGNORECASE)
_AGRO_CREDIT = re.compile(
    r"cr[eé]dito|custeio|investimento|comercializa|safra|soja|milho|algod|caf[eé]|trigo|cana|arroz|laranj|citros|mandioca|macaxeira|aipim|cacau|banan|sorgo|feij|produtor|fazenda|"
    r"rural|agro|financiament|garantia|penhor|aval|\bcpr\b|pol[ií]tica|document|elegib|alavancag|cobertura|estresse|"
    r"limite|ticket|taxa|prazo|amortiza|cat[aá]logo|produto|arrendamento|matr[ií]cula|plantio|demonstra|cliente|risco",
    re.IGNORECASE,
)
_DEMAND = re.compile(
    r"solicit|pede|pedido|quer |deseja|analis|avali|estrutur|financi|opera[cç]|precisa de r\$", re.IGNORECASE
)

# fração do peso da pergunta que o trecho precisa cobrir para ser citado como resposta
MIN_POLICY_SCORE = 0.5

CAPABILITIES = [
    "Analisar uma operação de crédito agro (custeio, investimento ou comercialização): monto uma squad que checa "
    "elegibilidade e documentação, calcula capacidade de pagamento e cenários de estresse, propõe estruturas e revisa "
    "o resultado.",
    "Responder dúvidas sobre políticas, documentos exigidos e produtos, citando a base interna.",
    "Conferir documentos que você anexar (plano de plantio, matrícula ou arrendamento, demonstrações financeiras) e "
    "apontar o que falta.",
    "Refazer partes da análise com os seus ajustes (garantias, prazo, leitura de riscos) e comparar as versões.",
    "Explicar o relatório: riscos, cenários de estresse, alternativas, pendências e o que a revisão encontrou.",
]

EXAMPLES = [
    "O cliente Fazenda Horizonte S.A. solicita R$ 50 milhões para custeio da safra de soja 2025/26.",
    "Quais documentos são obrigatórios para custeio?",
    "Qual é o limite de alavancagem da política de crédito?",
]


def assist(
    text: str, user: UserIdentity, state: CaseState | None, knowledge: KnowledgeRetriever, repo: DataRepository | None = None
) -> AssistReply:
    t = text.strip()
    h = heuristic_interpret(t)
    question = (t.endswith("?") or bool(_QUESTION_START.search(t))) and not _IMPERATIVE.search(t)
    # na revisão humana, "pode incluir aval?" é um pedido de ajuste, não uma dúvida
    if state is not None and state.status == CaseStatus.human_review_required and _REQUEST_START.search(t):
        question = False

    plain = _plain(t)
    if _ACK.search(plain):
        return AssistReply(kind="info", message=_ack_message(state))
    if _RUN_COMMAND.search(plain):
        return _run_command(state)
    # pergunta sobre o relatório já pronto tem prioridade: ele fala deste case, não do catálogo em geral
    if question and state is not None and state.report is not None and h.client_ref is None:
        if (reply := _case_answer(t, state.report)) is not None:
            return reply
    if (reply := _direct_answer(t, plain, question, h, user, state, repo)) is not None:
        return reply

    if (_GREETING.search(t) and len(t) < 40) or _CAPABILITIES.search(t):
        return AssistReply(
            kind="capabilities",
            message="Sou o Orquestrador das squads de crédito agro. Posso:",
            bullets=CAPABILITIES,
            suggestions=EXAMPLES,
        )

    agro = bool(_AGRO_CREDIT.search(t))
    has_case_data = h.client_ref is not None or h.requested_amount is not None

    if state is not None:
        return _with_case(t, question, has_case_data, h, user, state, knowledge)

    if question and agro and not has_case_data:
        return _policy_answer(t, user, knowledge)
    signals = sum([has_case_data, h.purpose is not None and h.crop is not None, bool(_DEMAND.search(t))])
    if agro and signals >= 1:
        return AssistReply(kind="credit_demand")
    if agro:
        return _clarify()
    return AssistReply(
        kind="out_of_scope",
        message="Isso fica fora do que eu faço. Meu escopo é crédito agro: análise de operações, políticas, documentos e "
        "produtos. Posso ajudar com algo assim?",
        suggestions=EXAMPLES,
    )


def _with_case(
    t: str,
    question: bool,
    has_case_data: bool,
    h: InterpretedDemand,
    user: UserIdentity,
    state: CaseState,
    knowledge: KnowledgeRetriever,
) -> AssistReply:
    status = state.status
    # "O prazo é de 18 meses?" passa um número para a análise, mesmo com interrogação
    if question and not has_case_data and not (h.tenor_months or h.area_hectares):
        policy = _policy_answer(t, user, knowledge)
        if policy.kind == "policy_answer":
            return policy
        if status == CaseStatus.waiting_input and state.missing_info is not None:
            return AssistReply(
                kind="clarify",
                message="Preciso dessa informação porque a análise não fecha sem ela. " + state.missing_info.message,
            )
        # sem nenhum dado reconhecido é só pergunta, e pergunta não pode virar observação do case
        if not (context_from(h) or h.crop or h.cycle or h.purpose):
            return AssistReply(
                kind="no_data",
                message="Não tenho essa informação nesta demonstração e não registrei a pergunta como contexto do case. "
                "Posso falar sobre a squad, políticas, garantias, documentos e preços de referência. Se for um dado ou "
                "um pedido de ajuste para a análise, escreva como afirmação.",
                suggestions=["Qual é a squad?", "Quais são as garantias possíveis?"],
            )
    if status == CaseStatus.human_review_required:
        return AssistReply(kind="adjustment")
    return AssistReply(kind="case_reply")


# ------------------------------------------------------------------ respostas diretas


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


# o texto já chega sem acento e em minúsculas (`_plain`)
_ACK = re.compile(r"^(ok(ay)?|obrigad[oa]|valeu|beleza|blz|certo|entendi|perfeito|show|tudo bem|combinado)[\s!.]*$")
_RUN_COMMAND = re.compile(
    r"^(pode\s+|vamos\s+|por favor,?\s+)?(execut|rod|inici|comec)\w*(\s+(a|o|essa|esta))?(\s+(squad|analise|agora))*[\s!.]*$"
)
_SQUAD = re.compile(
    r"\bsquad\b|\bagentes?\b|\bequipe\b|\btimes?\b|\betapas?\b|\bprocesso\b|proximo passo|o que acontece|"
    r"quem (vai )?(analisa|participa)|como funciona (a |essa |esta )?(analise|squad)"
)
_PRICE = re.compile(r"\bprecos?\b|\bcotac(ao|oes)\b")
_NAMES_ANOTHER_CROP = re.compile(r"\b(?:precos?|cotac(?:ao|oes))\s+d[aeo]s?\s+\w+")
_GUARANTEE = re.compile(r"\bgarantias?\b")
_INTEREST = re.compile(r"\bjuros\b|\btaxas?\b|\bspread\b")
_FX = re.compile(r"\bdolar\b|\bcambio\b|\beuro\b|\bbolsa\b|\bchicago\b")
_RATING = re.compile(r"\brating\b|nota interna")
_CLIENT_LIST = re.compile(
    r"(quais|que|quantos|lista\w*)\b.*\bclientes\b|\bclientes\b.*\b(existem|disponiveis|tenho)\b|\bcarteira\b"
)
_SETS_VALUE = re.compile(r"\b(consider|inclu|use\b|usar\b|assum|ajust|troqu|mude|mudar|reduz|retir|adicion)")

SQUAD = (
    ("agro_eligibility", "Elegibilidade", "checa documentação e enquadramento antes de qualquer cálculo"),
    ("agro_credit_risk", "Risco", "avalia a capacidade de pagamento e os cenários de estresse"),
    ("agro_structuring", "Estruturação", "propõe estruturas comparáveis a partir do catálogo"),
    ("credit_review", "Revisor", "faz a revisão independente, procura inconsistências e pede retrabalho"),
)
_CROP_LABEL = {"algodao": "algodão", "cafe": "café", "feijao": "feijão"}
_PURPOSE_LABEL = {"comercializacao": "comercialização", "industrializacao": "industrialização"}
_PURPOSES = ("custeio", "investimento", "comercializacao")
_GUARANTEE_LABEL = {
    "penhor_safra": "penhor de safra",
    "cpr_financeira": "CPR financeira",
    "aval_socios": "aval dos sócios",
    "alienacao_fiduciaria_graos": "alienação fiduciária de grãos",
    "alienacao_fiduciaria_bens": "alienação fiduciária de bens",
    "cessao_contratos_venda": "cessão de contratos de venda",
    "hipoteca_rural": "hipoteca rural",
    "seguro_agricola": "seguro agrícola",
    "penhor_de_estoque": "penhor de estoque",
}


def _direct_answer(
    t: str,
    plain: str,
    question: bool,
    h: InterpretedDemand,
    user: UserIdentity,
    state: CaseState | None,
    repo: DataRepository | None,
) -> AssistReply | None:
    """Respostas que não dependem de busca. Só para perguntas ou frases curtas sem valor ("preço da soja"): quem
    escreve "considere o preço a 120" ou "as garantias são penhor e aval" está passando um dado, não perguntando."""
    short = len(plain.split()) <= 6
    if not (question or short) or h.requested_amount is not None or re.search(r"\d", plain):
        return None
    if not question and _SETS_VALUE.search(plain):
        return None
    if _SQUAD.search(plain):
        return _squad_answer(state)
    if h.guarantees and not question:
        return None
    if _PRICE.search(plain):
        return _price_answer(h, plain, user, state, repo)
    if _GUARANTEE.search(plain):
        return _guarantee_answer(h, user, state, repo)
    if _RATING.search(plain):
        return _no_data("Rating e notas internas são campos restritos: ficam fora de qualquer resposta nesta demonstração.")
    if _CLIENT_LIST.search(plain):
        return _no_data(
            "Não listo clientes: a busca só aceita um nome ou código específico, para que ninguém percorra a carteira. "
            "Me diga para quem é a operação."
        )
    if _INTEREST.search(plain):
        return _no_data(
            "A demonstração não tem taxas: o catálogo traz só uma nota indicativa, e a precificação é uma etapa posterior."
        )
    if _FX.search(plain):
        return _no_data("Não tenho câmbio nem cotações de bolsa. A base de mercado traz só preços fictícios por cultura.")
    if question and h.client_ref and not h.purpose and not _DEMAND.search(t):
        return _no_data(
            "Dados de cliente só são consultados dentro de uma análise, com o escopo travado nesse cliente. "
            "Para abrir uma, me diga o valor e a finalidade da operação."
        )
    return None


def _no_data(message: str) -> AssistReply:
    return AssistReply(kind="no_data", message=message)


def _next_step(state: CaseState | None) -> str:
    if state is None:
        return "Para montar a squad, me diga o cliente, o valor e a finalidade da operação."
    if state.status == CaseStatus.planned:
        return (
            "Agora é com você: use Executar squad ou me peça para executar. "
            "Dados novos que você me passar entram como contexto."
        )
    if state.status == CaseStatus.waiting_input and state.missing_info is not None:
        return "A análise está esperando uma informação sua. " + state.missing_info.message
    if state.status == CaseStatus.running:
        return "A squad está trabalhando agora; acompanhe o andamento aqui na conversa."
    if state.status == CaseStatus.human_review_required:
        return "O relatório está pronto: a decisão é sua. Você pode aprovar para a próxima etapa ou pedir um ajuste."
    return "Esta análise não está em andamento."


def _ack_message(state: CaseState | None) -> str:
    return "Combinado. " + _next_step(state)


def _run_command(state: CaseState | None) -> AssistReply:
    if state is not None and state.status == CaseStatus.planned:
        return AssistReply(kind="run_squad")
    if state is None:
        return _clarify("Ainda não há uma operação para analisar.")
    return AssistReply(kind="info", message=_next_step(state))


def _squad_answer(state: CaseState | None) -> AssistReply:
    selected = set(state.selected_agents) if state is not None else set()
    members = [(label, what) for agent_id, label, what in SQUAD if not selected or agent_id in selected]
    return AssistReply(
        kind="info",
        message="A squad de crédito agro tem estes agentes, nesta ordem. Cada um recebe só os dados que a sua tarefa exige, "
        "e a decisão final é sempre humana. " + _next_step(state),
        bullets=[f"{label}: {what}." for label, what in members],
    )


def _price_answer(
    h: InterpretedDemand, plain: str, user: UserIdentity, state: CaseState | None, repo: DataRepository | None
) -> AssistReply:
    crop = h.crop
    # "e o preço?" fala da cultura do case; "preço do girassol" fala de outra, e não recebe o preço da do case
    if crop is None and not _NAMES_ANOTHER_CROP.search(plain) and state is not None and state.interpreted:
        crop = state.interpreted.crop
    if crop is None:
        known = ", ".join(_CROP_LABEL.get(c, c) for c in COMMON_CROPS)
        return AssistReply(kind="clarify", message=f"De qual cultura? A base de demonstração tem referência de {known}.")
    label = _CROP_LABEL.get(crop, crop)
    if repo is None or "market_data" not in user.permissions_read:
        return _no_data("Seu perfil não tem acesso à base de mercado.")
    row = repo.get_market_data(crop)
    if row is None:
        return _no_data(
            f"Não tenho cotação de {label} ({crop}) na base de demonstração, e não uso o preço de outra cultura no lugar."
        )
    # só campos públicos: trader_desk_notes é `never` na política de recursos
    price = f"{float(row['reference_price']):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return AssistReply(
        kind="info",
        message=f"Referência fictícia de {label} na base de demonstração. Não é cotação real de mercado.",
        bullets=[f"{price} {row['unit']}, com data de {row['price_date']}.", str(row.get("trend_note") or "")],
    )


def _guarantee_answer(
    h: InterpretedDemand, user: UserIdentity, state: CaseState | None, repo: DataRepository | None
) -> AssistReply | None:
    if repo is None or "product_catalog" not in user.permissions_read:
        return None  # segue para a busca nas políticas
    demand = state.interpreted if state is not None else None
    purpose = h.purpose or (demand.purpose if demand else None)
    crop = h.crop or (demand.crop if demand else None)
    products = [p for key in ((purpose,) if purpose in _PURPOSES else _PURPOSES) for p in repo.list_products(key)]
    if crop:
        products = [p for p in products if product_supports_crop(p, crop)]
    options = dict.fromkeys(o for p in products for o in p.get("guarantee_options", []))
    if not options:
        return _no_data("Não há produto no catálogo de demonstração para essa combinação de finalidade e cultura.")
    what = " de ".join(x for x in (_PURPOSE_LABEL.get(purpose or "", purpose), _CROP_LABEL.get(crop or "", crop)) if x)
    return AssistReply(
        kind="info",
        message=f"Garantias aceitas no catálogo fictício{' para ' + what if what else ''}:",
        bullets=[_sentence_case(_GUARANTEE_LABEL.get(o, o.replace("_", " "))) for o in options],
        suggestions=["Quais documentos são obrigatórios para custeio?"],
    )


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:]  # capitalize() estragaria siglas como CPR


def _policy_answer(t: str, user: UserIdentity, knowledge: KnowledgeRetriever) -> AssistReply:
    if "knowledge" not in user.permissions_read:
        return AssistReply(kind="clarify", message="Seu perfil não tem acesso à base de políticas internas.")
    chunks = [c for c in knowledge.search(t, top_k=3) if c.get("score", 0) >= MIN_POLICY_SCORE]
    if not chunks:
        return _clarify("Não encontrei isso nas políticas internas.")
    return AssistReply(
        kind="policy_answer",
        message="Encontrei isto nas políticas internas (dados de demonstração):",
        bullets=[f"{_short_title(c['title'])}: {_gist(c['text'])}" for c in chunks],
        citations=[
            Citation(id=f"KB-{c['doc_id']}-c{c['chunk_index']}", title=c["title"], excerpt=c["text"][:900]) for c in chunks
        ],
        suggestions=["Analisar uma operação com base nisso", "Quais documentos são obrigatórios para custeio?"],
    )


def _clarify(lead: str = "") -> AssistReply:
    return AssistReply(
        kind="clarify",
        message=(lead + " " if lead else "")
        + "Posso seguir de dois jeitos: analisar uma operação de um cliente (me diga quem é, o valor e a finalidade) "
        "ou responder uma dúvida sobre políticas, documentos e produtos. O que você prefere?",
        suggestions=EXAMPLES,
    )


_CLASSIFICATION = {
    "comfortable": "confortável",
    "reduced_buffer": "folga reduzida",
    "attention_required": "requer atenção",
    "insufficient": "insuficiente",
}


def _case_answer(t: str, r: Report) -> AssistReply | None:
    low = t.lower()
    if re.search(r"risco|estresse|cobertura|pagamento|caixa|cen[aá]rio", low):
        bullets = [
            f"{s.label}: cobertura de {s.coverage:.2f}x".replace(".", ",")
            + f" ({_CLASSIFICATION.get(s.classification, s.classification)})"
            for s in r.stress_scenarios
        ] + [f.text for f in r.risk_factors[:3]]
        return AssistReply(kind="case_question", message="Sobre risco e capacidade de pagamento:", bullets=bullets)
    if re.search(r"alternativ|estrutura|garantia|prazo|amortiza", low):
        bullets = [
            f"{a.name}: {a.tenor_months} meses, {a.amortization.replace('_', ' ')}, garantias {', '.join(a.guarantees)}"
            for a in r.alternatives
        ]
        return AssistReply(
            kind="case_question",
            message="As estruturas comparáveis que a squad propôs (sem preferência do sistema):",
            bullets=bullets,
        )
    if re.search(r"pend[eê]nc|document|falta|incerte", low):
        items = [i.text for i in r.missing_data + r.uncertainties]
        return AssistReply(
            kind="case_question",
            message="Pendências e incertezas registradas:" if items else "O relatório não registrou pendências.",
            bullets=items,
        )
    if re.search(r"revis|achado|problema|erro|corrig", low):
        bullets = [f"{f.message} ({'resolvido' if f.status == 'resolved' else f.status})" for f in r.review.findings]
        return AssistReply(kind="case_question", message="O que a revisão encontrou:", bullets=bullets)
    return None


def _short_title(title: str) -> str:
    return title.split(" — ")[-1]


def _gist(text: str) -> str:
    lines = [ln.strip("- ").strip().rstrip(".;") for ln in text.splitlines() if ln.strip()]
    gist = "; ".join(lines[:3]) + "."
    return gist if len(gist) <= 260 else gist[:257].rstrip() + "…"
