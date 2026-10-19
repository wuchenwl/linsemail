"""Loopback-only SMTP input for owned local test mailboxes; never relays messages."""
import asyncio
from email import policy
from email.parser import BytesParser
import hashlib

from aiosmtpd.controller import Controller
from sqlalchemy import select

from .models import Inbound, Mailbox, SyncJob, now, uid
from .providers import normalize
from .service import aware, iso, workspace_lock


class LocalHandler:
    def __init__(self,database):
        self.db=database

    async def handle_RCPT(self,server,session,envelope,address,rcpt_options):
        with self.db.session() as dbsession:
            box=dbsession.scalar(select(Mailbox).where(Mailbox.address==address.lower(),Mailbox.provider_id=="local",Mailbox.state=="active"))
            if not box or aware(box.expires_at)<=now():
                return "550 Unknown or expired local mailbox"
        envelope.rcpt_tos.append(address.lower())
        return "250 Accepted"

    async def handle_DATA(self,server,session,envelope):
        try:
            message=BytesParser(policy=policy.default).parsebytes(envelope.original_content)
            text,html=None,None
            count=0
            for part in message.walk():
                count+=1
                if count>50:
                    return "552 Too many MIME parts"
                if part.get_content_disposition()=="attachment" or part.is_multipart():
                    continue
                if part.get_content_type()=="text/plain" and text is None:
                    text=part.get_content()
                elif part.get_content_type()=="text/html" and html is None:
                    html=part.get_content()
            identity=hashlib.sha256(envelope.original_content).hexdigest()
            item=normalize({"id":identity,"from":str(message.get("From","")),"subject":str(message.get("Subject","")),"text":text,"html":html,"received_at":now().isoformat()})
            item["received_at"]=iso(item["received_at"])
            with self.db.session.begin() as dbsession:
                workspace_lock(dbsession,"ws_local")
                for address in envelope.rcpt_tos:
                    box=dbsession.scalar(select(Mailbox).where(Mailbox.address==address,Mailbox.state=="active"))
                    if not box or aware(box.expires_at)<=now():
                        return "550 Mailbox expired during delivery"
                    existing=dbsession.scalar(select(Inbound).where(Inbound.mailbox_id==box.id,Inbound.identity==identity))
                    if not existing:
                        dbsession.add(Inbound(id=uid("in"),mailbox_id=box.id,identity=identity,payload=item))
                    dbsession.get(SyncJob,box.id).next_due_at=now()
            return "250 Message persisted"
        except Exception:
            return "451 Temporary local processing error"


def start_smtp(database):
    controller=Controller(LocalHandler(database),hostname="127.0.0.1",port=database.settings.smtp_port,data_size_limit=1_000_000,enable_SMTPUTF8=True)
    controller.start()
    return controller
