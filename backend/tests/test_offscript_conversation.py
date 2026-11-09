"""Mensagens fora do roteiro: pergunta simples, nome escrito de outro jeito, outras culturas e dúvida sobre a squad."""

import pytest

from app.config import Settings
from app.container import build_container
from app.core.crops import COMMON_CROPS, expected_price_unit, productivity_unit
from app.core.schemas.case import CaseStatus, DemoOptions
from app.data.json_repository import JsonMockRepository
from app.governance.bootstrap_resolver import BootstrapClientResolver
from app.governance.loader import load_identities
from app.knowledge.retriever import KeywordKnowledgeRetriever, tokenize
from app.orchestration.assist import assist
from app.orchestration.clarify import demand_warnings, open_questions
from app.orchestration.interpreter import heuristic_interpret
from tests.fake_llm import StubProvider

ANALYST = "analyst-001"
PROMPT = "O cliente Fazenda Horizonte S.A. solicita R$ 50 milhões para custeio da safra de soja 2025/26."


def _container():
    c = build_container(Settings(llm_api_key="", _env_file=None))
    c.runtime.provider = StubProvider()
    return c


def _ask(c, text, state=None, user_id=ANALYST):
    return assist(text, load_identities()[user_id], state, c.knowledge, c.repo)


async def test_conversation_benchmark_has_no_regressions():
    """Offline e determinístico: qualquer caso que deixe de passar é regressão, não variação."""
    from app.evaluation.conversation import run

    out = await run()
    for section in ("routing", "lookup"):
        failed = [r for r in out[section]["rows"] if not r["passed"]]
        assert not failed, failed


# ------------------------------------------------------------------ sintoma 1: perguntas simples


@pytest.mark.parametrize("case_open", [False, True])
async def test_guarantee_question_lists_the_guarantees(case_open):
    c = _container()
    state = (await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())).state if case_open else None
    r = _ask(c, "Quais são as garantias possíveis?", state)
    text = " ".join(r.bullets).lower()
    assert r.kind == "info" and "penhor" in text and "cpr" in text and "aval" in text
    assert "MOCK-NEVER" not in str(r)


async def test_guarantees_follow_the_crop_of_the_open_case():
    """CPR Financeira Soja não pode aparecer como opção para um case de milho."""
    c = _container()
    state = (
        await c.orchestrator.create_case(
            ANALYST, "A Fazenda Três Irmãos pede R$ 10 milhões para custeio de milho 2026/27.", DemoOptions()
        )
    ).state
    r = _ask(c, "Quais garantias posso usar?", state)
    assert "milho" in r.message and "custeio" in r.message
    assert not any("alienacao" in b.lower() or "alienação" in b.lower() for b in r.bullets)


@pytest.mark.parametrize("text", ["Quanto está o preço da soja", "preço da soja", "Qual a cotação da soja hoje?"])
def test_price_question_answers_from_mock_market_data(text):
    r = _ask(_container(), text)
    joined = r.message + " ".join(r.bullets)
    assert r.kind == "info" and "135" in joined and "BRL/saca" in joined and "2026-09-01" in joined
    assert "fictíci" in joined.lower()
    assert "MOCK-NEVER" not in str(r) and not r.citations


def test_price_without_market_data_says_so_instead_of_citing_policies():
    class NoMarket(JsonMockRepository):
        def get_market_data(self, commodity):
            return None

    c = _container()
    r = assist("Quanto está o preço do algodão?", load_identities()[ANALYST], None, c.knowledge, NoMarket())
    assert r.kind == "no_data" and "algodao" in r.message.replace("ã", "a") and not r.citations


@pytest.mark.parametrize("case_open", [False, True])
async def test_price_of_an_unknown_crop_does_not_answer_with_another_crop(case_open):
    c = _container()
    state = (await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())).state if case_open else None
    r = _ask(c, "Quanto está o preço do girassol?", state)
    assert r.kind == "clarify" and "135" not in str(r) and "laranja" in r.message


def test_price_requires_market_permission():
    from app.core.schemas.context import UserIdentity

    c = _container()
    user = UserIdentity(user_id="u", name="x", role="r", permissions_read=("knowledge",))
    r = assist("Quanto está o preço da soja?", user, None, c.knowledge, c.repo)
    assert r.kind == "no_data" and "135" not in str(r)


