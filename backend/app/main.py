from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings

# anexos chegam em base64 (2 MB de arquivo ≈ 2,7 MB de corpo); qualquer coisa bem maior é recusada antes de ler
MAX_BODY_BYTES = 4_000_000

# o frontend não carrega nada de fora: tudo vem da própria origem. /docs (Swagger, CDN) fica fora da CSP.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

app = FastAPI(title="Atena — Crédito Agro", version="0.1.0")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
        detail = {"code": "body_too_large", "message": "corpo grande demais"}
        return JSONResponse(status_code=413, content={"detail": detail})
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(router, prefix="/api")

_settings = get_settings()
if _settings.frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=_settings.frontend_dist, html=True), name="frontend")
