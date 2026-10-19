import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import secrets
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from mailbox_clients import normalize_address_prefix, ProviderError
from sqlalchemy import select, text

from .config import ROOT, Settings
from .db import Database
from .models import Credential, Event, WaitJob, WorkerHeartbeat, Workspace, now
from .providers import PROVIDERS
from .service import DomainError, Service, aware, iso, problem, wait_dict, workspace_lock
from . import schemas as out


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateMailbox(Input):
    provider_id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=120)
    address_prefix: str = Field(default='', max_length=48, description='可选邮箱前缀；填写后追加16位随机字符，留空随机生成。仅支持 capabilities.custom_prefix=supported 的来源。')
    ttl_seconds: int = Field(default=3600, ge=300, le=86400, strict=True)
    labels: list[Annotated[str, Field(min_length=1, max_length=48)]] = Field(default_factory=list, max_length=16)

    @field_validator('address_prefix', mode='before')
    @classmethod
    def validate_address_prefix(cls, value):
        try:
            return normalize_address_prefix(value)
        except ProviderError as exc:
            raise ValueError(str(exc)) from None


class Predicate(Input):
    from_domain: str | None = Field(default=None, min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")
    subject_contains: str | None = Field(default=None, min_length=1, max_length=200)
    text_contains: str | None = Field(default=None, min_length=1, max_length=200)


class CreateWait(Input):
    after_seq: int | None = Field(default=None, ge=0, strict=True)
    timeout_seconds: int = Field(default=120, ge=5, le=300, strict=True)
    predicate: Predicate = Field(default_factory=Predicate)


class CreateToken(Input):
    scopes: list[Literal["mailbox:read", "messages:read", "messages:fetch", "waits:write", "events:read"]] = Field(min_length=1, max_length=5)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400, strict=True)


class ConfigureProvider(Input):
    credentials: dict[str, str] = Field(min_length=1, max_length=8)