async def test_price_uses_the_crop_of_the_open_case():
    c = _container()
    state = (await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())).state
    assert "135" in " ".join(_ask(c, "E o preço?", state).bullets + [_ask(c, "E o preço?", state).message])


@pytest.mark.parametrize(
    "text",
    ["Qual a taxa de juros?", "Quanto está o dólar?", "Qual o rating do cliente?", "quais clientes existem?"],
)
def test_data_the_demo_does_not_have_gets_an_honest_answer(text):
    r = _ask(_container(), text)
    assert r.kind == "no_data" and r.message and not r.citations


def test_question_about_a_client_does_not_open_a_case():
    r = _ask(_container(), "Qual o faturamento da Fazenda Horizonte?")
    assert r.kind == "no_data" and "valor" in r.message and "finalidade" in r.message


def test_retriever_indexes_every_corpus_document(knowledge):
    knowledge.search("custeio")
    loaded = {c["doc_id"] for c in knowledge._load()}
    assert {p.stem for p in knowledge._dir.glob("*.md")} == loaded


def test_stopwords_and_plurals_do_not_decide_the_match():
    assert "sao" not in tokenize("Quais são as garantias?") and "quais" not in tokenize("Quais são as garantias?")
    assert tokenize("garantias") == tokenize("garantia")


def test_generic_word_alone_does_not_retrieve_unrelated_policy():
    r = KeywordKnowledgeRetriever().search("Quais são as vantagens do preço para o cooperado?")
    assert all(c["score"] < 0.5 for c in r)


@pytest.mark.parametrize(
    ("text", "doc"),
    [
        ("O que é CPR?", "GLOSSARY-001"),
        ("Quais documentos são obrigatórios para investimento?", "POL-INV-003"),
        ("Quais documentos são obrigatórios para comercialização?", "POL-COM-004"),
        ("Qual o limite de alavancagem?", "POL-CRED-002"),
    ],
)
def test_policy_questions_reach_the_right_document(text, doc):
    r = _ask(_container(), text)
    assert r.kind == "policy_answer" and r.citations[0].id.startswith(f"KB-{doc}")


# ------------------------------------------------------------------ sintoma 2: nomes


@pytest.mark.parametrize(
    ("ref", "client_id"),
    [
        ("A fazenda horizonte", "CLIENTE-001"),
        ("a Horizonte", "CLIENTE-001"),
        ("Horizonte", "CLIENTE-001"),
        ("Horizonte S.A.", "CLIENTE-001"),
        ("FAZENDA  HORIZONTE, S/A", "CLIENTE-001"),
        ("Faz. Horizonte", "CLIENTE-001"),
        ("cliente Fazenda Horizonte", "CLIENTE-001"),
        ("agro delta", "CLIENTE-999"),
        ("delta", "CLIENTE-999"),
        ("fazenda tres irmaos", "CLIENTE-002"),
        ("Três Irmãos", "CLIENTE-002"),
        ("sitio bom jesus", "CLIENTE-007"),
        ("agropecuaria santa clara", "CLIENTE-004"),
    ],
)
def test_name_variations_resolve(repo, analyst, ref, client_id):
    r = BootstrapClientResolver(repo).resolve(analyst, ref)
    assert r.status == "ok" and r.client is not None and r.client.client_id == client_id, ref


def test_partial_name_shared_by_two_clients_asks_without_naming_them(repo, analyst):
    r = BootstrapClientResolver(repo).resolve(analyst, "Santa Clara")
    assert r.status == "ambiguous" and r.count == 2 and r.client is None
    assert "CLIENTE" not in r.model_dump_json().replace('"client":null', "")


@pytest.mark.parametrize(
    "ref",
    [
        "Fazenda",
        "fazenda s.a.",
        "Agro",
        "Agropecuária",
        "S.A.",
        "Faz",
        "Horiz",
        "Fazenda Horizonte Agro",
        "Horizontes Verdes",
    ],
)
def test_generic_or_prefix_or_extra_words_never_match(repo, analyst, ref):
    assert BootstrapClientResolver(repo).resolve(analyst, ref).status == "not_found", ref


