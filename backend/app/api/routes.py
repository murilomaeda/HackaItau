"""Endpoints (ARCHITECTURE.md §19.9). Todos, menos /health, /identities, /benchmarks/latest e /auth/login, exigem token.

O usuário vem só do token assinado. Um case de outro usuário responde 404, como um case que não existe: a API não
confirma IDs alheios.
"""

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.container import Container, get_container, llm_mode
from app.core.events import ChainCheck
from app.core.schemas.case import (
    AttachmentRequest,
    CaseState,
    CreateCaseRequest,
    HumanReviewRequest,
    InputRequest,
    ReplyRequest,
)
from app.core.schemas.context import UserIdentity
from app.core.schemas.events import Event
from app.core.schemas.evidence import AgentOutputRecord, CalculationRecord, SourceRecord
from app.core.schemas.report import Report
from app.evaluation.results import latest_summary
from app.governance.loader import load_credentials, load_identities
from app.orchestration.assist import AssistReply, AssistRequest, assist
from app.orchestration.metrics import compute_metrics
from app.orchestration.orchestrator import OrchestratorError
from app.security.auth import Session, TokenError, issue, verify, verify_password

router = APIRouter()
Deps = Annotated[Container, Depends(get_container)]
_bearer = HTTPBearer(auto_error=False)


def _handle(exc: OrchestratorError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.detail})


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"code": code, "message": message}, headers={"WWW-Authenticate": "Bearer"})


def _can_sign_in(c: Container, user_id: str) -> bool:
    """Usuário de demonstração só vale com DEMO_MODE ligado; fora disso, precisa de hash próprio em identities.json."""
    cred = load_credentials().get(user_id)
    return cred is not None and cred.password_hash is not None and (c.settings.demo_mode or not cred.demo)


def _revoked(c: Container, jti: str) -> bool:
    return c.db.is_revoked(jti) if c.db is not None else jti in c.revoked


