"""Punto de entrada de la aplicación."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import maintenance, quemaos, settings_store
from .config import get_config
from .database import init_db, session_scope
from .templating import render

log = logging.getLogger("drive")


def configure_logging() -> None:
    config = get_config()
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.logging.file:
        Path(config.logging.file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                config.logging.file, maxBytes=10 * 1024**2, backupCount=5, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    if not config.logging.access_log:
        logging.getLogger("uvicorn.access").disabled = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()

    # Los ajustes guardados desde el panel se aplican sobre el YAML.
    with session_scope() as db:
        settings_store.apply_overrides(db)

    config = get_config()
    for warning in config.validate_runtime():
        log.warning(warning)

    thread = stop = None
    if config.maintenance.enabled:
        thread, stop = maintenance.start()

    probe = quemaos.start(config)

    log.info("%s listo en %s", config.app.name, config.app.base_url)
    yield

    if probe is not None:
        probe.stop()

    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=5)


def create_app() -> FastAPI:
    config = get_config()
    app = FastAPI(
        title=config.app.name,
        description="Almacenamiento de ficheros autoalojado.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from .routers import (
        admin_routes,
        api_routes,
        auth_routes,
        files_routes,
        profile_routes,
        public_routes,
        shares_routes,
    )

    app.include_router(auth_routes.router)
    app.include_router(files_routes.router)
    app.include_router(shares_routes.router)
    app.include_router(public_routes.router)
    app.include_router(profile_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(api_routes.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        headers = get_config().security.headers
        if headers.content_security_policy:
            response.headers.setdefault(
                "Content-Security-Policy", headers.content_security_policy
            )
        if headers.frame_options:
            response.headers.setdefault("X-Frame-Options", headers.frame_options)
        if headers.referrer_policy:
            response.headers.setdefault("Referrer-Policy", headers.referrer_policy)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if headers.hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        """Errores en JSON para la API y en HTML para el navegador."""
        wants_json = request.url.path.startswith("/api") or "application/json" in (
            request.headers.get("accept", "")
        )
        if wants_json:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))

        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            target = request.url.path
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(
                f"/login?next={target}", status.HTTP_303_SEE_OTHER
            )

        # La página de error se renderiza sin principal a propósito: el
        # manejador corre fuera del ciclo de dependencias y abrir aquí una
        # sesión de BD dejaría conexiones colgando en cada 404.
        return render(
            request, "error.html", None,
            status_code=exc.status_code,
            code=exc.status_code,
            detail=exc.detail,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return render(
            request, "error.html", None,
            status_code=status.HTTP_400_BAD_REQUEST,
            code=400,
            detail="La petición no es válida.",
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