async def test_symptom_sentence_opens_the_case_for_the_right_client():
    c = _container()
    text = "A fazenda horizonte quer 50 mil reais para o custeio da plantação de milho"
    assert _ask(c, text).kind == "credit_demand"
    rec = await c.orchestrator.create_case(ANALYST, text, DemoOptions())
    assert rec.state.scope is not None and rec.state.scope.client_ids == ("CLIENTE-001",)


async def test_llm_copying_the_article_still_resolves():
    """O prompt do LLM manda copiar o nome como está; "A fazenda horizonte" não pode virar cliente desconhecido."""
    c = _container()
    user = load_identities()[ANALYST]
    assert c.resolver.resolve(user, "A fazenda horizonte").status == "ok"


async def test_ambiguous_case_stays_unscoped_until_the_analyst_answers():
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, "O cliente Santa Clara pede R$ 5 milhões para custeio", DemoOptions())
    assert rec.state.scope is None and rec.state.status == CaseStatus.waiting_input
    assert rec.state.missing_info.reason == "client_ambiguous"
    c.orchestrator.provide_text(rec.state.case_id, "CLIENTE-003")
    assert rec.state.scope.client_ids == ("CLIENTE-003",)


async def test_scope_lock_survives_other_client_names_in_the_conversation():
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())
    c.orchestrator.provide_text(rec.state.case_id, "A Agro Delta também quer entrar, e a Fazenda Três Irmãos.")
    assert rec.state.scope.client_ids == ("CLIENTE-001",)


# ------------------------------------------------------------------ sintoma 3: outras culturas


@pytest.mark.parametrize(
    ("client", "crop", "client_id"),
    [
        ("Fazenda Três Irmãos", "milho", "CLIENTE-002"),
        ("Fazenda Santa Clara", "café", "CLIENTE-003"),
        ("Agropecuária Santa Clara", "trigo", "CLIENTE-004"),
        ("Cooperativa Vale do Rio Verde", "arroz", "CLIENTE-005"),
        ("Sítio Bom Jesus", "feijão", "CLIENTE-007"),
        ("Grupo Cerrado Verde", "algodão", "CLIENTE-006"),
        ("Usina Boa Esperança", "cana", "CLIENTE-008"),
        ("Agropecuária Boa Vista", "sorgo", None),
        ("Fazenda Pomar do Sol", "laranja", "CLIENTE-010"),
        ("Agropecuária Raiz Forte", "mandioca", "CLIENTE-011"),
        ("Fazenda Cabruca", "cacau", "CLIENTE-012"),
        ("Sítio Recanto do Vale", "banana", "CLIENTE-013"),
    ],
)
async def test_other_crops_run_the_whole_squad(client, crop, client_id):
    c = _container()
    rec = await c.orchestrator.create_case(
        ANALYST, f"{client} pede R$ 2 milhões para custeio de {crop} safra 2026/27.", DemoOptions()
    )
    assert not _warnings(rec)
    await c.orchestrator.run(rec.state.case_id)
    if client_id is None:  # Boa Vista não tem demonstrações financeiras: o bloqueio documental continua valendo
        assert rec.state.status == CaseStatus.waiting_input
        assert "demonstracoes_financeiras" in rec.state.missing_info.items
        return
    assert rec.state.scope.client_ids == (client_id,)
    assert rec.state.status == CaseStatus.human_review_required, (rec.state.error, rec.state.missing_info)


@pytest.mark.parametrize(
    ("client", "crop", "price", "productivity", "unit"),
    [
        ("Usina Boa Esperança", "cana", 150, 78, "t/ha"),
        ("Fazenda Pomar do Sol", "laranja", 45, 680, "cx/ha"),
        ("Grupo Cerrado Verde", "algodão", 135, 120, "@/ha"),
    ],
)
async def test_calculation_uses_the_unit_of_the_crop(client, crop, price, productivity, unit):
    c = _container()
    rec = await c.orchestrator.create_case(
        ANALYST, f"{client} pede R$ 5 milhões para custeio de {crop} safra 2026/27.", DemoOptions()
    )
    await c.orchestrator.run(rec.state.case_id)
    calc = next(x for x in rec.evidence.calculations() if x.name == "credit_metrics")
    assert calc.inputs["price"] == price and calc.inputs["productivity"] == productivity
    assert unit in rec.state.report.model_dump_json()


