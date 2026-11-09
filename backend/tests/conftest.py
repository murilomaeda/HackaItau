import json
import os
from pathlib import Path

# testes não gravam em var/: cada um que precisa de banco cria o seu (tmp_path)
os.environ.setdefault("PERSISTENCE", "false")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.events import EventLog  # noqa: E402
from app.core.evidence import EvidenceRegistry  # noqa: E402
from app.core.schemas.context import CaseScope, ExecutionContext, UserIdentity  # noqa: E402
from app.data.json_repository import JsonMockRepository  # noqa: E402
from app.governance.loader import load_agent_cards, load_identities  # noqa: E402
from app.knowledge.retriever import KeywordKnowledgeRetriever  # noqa: E402
from app.tools.calc_tools import install_calc_handlers  # noqa: E402
from app.tools.data_tools import install_data_handlers  # noqa: E402
from app.tools.deps import ToolDeps  # noqa: E402
from app.tools.gateway import Toolbox  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
DEMO_PASSWORD = "atena-demo"  # senha de demonstração (governance/identities.json, só com DEMO_MODE)


def login(client, user_id: str = "analyst-001"):
    """Faz login pela API e deixa o token nos headers do TestClient. Devolve o próprio client."""
    r = client.post("/api/auth/login", json={"user_id": user_id, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return client


PURPOSE = "credit_analysis_agro"


@pytest.fixture
def unjustified_baseline(monkeypatch):
    """Injeta a premissa otimista antiga para continuar exercitando o fallback de revisão/retrabalho."""
    from app.agents.risk.agent import RiskAgent

    original = RiskAgent._policy

    def choose(self, task):
        selected = original(self, task)
        return "declared" if selected == "auto" else selected

    monkeypatch.setattr(RiskAgent, "_policy", choose)


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def golden_case() -> dict:
    return json.loads((FIXTURES / "golden_case.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def mock_data(settings) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in settings.mock_data_dir.glob("*.json")}


@pytest.fixture(scope="session")
def repo() -> JsonMockRepository:
    return JsonMockRepository()


@pytest.fixture(scope="session")
def knowledge() -> KeywordKnowledgeRetriever:
    return KeywordKnowledgeRetriever()


@pytest.fixture(scope="session")
def cards():
    return load_agent_cards()


@pytest.fixture(scope="session")
def analyst() -> UserIdentity:
    return load_identities()["analyst-001"]


@pytest.fixture(scope="session")
def manager() -> UserIdentity:
    return load_identities()["manager-001"]


@pytest.fixture
def scope_001() -> CaseScope:
    return CaseScope(client_ids=("CLIENTE-001",), purpose=PURPOSE)


def make_ctx(user: UserIdentity, agent_id: str, scope: CaseScope, purpose: str = PURPOSE) -> ExecutionContext:
    return ExecutionContext(
        user=user, case_id="case-test", task_id="t-1", agent_id=agent_id, purpose=purpose, case_scope=scope
    )


@pytest.fixture
def toolbox_factory(repo, knowledge, cards, analyst, scope_001):
    install_data_handlers()
    install_calc_handlers()

    def make(
        agent_id: str,
        *,
        user: UserIdentity = analyst,
        adversarial: bool = False,
        scope: CaseScope = scope_001,
        round_: int = 1,
        events: EventLog | None = None,
        evidence: EvidenceRegistry | None = None,
    ):
        events = events if events is not None else EventLog("case-test")
        evidence = evidence if evidence is not None else EvidenceRegistry()
        deps = ToolDeps(repo, knowledge, ("adversarial",) if adversarial else (), agent_id=agent_id, round=round_)
        return Toolbox(make_ctx(user, agent_id, scope), cards[agent_id], deps, events, evidence)

    return make
