"""Runtime OpenAPI schemas, also used to validate outgoing API responses."""
from typing import Generic, Literal, TypeVar
from pydantic import BaseModel

T=TypeVar("T")

class Page(BaseModel,Generic[T]):
    items:list[T]
    next_cursor:str|None
    has_more:bool

class Problem(BaseModel):
    type:str
    title:str
    detail:str
    status:int
    code:str
    request_id:str

class Health(BaseModel):
    status:Literal["ok"]
    database:str
    worker_online:bool
    smtp_port:int

class Session(BaseModel):
    workspace_id:str
    workspace_name:str
    csrf_token:str|None
    local_mode:bool

class Sync(BaseModel):
    state:str
    last_success_at:str|None
    freshness:Literal["fresh","stale","unknown"]
    last_error:Problem|None
    list_completeness:str

class Mailbox(BaseModel):
    id:str
    provider_id:str
    address:str|None
    name:str
    state:Literal["creating","active","failed","expired","deleting","deleted"]
    mailbox_access:str
    labels:list[str]
    created_at:str
    expires_at:str
    provider_expires_at:str|None
    last_ingest_seq:int
    version:int
    message_count:int
    sync:Sync

class Operation(BaseModel):
    id:str
    type:Literal["create_mailbox","sync_mailbox","fetch_body","delete_mailbox"]
    state:Literal["queued","running","succeeded","failed","uncertain"]
    resource_id:str
    error:Problem|None
    created_at:str
    updated_at:str

class Message(BaseModel):
    id:str
    mailbox_id:str
    from_address:str|None
    from_domain:str|None
    subject:str|None
    ingest_seq:int
    body_state:Literal["metadata_only","ready"]
    identity_strength:Literal["strong","weak"]
    version:int
    received_at:str|None
    first_seen_at:str

class Span(BaseModel):
    start:int
    end:int

class Extraction(BaseModel):
    type:Literal["otp","magic_link"]
    value:str
    source_field:Literal["text_body"]
    source_span:Span
    rule_id:str
    rule_strength:str

class MessageDetail(Message):
    text_body:str|None
    sanitized_html:None=None
    sanitization_version:None=None
    extractions:list[Extraction]
    extraction_state:Literal["unavailable","none","unique","ambiguous"]

class MessagePage(Page[Message]):
    mailbox_id:str
    sync:Sync

class Wait(BaseModel):
    id:str
    mailbox_id:str
    state:Literal["waiting","matched","cancelled","timed_out","expired"]
    after_seq:int
    predicate:dict[str,str]
    matched_message_id:str|None
    version:int
    error:Problem|None
    created_at:str
    deadline_at:str

class Token(BaseModel):
    id:str
    mailbox_id:str
    scopes:list[str]
    created_at:str
    expires_at:str|None
    revoked_at:str|None

class TokenIssued(Token):
    access_token:str
    token_type:Literal["Bearer"]

class Provider(BaseModel):
    intelligence:dict|None=None
    id:str
    name:str
    mailbox_access:str
    description:str
    source_kind:str
    admission_state:str
    adapter_version:str|None
    transport:list[str]
    capabilities:dict[str,str]
    homepage:str|None
    blocked_reason:str|None
    adapter_implemented:bool
    configured:bool
    credential_fields:list[dict[str,str|bool]]
    last_check:dict|None
    documentation:list[str]
    interface:str

class ProviderPage(Page[Provider]):
    summary:dict[str,int]


class Task(BaseModel):
    id:str
    kind:Literal['workflow','probe']
    state:Literal['queued','running','waiting','succeeded','matched','failed','timed_out','cancelled']
    result:dict
    error:Problem|None
    attempts:int
    created_at:str
    updated_at:str
    deadline_at:str


class Webhook(BaseModel):
    id:str
    url:str
    events:list[str]
    active:bool
    created_at:str


class WebhookIssued(Webhook):
    secret:str