@pytest.mark.parametrize(
    ("purpose", "client", "crop", "client_id"),
    [
        ("investimento em silos", "Fazenda Três Irmãos", "milho", "CLIENTE-002"),
        ("comercialização", "Fazenda Santa Clara", "café", "CLIENTE-003"),
    ],
)
async def test_other_purposes_run_the_whole_squad(purpose, client, crop, client_id):
    c = _container()
    rec = await c.orchestrator.create_case(
        ANALYST, f"{client} pede R$ 5 milhões para {purpose}, {crop} safra 2026/27.", DemoOptions()
    )
    assert rec.state.scope.client_ids == (client_id,)
    await c.orchestrator.run(rec.state.case_id)
    assert rec.state.status == CaseStatus.human_review_required, (rec.state.error, rec.state.missing_info)


def _warnings(rec):
    return [q for q in rec.state.open_questions if q.key.startswith("aviso_")]


@pytest.mark.parametrize(
    ("prompt", "key", "expected"),
    [
        (
            "A fazenda horizonte quer R$ 5 milhões para custeio de milho safra 2026/27",
            "aviso_cultura",
            ["soja", "milho"],
        ),
        ("A fazenda horizonte quer 50 mil reais para custeio de soja", "aviso_ticket", ["R$ 1.000.000"]),
    ],
)
async def test_known_blockers_are_flagged_before_spending_a_run(prompt, key, expected):
    """Sem isso, o analista só descobria o bloqueio depois de clicar em Executar squad."""
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, prompt, DemoOptions())
    warning = next(q for q in rec.state.open_questions if q.key == key)
    assert all(e in warning.question for e in expected) and not warning.blocking


@pytest.mark.parametrize(
    ("crop", "market", "expected"),
    [
        ("algodao", None, "algodao"),
        ("milho", {"unit": "BRL/tonelada"}, "BRL/saca"),
        ("cana", {"unit": "BRL/saca"}, "BRL/tonelada"),
    ],
)
def test_market_warning_names_the_problem(crop, market, expected):
    it = heuristic_interpret("x").model_copy(update={"crop": crop})
    warning = next(q for q in demand_warnings(it, crop, market, 1) if q.key == "aviso_mercado")
    assert expected in warning.question


def test_each_crop_has_one_price_unit_and_market_data_follows_it(repo):
    for crop in COMMON_CROPS:
        row = repo.get_market_data(crop)
        assert row is not None and row["unit"] == expected_price_unit(crop), crop
    assert expected_price_unit("soja") == "BRL/saca" and productivity_unit("cana") == "t/ha"
    assert productivity_unit("laranja") == "cx/ha" and productivity_unit("algodao") == "@/ha"


@pytest.mark.parametrize(
    ("text", "crop"),
    [
        ("custeio de laranja", "laranja"),
        ("custeio do laranjal", "laranja"),
        ("custeio de citros", "laranja"),
        ("plantar macaxeira", "mandioca"),
        ("plantar aipim", "mandioca"),
        ("custeio de mandioca", "mandioca"),
        ("custeio de cacau", "cacau"),
        ("custeio de banana", "banana"),
        ("custeio de cana", "cana"),
    ],
)
def test_common_crops_are_read(text, crop):
    assert heuristic_interpret(text).crop == crop


def test_volume_in_boxes_or_arrobas_is_not_money():
    it = heuristic_interpret("Fazenda Pomar do Sol quer R$ 6 milhões para custeio, colhe 500 mil caixas de laranja")
    assert it.requested_amount == 6_000_000
    assert heuristic_interpret("colhe 90 mil arrobas de cacau").requested_amount is None


async def test_correcting_the_crop_clears_the_warning():
    c = _container()
    rec = await c.orchestrator.create_case(
        ANALYST, "A fazenda horizonte quer R$ 5 milhões para custeio de milho 2026/27", DemoOptions()
    )
    assert "aviso_cultura" in [q.key for q in rec.state.open_questions]
    c.orchestrator.provide_text(rec.state.case_id, "Na verdade é soja")
    assert "aviso_cultura" not in [q.key for q in rec.state.open_questions]