def create_app(settings=None, database=None):
    settings = settings or Settings.load()
    db = database or Database(settings)
    service = Service(db)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(db.initialize)
        yield
        if database is None:
            db.close()

    def strict_query(request:Request):
        path=getattr(request.scope.get("route"),"path","")
        allowed={"cursor","limit"} if path in ("/api/v1/mailboxes","/api/v1/mailboxes/{mailbox_id}/messages","/api/v1/waits") else \
            {"timeout_seconds"} if path=="/api/v1/waits/{wait_id}" and request.method=="GET" else \
            {"cursor"} if path.endswith("/events") else set()
        if any(key not in allowed or len(request.query_params.getlist(key))>1 for key in request.query_params):
            raise DomainError(422,"unknown_query_parameter","查询参数未知或重复。")

    app = FastAPI(title="Linsemail Console API", version="0.3.0", docs_url="/api/docs", openapi_url="/api/v1/openapi.json", lifespan=lifespan,dependencies=[Depends(strict_query)])
    app.state.db, app.state.service = db, service

    @app.middleware("http")
    async def boundary(request, call_next):
        host = urlsplit("http://"+request.headers.get("host", "")).hostname
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1", "testserver"):
            return JSONResponse(problem(400, "invalid_host", "服务只接受本机访问。"), status_code=400, media_type="application/problem+json")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            allowed = {"http://127.0.0.1:"+str(settings.api_port), "http://localhost:"+str(settings.api_port), "http://127.0.0.1:5173", "http://localhost:5173"}
            if (origin and origin not in allowed) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(problem(403, "invalid_origin", "请求来源不受信任。"), status_code=403, media_type="application/problem+json")
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 16384:
                return JSONResponse(problem(413, "body_too_large", "请求体超过限制。"), status_code=413, media_type="application/problem+json")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/v1"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/":
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(problem(exc.status, exc.code, exc.detail), status_code=exc.status, media_type="application/problem+json")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        value=problem(422,"validation_failed","请求字段格式不正确。")
        value["errors"]=[{"pointer":"/"+"/".join(str(v) for v in err["loc"]),"code":err["type"],"message":err["msg"]} for err in exc.errors()]
        return JSONResponse(value,status_code=422,media_type="application/problem+json")

    def auth(request, scope=None, mailbox_id=None, workspace_only=False):
        header = request.headers.get("authorization", "")
        token=header[7:] if header.startswith("Bearer ") else request.cookies.get("lm_session")
        principal=service.principal(token)
        if principal["kind"]=="session" and request.method not in ("GET","HEAD"):
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), principal["csrf_token"] or ""):
                raise DomainError(403,"csrf_required","缺少有效的请求校验值。")
        if scope:
            service.require(principal,scope,mailbox_id,workspace_only)
        return principal

    @app.get("/api/v1/health", tags=["system"], response_model=out.Health)
    def health():
        with db.session() as session:
            session.execute(text("SELECT 1"))
            latest=session.scalar(select(WorkerHeartbeat.updated_at).order_by(WorkerHeartbeat.updated_at.desc()).limit(1))
        return {"status":"ok","database":"postgresql" if db.engine.dialect.name=="postgresql" else db.engine.dialect.name,
                "worker_online":bool(latest and (now()-aware(latest)).total_seconds()<15),"smtp_port":settings.smtp_port}

    @app.get("/api/v1/session", tags=["session"], response_model=out.Session)
    def session_status(request:Request):
        principal=auth(request)
        return {"workspace_id":principal["workspace_id"],"workspace_name":"本地工作区","csrf_token":principal["csrf_token"],"local_mode":settings.local_session}

    @app.post("/api/v1/session", tags=["session"], response_model=out.Session)
    def login(request:Request):
        if not settings.local_session or request.client.host not in ("127.0.0.1","::1","testclient"):
            raise DomainError(403,"local_login_disabled","本地会话入口不可用。")
        token,csrf=service.local_login()
        response=JSONResponse({"workspace_id":"ws_local","workspace_name":"本地工作区","csrf_token":csrf,"local_mode":True})
        response.set_cookie("lm_session",token,httponly=True,samesite="strict",max_age=43200,secure=False)
        return response

    @app.delete("/api/v1/session", tags=["session"])
    def logout(request:Request):
        principal=auth(request)
        if principal["kind"]=="session":
            with db.session.begin() as session:
                session.get(Credential,principal["id"]).revoked=True
        response=JSONResponse({"signed_out":True});response.delete_cookie("lm_session")
        return response

    @app.get("/api/v1/providers", tags=["providers"], response_model=out.ProviderPage)
    def providers(request:Request):
        items=service.list_providers(auth(request,"providers:read",workspace_only=True))
        external=[item for item in items if item["id"]!="local"]
        return {"items":items,"next_cursor":None,"has_more":False,"summary":{
            "external_total":len(external),"adapter_total":sum(item["adapter_implemented"] for item in external),
            "ready":sum(item["admission_state"]=="enabled" for item in external),
            "credential_required":sum(item["admission_state"]=="credential_required" for item in external),
            "pending":sum(not item["adapter_implemented"] for item in external),"local":1}}

    @app.get("/api/v1/providers/{provider_id}", tags=["providers"], response_model=out.Provider)
    def provider_detail(request:Request,provider_id:str):
        return service.get_provider(auth(request,"providers:read",workspace_only=True),provider_id)

    @app.put("/api/v1/providers/{provider_id}/configuration", tags=["providers"], response_model=out.Provider)
    def configure_provider(request:Request,provider_id:str,data:ConfigureProvider):
        return service.configure_provider(auth(request,"mailbox:write",workspace_only=True),provider_id,data.credentials)

    @app.delete("/api/v1/providers/{provider_id}/configuration", tags=["providers"], response_model=out.Provider)
    def clear_configuration(request:Request,provider_id:str):
        return service.clear_provider_configuration(auth(request,"mailbox:write",workspace_only=True),provider_id)

    @app.get("/api/v1/mailboxes", tags=["mailboxes"], response_model=out.Page[out.Mailbox])
    def mailboxes(request:Request,cursor:str|None=Query(None,max_length=512),limit:int=Query(50,ge=1,le=100)):
        return service.list_mailboxes(auth(request),cursor,limit)

    @app.post("/api/v1/mailboxes", status_code=202, tags=["mailboxes"], response_model=out.Operation)
    def create_mailbox(request:Request,data:CreateMailbox,idempotency_key:str|None=Header(None)):
        return service.create_mailbox(auth(request),data.model_dump(),idempotency_key)

    @app.get("/api/v1/mailboxes/{mailbox_id}", tags=["mailboxes"], response_model=out.Mailbox)
    def get_mailbox(request:Request,mailbox_id:str):
        return service.get_mailbox(auth(request),mailbox_id)

    @app.delete("/api/v1/mailboxes/{mailbox_id}",status_code=202,tags=["mailboxes"],response_model=out.Operation)
    def delete_mailbox(request:Request,mailbox_id:str):
        return service.queue_operation(auth(request),mailbox_id,"delete_mailbox")

    @app.post("/api/v1/mailboxes/{mailbox_id}/syncs",status_code=202,tags=["pickup"],response_model=out.Operation)
    def sync_mailbox(request:Request,mailbox_id:str):
        return service.queue_operation(auth(request),mailbox_id,"sync_mailbox")

    @app.get("/api/v1/mailboxes/{mailbox_id}/messages",tags=["messages"],response_model=out.MessagePage)
    def messages(request:Request,mailbox_id:str,cursor:str|None=Query(None,max_length=512),limit:int=Query(50,ge=1,le=100)):
        return service.list_messages(auth(request),mailbox_id,cursor,limit)

    @app.get("/api/v1/mailboxes/{mailbox_id}/messages/{message_id}",tags=["messages"],response_model=out.MessageDetail)
    def message(request:Request,mailbox_id:str,message_id:str):
        return service.get_message(auth(request),mailbox_id,message_id)

    @app.post("/api/v1/mailboxes/{mailbox_id}/messages/{message_id}/fetches",status_code=202,tags=["pickup"],response_model=out.Operation)
    def fetch_message(request:Request,mailbox_id:str,message_id:str):
        return service.queue_operation(auth(request),mailbox_id,"fetch_body",message_id)

    @app.get("/api/v1/operations/{operation_id}",tags=["operations"],response_model=out.Operation)
    def operation(request:Request,operation_id:str):
        return service.get_operation(auth(request),operation_id)

    @app.post("/api/v1/mailboxes/{mailbox_id}/waits",status_code=201,tags=["waits"],response_model=out.Wait)
    def create_wait(request:Request,mailbox_id:str,data:CreateWait,idempotency_key:str|None=Header(None)):
        return service.create_wait(auth(request),mailbox_id,data.model_dump(exclude_none=True),idempotency_key)

    @app.get("/api/v1/waits",tags=["waits"],response_model=out.Page[out.Wait])
    def list_waits(request:Request,cursor:str|None=Query(None,max_length=512),limit:int=Query(50,ge=1,le=100)):
        return service.list_waits(auth(request),cursor,limit)

    @app.get("/api/v1/waits/{wait_id}",tags=["waits"],response_model=out.Wait)
    async def wait_status(request:Request,wait_id:str,timeout_seconds:int=Query(0,ge=0,le=25)):
        principal=await asyncio.to_thread(auth,request)
        loop=asyncio.get_running_loop();deadline=loop.time()+timeout_seconds
        result=await asyncio.to_thread(service.get_wait,principal,wait_id)
        version=result["version"]
        while result["state"]=="waiting" and loop.time()<deadline and not await request.is_disconnected():
            await asyncio.sleep(min(.4,max(0,deadline-loop.time())))
            principal=await asyncio.to_thread(auth,request)
            result=await asyncio.to_thread(service.get_wait,principal,wait_id)
            if result["version"]!=version:
                break
        return result

    @app.delete("/api/v1/waits/{wait_id}",tags=["waits"],response_model=out.Wait)
    def cancel_wait(request:Request,wait_id:str):
        return service.get_wait(auth(request),wait_id,True)

    @app.post("/api/v1/mailboxes/{mailbox_id}/tokens",status_code=201,tags=["tokens"],response_model=out.TokenIssued)
    def create_token(request:Request,mailbox_id:str,data:CreateToken,idempotency_key:str|None=Header(None)):
        return service.issue_token(auth(request),mailbox_id,data.model_dump(),idempotency_key)

    @app.get("/api/v1/mailboxes/{mailbox_id}/tokens",tags=["tokens"],response_model=out.Page[out.Token])
    def list_tokens(request:Request,mailbox_id:str):
        principal=auth(request,"tokens:write",mailbox_id,True)
        with db.session() as session:
            service.box(session,principal,mailbox_id)
            rows=session.scalars(select(Credential).where(Credential.mailbox_id==mailbox_id,Credential.revoked.is_(False))).all()
            return {"items":[{"id":r.id,"mailbox_id":r.mailbox_id,"scopes":r.scopes,"created_at":iso(r.created_at),"expires_at":iso(r.expires_at),"revoked_at":None} for r in rows],"next_cursor":None,"has_more":False}

    @app.delete("/api/v1/mailboxes/{mailbox_id}/tokens/{token_id}",status_code=204,tags=["tokens"])
    def revoke_token(request:Request,mailbox_id:str,token_id:str):
        principal=auth(request,"tokens:write",mailbox_id,True)
        with db.session.begin() as session:
            workspace_lock(session,principal["workspace_id"])
            service.box(session,principal,mailbox_id)
            row=session.get(Credential,token_id)
            if not row or row.mailbox_id!=mailbox_id:
                raise DomainError(404,"resource_not_found","令牌不存在。")
            row.revoked=True

    def read_events(principal,mailbox_id,after):
        with db.session() as session:
            rows=select(Event).where(Event.workspace_id==principal["workspace_id"],Event.seq>after,Event.occurred_at>now()-timedelta(hours=24))
            if mailbox_id:
                rows=rows.where(Event.mailbox_id==mailbox_id)
            return session.scalars(rows.order_by(Event.seq).limit(100)).all()

    async def stream(request,mailbox_id,cursor,last_event_id):
        principal=await asyncio.to_thread(auth,request,"events:read",mailbox_id,not mailbox_id)
        if cursor and last_event_id and cursor!=last_event_id:
            raise DomainError(400,"cursor_conflict","事件游标不一致。")
        scope="events/"+(mailbox_id or "workspace")
        after=service.decode_cursor(principal,scope,cursor or last_event_id)
        if mailbox_id:
            with db.session() as session:
                service.box(session,principal,mailbox_id,active=True)
        if after is None:
            with db.session() as session:
                after=session.get(Workspace,principal["workspace_id"]).event_seq
        async def generate():
            position=after
            heartbeat=asyncio.get_running_loop().time()
            yield ": connected\n\n"
            while not await request.is_disconnected():
                # Revalidate revoked/expired credentials even on an already-open stream.
                try:
                    await asyncio.to_thread(auth,request,"events:read",mailbox_id,not mailbox_id)
                    if mailbox_id:
                        def validate_box():
                            with db.session() as session:
                                service.box(session,principal,mailbox_id,active=True)
                        await asyncio.to_thread(validate_box)
                except DomainError:
                    return
                events=await asyncio.to_thread(read_events,principal,mailbox_id,position)
                for event in events:
                    position=event.seq
                    data={"event_id":event.id,"type":event.type,"mailbox_id":event.mailbox_id,"message_id":event.message_id,
                          "wait_id":event.wait_id,"resource_version":event.resource_version,"occurred_at":iso(event.occurred_at)}
                    event_cursor=service.encode_cursor(principal["workspace_id"],scope,position)
                    yield "id: "+event_cursor+"\nevent: "+event.type+"\ndata: "+json.dumps(data)+"\n\n"
                if asyncio.get_running_loop().time()-heartbeat>=15:
                    heartbeat=asyncio.get_running_loop().time();yield ": heartbeat\n\n"
                await asyncio.sleep(.75)
        return StreamingResponse(generate(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.get("/api/v1/events",tags=["events"])
    async def events(request:Request,cursor:str|None=Query(None,max_length=512),last_event_id:str|None=Header(None,max_length=512)):
        return await stream(request,None,cursor,last_event_id)

    @app.get("/api/v1/mailboxes/{mailbox_id}/events",tags=["events"])
    async def mailbox_events(request:Request,mailbox_id:str,cursor:str|None=Query(None,max_length=512),last_event_id:str|None=Header(None,max_length=512)):
        return await stream(request,mailbox_id,cursor,last_event_id)

    dist=ROOT/"frontend/dist"
    if (dist/"assets").exists():
        app.mount("/assets",StaticFiles(directory=dist/"assets"),name="assets")
    @app.get("/",include_in_schema=False)
    def index():
        if not (dist/"index.html").exists():
            return JSONResponse({"message":"Build the console with npm run build in frontend/."},status_code=503)
        return FileResponse(dist/"index.html")
    return app


def main():
    import uvicorn
    settings=Settings.load()
    uvicorn.run(create_app(settings),host="127.0.0.1",port=settings.api_port,access_log=False)


if __name__=="__main__":
    main()
