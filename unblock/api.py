import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mangum import Mangum
from pydantic import BaseModel, Field

from unblock import service, storage
from unblock.auth import Identity, identity, writer
from unblock.config import settings
from unblock.domain import NewCase
from unblock.presentation import present, redact
from unblock.repository import Conflict, Missing, repository

app = FastAPI(title="Unblock API", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
WEB = Path(__file__).parent / "web"
User = Annotated[Identity, Depends(identity)]
Writer = Annotated[Identity, Depends(writer)]


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://*.amazoncognito.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        }
    )
    return response


@app.exception_handler(Conflict)
async def conflict(request, error):
    return JSONResponse(status_code=409, content={"detail": "The case changed. Refresh and retry."})


@app.exception_handler(Missing)
async def missing(request, error):
    return JSONResponse(status_code=404, content={"detail": "Case or document not found."})


@app.exception_handler(ValueError)
async def invalid(request, error):
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/config")
def config():
    cfg = settings()
    return {
        "clientId": cfg.unblock_client_id,
        "authDomain": cfg.unblock_auth_domain,
        "local": cfg.app_env == "local",
        "demoAvailable": bool(cfg.unblock_demo_username and cfg.unblock_demo_password),
        "version": "0.1.0",
    }


@app.post("/api/demo/session")
def demo_session():
    """Sign a visitor into the shared read-only demo account without exposing its password."""
    cfg = settings()
    if not (cfg.unblock_demo_username and cfg.unblock_demo_password):
        raise HTTPException(404, "No demo workspace is configured.")
    import boto3

    try:
        result = boto3.client("cognito-idp", region_name=cfg.aws_default_region).initiate_auth(
            ClientId=cfg.unblock_client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": cfg.unblock_demo_username,
                "PASSWORD": cfg.unblock_demo_password,
            },
        )
    except Exception:
        raise HTTPException(503, "The demo workspace is unavailable right now.") from None
    return {"token": result["AuthenticationResult"]["IdToken"]}


@app.get("/api/me")
def me(user: User):
    return user.model_dump()


@app.get("/api/cases")
def cases(user: User):
    return [present(c) for c in repository().list(user.tenant)]


@app.post("/api/cases", status_code=201)
def create(
    data: NewCase,
    user: Writer,
    idempotency_key: Annotated[str, Header(min_length=8, max_length=100)],
):
    return present(
        service.create_case(repository(), user.tenant, data, idempotency_key, user.actor)
    )


@app.get("/api/cases/{case_id}")
def case(case_id: str, user: User):
    return present(repository().get(user.tenant, case_id))


@app.get("/api/cases/{case_id}/audit")
def audit(case_id: str, user: User):
    repo = repository()
    current = repo.get(user.tenant, case_id)
    return redact(repo.audit(user.tenant, case_id), current.get("reply_token") or "")


def local_job(job):
    from unblock.agent import process_job

    try:
        process_job(repository(), job)
    except Exception as error:
        logging.getLogger(__name__).error("Local agent job failed: %s", type(error).__name__)


@app.post("/api/cases/{case_id}/run", status_code=202)
def run(case_id: str, user: Writer, tasks: BackgroundTasks):
    repo = repository()
    result, job = service.enqueue(repo, repo.get(user.tenant, case_id), user.actor)
    if job and not settings().unblock_table:
        tasks.add_task(local_job, job)
    return present(result)


@app.post("/api/cases/{case_id}/documents", status_code=201)
async def upload(case_id: str, user: Writer, tasks: BackgroundTasks, file: UploadFile = File()):
    repo = repository()
    current = repo.get(user.tenant, case_id)
    # Uploads cannot race a running review; worker retries remain idempotent.
    if service.is_busy(current):
        raise HTTPException(409, "Wait for the current review to finish before uploading.")
    if current.get("review"):
        raise ValueError("Reviewed cases cannot receive new evidence.")
    data = await file.read(storage.MAX_BYTES + 1)
    content_type = file.content_type or "application/octet-stream"
    storage.validate_file(data, content_type)
    digest, key = storage.put(user.tenant, case_id, data, content_type)
    filename = Path(file.filename or "document").name[:160]
    return present(service.attach(repo, current, digest, key, filename, content_type, user.actor))


@app.get("/api/cases/{case_id}/documents/{document_id}")
def download(case_id: str, document_id: str, user: User):
    current = repository().get(user.tenant, case_id)
    document = next((d for d in current["documents"] if d["id"] == document_id), None)
    if not document:
        raise Missing()
    return Response(
        storage.get(document["key"]),
        media_type=document["content_type"],
        headers={"Content-Disposition": 'attachment; filename="evidence"'},
    )


class ReviewBody(BaseModel):
    version: int = Field(ge=1)
    note: str = Field(min_length=5, max_length=1000)


@app.post("/api/cases/{case_id}/review")
def review(case_id: str, body: ReviewBody, user: Writer):
    if not user.reviewer:
        raise HTTPException(403, "Only reviewers can accept an evidence packet.")
    repo = repository()
    return present(
        service.review(repo, repo.get(user.tenant, case_id), user.actor, body.version, body.note)
    )


class DismissBody(BaseModel):
    version: int = Field(ge=1)


@app.post("/api/cases/{case_id}/quarantine/{entry_id}/dismiss")
def dismiss(case_id: str, entry_id: str, body: DismissBody, user: Writer):
    if not user.reviewer:
        raise HTTPException(403, "Only reviewers can decide held messages.")
    repo = repository()
    return present(
        service.release_quarantine(
            repo, repo.get(user.tenant, case_id), entry_id, user.actor, body.version
        )
    )


@app.get("/api/cases/{case_id}/packet")
def packet(case_id: str, user: User):
    repo = repository()
    current = repo.get(user.tenant, case_id)
    # The reply token is a live capability; it must not travel inside an exported packet.
    export = {
        "case": present(current),
        "audit": redact(repo.audit(user.tenant, case_id), current.get("reply_token") or ""),
        "scope": "Evidence completeness only. This packet does not authorize payment or authenticate documents.",
    }
    return Response(
        json.dumps(export, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="unblock-evidence-packet.json"'},
    )


app.mount("/assets", StaticFiles(directory=WEB), name="assets")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


handler = Mangum(app, lifespan="off")