async def test_divergent_crop_is_still_blocked_at_eligibility():
    """O aviso antecipa, não relaxa: milho sobre perfil de soja continua parando antes dos cálculos."""
    c = _container()
    rec = await c.orchestrator.create_case(
        ANALYST, "A fazenda horizonte quer R$ 5 milhões para custeio de milho 2026/27", DemoOptions()
    )
    await c.orchestrator.run(rec.state.case_id)
    assert rec.state.status == CaseStatus.waiting_input and "crop" in rec.state.missing_info.items


def test_llm_missing_field_questions_do_not_duplicate_the_standard_ones():
    """Medido no app real: o LLM repetia como "ambiguidade" o que `open_questions` já pergunta."""
    it = heuristic_interpret("A fazenda horizonte quer 50 mil reais para o custeio da plantação de milho")
    real_ambiguity = "O pedido é para a Fazenda Horizonte ou para a Agro Delta?"
    questions = open_questions(
        it.model_copy(
            update={
                "ambiguities": [
                    "Qual é a safra específica?",
                    "Há garantias a serem apresentadas?",
                    "Qual é a região da operação?",
                    "Qual é a área em hectares?",
                    real_ambiguity,
                    "Confirme a cultura: entendi milho, mas o texto também cita soja.",
                ]
            }
        )
    )
    texts = [q.question for q in questions]
    assert real_ambiguity in texts and any(t.startswith("Confirme a cultura") for t in texts)
    assert not any(t in texts for t in ("Qual é a safra específica?", "Qual é a região da operação?"))
    assert sum("safra" in t.lower() and t.startswith("Qual") for t in texts) == 1


def test_cycle_question_does_not_ask_for_a_crop_already_known():
    it = heuristic_interpret("A fazenda horizonte quer R$ 5 milhões para custeio de milho")
    question = next(q.question for q in open_questions(it) if q.key == "crop_cycle")
    assert "cultura" not in question.lower() and "safra" in question.lower()


def test_guarantee_labels_keep_acronyms():
    assert "CPR financeira" in _ask(_container(), "Quais são as garantias possíveis?").bullets


# ------------------------------------------------------------------ sintoma 4: pergunta não vira observação


@pytest.mark.parametrize(
    "text", ["Qual é a squad", "Qual é a squad?", "quem são os agentes", "me explica a squad", "como funciona a análise?"]
)
async def test_squad_question_is_answered_not_recorded(text):
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())
    r = _ask(c, text, rec.state)
    joined = " ".join(r.bullets)
    assert r.kind == "info"
    assert all(a in joined for a in ("Elegibilidade", "Risco", "Estruturação", "Revisor"))
    assert "observacoes_do_analista" not in rec.answers


def test_squad_question_without_case_is_answered():
    r = _ask(_container(), "Qual é a squad?")
    assert r.kind == "info" and "Elegibilidade" in " ".join(r.bullets)


@pytest.mark.parametrize("text", ["o que você acha?", "o que acontece agora", "por que isso?", "qual a capital da França?"])
async def test_no_question_falls_through_to_case_notes(text):
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())
    r = _ask(c, text, rec.state)
    assert r.kind != "case_reply" and r.message


@pytest.mark.parametrize("text", ["ok", "obrigado", "Beleza!", "valeu"])
async def test_acknowledgement_is_not_a_case_note(text):
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())
    assert _ask(c, text, rec.state).kind == "info"


@pytest.mark.parametrize("text", ["pode executar", "rode a squad", "executar", "pode rodar a análise"])
async def test_run_command_with_a_planned_case_runs(text):
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())
    assert _ask(c, text, rec.state).kind == "run_squad"


@pytest.mark.parametrize(
    "text",
    [
        "Prazo de 18 meses",
        "O prazo é de 18 meses?",
        "milho 2025/2026. O prazo é para daqui a 6 meses.",
        "O cliente tem bom histórico de pagamento com a gente.",
        "As garantias são penhor e aval",
    ],
)
async def test_real_context_is_still_recorded(text):
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())
    assert _ask(c, text, rec.state).kind == "case_reply"


async def test_requests_in_human_review_are_still_adjustments():
    c = _container()
    rec = await c.orchestrator.create_case(ANALYST, PROMPT, DemoOptions())
    await c.orchestrator.run(rec.state.case_id)
    for text in ("Pode considerar o preço da soja a 120?", "Considere preço menor", "Pode incluir seguro agrícola?"):
        assert _ask(c, text, rec.state).kind == "adjustment", text