def current_session(
    request: Request, c: Deps, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> tuple[UserIdentity, Session]:
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized("unauthenticated", "faça login para continuar")
    try:
        session = verify(c.session_key, creds.credentials)
    except TokenError as exc:
        raise _unauthorized("invalid_session", "sessão inválida ou expirada") from exc
    user = load_identities().get(session.user_id)
    if user is None or _revoked(c, session.jti) or not _can_sign_in(c, user.user_id):
        raise _unauthorized("invalid_session", "sessão inválida ou expirada")
    return user, session


def current_user(auth: Annotated[tuple[UserIdentity, Session], Depends(current_session)]) -> UserIdentity:
    return auth[0]


User = Annotated[UserIdentity, Depends(current_user)]


def _same_user(user: UserIdentity, claimed: str | None) -> None:
    """Corpo antigo ainda manda user_id: aceito só se for o mesmo do token."""
    if claimed is not None and claimed != user.user_id:
        raise HTTPException(status_code=403, detail={"code": "user_mismatch", "message": "usuário diferente da sessão"})


def _owned(c: Container, case_id: str, user: UserIdentity):
    try:
        rec = c.orchestrator.get(case_id)
    except OrchestratorError as exc:
        raise _handle(exc) from exc
    if rec.state.user_id != user.user_id:
        raise HTTPException(status_code=404, detail={"code": "case_not_found", "message": "case não encontrado"})
    return rec


class LoginRequest(BaseModel):
    user_id: str
    password: str


class LoginReply(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int
    user: UserIdentity


@router.post("/auth/login", response_model=LoginReply)
def login(body: LoginRequest, request: Request, c: Deps) -> LoginReply:
    """Senha conferida com scrypt; falhas contadas por usuário e por endereço (429 ao passar do limite)."""
    user_id = body.user_id.strip().lower()
    keys = (f"user:{user_id}", f"ip:{request.client.host if request.client else '?'}")
    if wait := c.throttle.retry_after(*keys):
        raise HTTPException(
            status_code=429,
            detail={"code": "too_many_attempts", "message": f"muitas tentativas; tente de novo em {wait} s"},
            headers={"Retry-After": str(wait)},
        )
    cred = load_credentials().get(user_id)
    ok = verify_password(body.password, cred.password_hash if cred else None) and _can_sign_in(c, user_id)
    if not ok:
        c.throttle.failed(*keys)
        raise _unauthorized("invalid_credentials", "usuário ou senha incorretos")
    c.throttle.succeeded(*keys)
    token, session = issue(c.session_key, user_id, c.settings.session_ttl_minutes * 60)
    return LoginReply(access_token=token, expires_at=session.expires_at, user=load_identities()[user_id])


@router.post("/auth/logout", status_code=204)
def logout(auth: Annotated[tuple[UserIdentity, Session], Depends(current_session)], c: Deps) -> None:
    session = auth[1]
    if c.db is not None:
        c.db.revoke(session.jti, session.expires_at)
    else:
        c.revoked.add(session.jti)


@router.get("/auth/me", response_model=UserIdentity)
def me(user: User) -> UserIdentity:
    return user


@router.get("/health")
def health(c: Deps) -> dict:
    return {"ok": True, "llm_mode": llm_mode(c.settings), "demo_mode": c.settings.demo_mode}


@router.get("/identities")
def identities(c: Deps) -> list[dict]:
    """Usuários de demonstração, para a tela de login. Fora do DEMO_MODE não lista ninguém (sem enumeração)."""
    if not c.settings.demo_mode:
        return []
    creds = load_credentials()
    return [u.model_dump() for u in load_identities().values() if creds[u.user_id].demo]


@router.post("/assist", response_model=AssistReply)
def assist_message(body: AssistRequest, user: User, c: Deps) -> AssistReply:
    """Lê a mensagem do analista: demanda (abrir case), dúvida de política, pergunta sobre o case, ajuste…"""
    _same_user(user, body.user_id)
    state = _owned(c, body.case_id, user).state if body.case_id else None
    return assist(body.text, user, state, c.knowledge, c.repo)


@router.get("/metrics")
def metrics(_: User, c: Deps) -> dict:
    """Desempenho dos agentes nos cases guardados (agregado: sem dados de cliente)."""
    return compute_metrics(c.store, c.agents)


@router.get("/benchmarks/latest")
def benchmark_latest(c: Deps) -> dict:
    """Resumo do comparativo executado pelo CLI. Público: a tela de entrada mostra estes números antes do login, e
    o resumo só tem agregados de casos fictícios (custo, tempo, acertos), nenhum dado de cliente."""
    return {"benchmark": latest_summary(c.settings.benchmark_results_dir, c.settings.benchmark_snapshot_file)}


@router.post("/cases", response_model=CaseState, status_code=201)
async def create_case(body: CreateCaseRequest, user: User, c: Deps) -> CaseState:
    _same_user(user, body.user_id)
    try:
        return (await c.orchestrator.create_case(user.user_id, body.prompt, body.demo_options)).state
    except OrchestratorError as exc:
        raise _handle(exc) from exc


@router.get("/cases/{case_id}", response_model=CaseState)
def get_case(case_id: str, user: User, c: Deps) -> CaseState:
    return _owned(c, case_id, user).state


@router.get("/cases/{case_id}/events", response_model=list[Event])
def list_events(case_id: str, user: User, c: Deps, after: Annotated[int, Query(ge=0)] = 0) -> list[Event]:
    return _owned(c, case_id, user).events.list_after(after)


@router.get("/cases/{case_id}/audit/verify", response_model=ChainCheck)
def verify_audit(case_id: str, user: User, c: Deps) -> ChainCheck:
    """Recalcula a cadeia HMAC do audit gravado: ok, ou o primeiro evento alterado, removido ou fora de ordem."""
    _owned(c, case_id, user)
    return c.store.verify_audit(case_id)


@router.post("/cases/{case_id}/input", response_model=CaseState)
def provide_input(case_id: str, body: InputRequest, user: User, c: Deps) -> CaseState:
    _owned(c, case_id, user)
    try:
        return c.orchestrator.provide_input(case_id, body.answers).state
    except OrchestratorError as exc:
        raise _handle(exc) from exc


@router.post("/cases/{case_id}/reply", response_model=CaseState)
def reply(case_id: str, body: ReplyRequest, user: User, c: Deps) -> CaseState:
    """Resposta em texto livre: completa um pedido de informação ou acrescenta contexto antes de rodar a squad."""
    _owned(c, case_id, user)
    try:
        return c.orchestrator.provide_text(case_id, body.text).state
    except OrchestratorError as exc:
        raise _handle(exc) from exc


@router.post("/cases/{case_id}/attachments", response_model=CaseState, status_code=201)
def attach(case_id: str, body: AttachmentRequest, user: User, c: Deps) -> CaseState:
    """Documento anexado na conversa (base64). Vira documento do case, lido pelos agentes via Gateway."""
    _same_user(user, body.user_id)
    _owned(c, case_id, user)
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_base64", "message": "arquivo inválido"}) from exc
    try:
        return c.orchestrator.attach_document(case_id, user.user_id, body.filename, data).state
    except OrchestratorError as exc:
        raise _handle(exc) from exc


@router.post("/cases/{case_id}/run", response_model=CaseState, status_code=202)
async def run_case(case_id: str, user: User, c: Deps) -> CaseState:
    """Dispara a execução em background; acompanhe por GET /cases/{id} e /events."""
    _owned(c, case_id, user)
    try:
        return c.orchestrator.start_run(case_id).state
    except OrchestratorError as exc:
        raise _handle(exc) from exc


@router.post("/cases/{case_id}/retry", response_model=CaseState, status_code=202)
async def retry_case(case_id: str, user: User, c: Deps) -> CaseState:
    """Retoma um case `failed` a partir do agente que falhou (os que já concluíram não rodam de novo)."""
    _owned(c, case_id, user)
    try:
        return c.orchestrator.start_retry(case_id).state
    except OrchestratorError as exc:
        raise _handle(exc) from exc


@router.get("/cases/{case_id}/report", response_model=Report)
def get_report(case_id: str, user: User, c: Deps) -> Report:
    report = _owned(c, case_id, user).state.report
    if report is None:
        raise HTTPException(
            status_code=409, detail={"code": "report_not_ready", "message": "relatório ainda não consolidado"}
        )
    return report


@router.get("/cases/{case_id}/evidence/{evidence_id}", response_model=SourceRecord | CalculationRecord | AgentOutputRecord)
def get_evidence(
    case_id: str, evidence_id: str, user: User, c: Deps
) -> SourceRecord | CalculationRecord | AgentOutputRecord:
    """Payload por trás de um source/calculation/output ID citado no relatório (já filtrado pelo Gateway)."""
    item = _owned(c, case_id, user).evidence.get(evidence_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "evidence_not_found", "message": evidence_id})
    return item


@router.post("/cases/{case_id}/human-review", response_model=CaseState)
async def human_review(case_id: str, body: HumanReviewRequest, user: User, c: Deps) -> CaseState:
    """async: um ajuste com target_agent dispara a reexecução em background no event loop."""
    _owned(c, case_id, user)
    try:
        return c.orchestrator.human_review(case_id, body.decision, body.comment, body.target_agent).state
    except OrchestratorError as exc:
        raise _handle(exc) from exc
