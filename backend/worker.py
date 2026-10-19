"""Independent bounded pickup worker. Run: python -m backend.worker."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import random
import signal
import threading

from sqlalchemy import delete, or_, select

from mailbox_clients import ProviderError
from .config import Settings
from .db import Database
from .models import Credential, Event, Idempotency, Inbound, Mailbox, Message, Operation, ProviderBudget, ProviderConfiguration, SyncJob, WaitJob, WorkerHeartbeat, Workspace, now, uid
from .providers import LegacyBridge, parse_time
from .service import aware, emit, problem, upsert_messages, workspace_lock


class Worker:
    def __init__(self, database):
        self.db = database
        self.bridge = LegacyBridge(database)
        self.owner = uid("worker")
        self.stop_event = threading.Event()

    def record_provider_check(self, session, provider_id, action, error):
        if provider_id == "local":
            return
        config = session.get(ProviderConfiguration, provider_id)
        if not config:
            config = ProviderConfiguration(provider_id=provider_id)
            session.add(config)
        check = dict(config.last_check or {})
        check.update(checked_at=now().isoformat(), status="failed" if error else "ok", action=action,
                     code=error["code"] if error else None)
        check[action] = error is None
        config.last_check = check
        config.updated_at = now()

    def maintain(self):
        stamp = now()
        with self.db.session.begin() as session:
            workspace = workspace_lock(session, "ws_local")
            heartbeat = session.get(WorkerHeartbeat, self.owner)
            if not heartbeat:
                session.add(WorkerHeartbeat(id=self.owner, updated_at=stamp))
            else:
                heartbeat.updated_at = stamp
            boxes = session.scalars(select(Mailbox).where(Mailbox.state.in_(["creating", "active"]), Mailbox.expires_at <= stamp)).all()
            for box in boxes:
                workspace=workspace_lock(session,box.workspace_id)
                box.state, box.sync_state = "expired", "idle"
                box.version += 1
                emit(session, workspace, "mailbox.expired", box.id, version=box.version)
            waits = session.scalars(select(WaitJob).join(Mailbox).where(WaitJob.state == "waiting",
                or_(WaitJob.deadline_at<=stamp,Mailbox.state=="expired"))).all()
            for wait in waits:
                workspace=workspace_lock(session,wait.workspace_id)
                box = session.get(Mailbox, wait.mailbox_id)
                wait.state = "expired" if box.state in ("expired", "deleting", "deleted") else "timed_out"
                wait.version += 1
                emit(session, workspace, "wait.terminated", box.id, wait_id=wait.id, version=wait.version)
            interrupted = session.scalars(select(Operation).where(Operation.state == "running", Operation.lease_until < stamp)).all()
            for operation in interrupted:
                operation.state = "uncertain" if operation.type == "create_mailbox" else "failed"
                operation.error = problem(503, "worker_interrupted", "执行中断，已保留原箱；创建结果需要核实。")
                operation.updated_at = stamp
                if operation.type == "create_mailbox":
                    session.get(Mailbox, operation.resource_id).state = "failed"
            cleanup = session.scalars(select(Mailbox).where(Mailbox.state == "expired", Mailbox.expires_at < stamp-timedelta(minutes=15))).all()
            for box in cleanup:
                session.execute(delete(Inbound).where(Inbound.mailbox_id == box.id))
                session.execute(delete(Message).where(Message.mailbox_id == box.id))
                box.vault_blob = None
            session.execute(delete(Event).where(Event.occurred_at<stamp-timedelta(hours=24)))
            session.execute(delete(Idempotency).where(Idempotency.created_at<stamp-timedelta(hours=24)))
            session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.updated_at<stamp-timedelta(hours=24)))

    def _budget(self, session, provider_id):
        budget = session.get(ProviderBudget, provider_id)
        if budget and aware(budget.next_allowed_at) > now():
            return False
        if budget:
            # Conservative operation-level spacing; each enabled adapter has bounded calls.
            interval = 0 if provider_id == "local" else 60 if provider_id == "destroy_network" else 5
            budget.next_allowed_at = now()+timedelta(seconds=interval)
        return True

    def claim_operation(self):
        with self.db.session.begin() as session:
            workspace_lock(session, "ws_local")
            operations = session.scalars(select(Operation).where(Operation.state == "queued", Operation.type != "sync_mailbox").order_by(Operation.created_at).with_for_update(skip_locked=True).limit(10)).all()
            for operation in operations:
                box = session.get(Mailbox, operation.resource_id)
                job = session.get(SyncJob, box.id)
                if job.lease_until and aware(job.lease_until) > now():
                    continue
                if operation.type != "delete_mailbox" and not self._budget(session, box.provider_id):
                    continue
                if operation.type != "delete_mailbox" and (box.state not in ("creating", "active") or aware(box.expires_at) <= now()):
                    operation.state="failed";operation.error=problem(410,"mailbox_expired","邮箱已不可用。")
                    continue
                job.lease_epoch += 1
                job.lease_owner, job.lease_until = self.owner, now()+timedelta(seconds=90)
                operation.state="running";operation.updated_at=now();operation.lease_until=job.lease_until
                message = session.get(Message, operation.message_id) if operation.message_id else None
                session.flush()
                return operation, box, message, job.lease_epoch
        return None

    def execute_operation(self, task):
        operation, box, message, epoch = task
        error, output, vault = None, None, box.vault_blob
        try:
            if operation.type == "create_mailbox":
                if box.provider_id == "local":
                    output={"address":"qa-"+uid("box")[4:16]+"@local.linsemail.test", "expires_at":None}
                else:
                    output, vault=self.bridge.run(box,"create")
            elif operation.type == "fetch_body":
                if box.provider_id == "local":
                    output={"upstream_identity":message.upstream_identity,"identity_strength":message.identity_strength,
                            "from_address":message.from_address,"from_domain":message.from_domain,"subject":message.subject,
                            "received_at":message.received_at,"text_body":message.text_body}
                else:
                    output, vault=self.bridge.run(box,"read",message.upstream_identity)
        except Exception as exc:
            error=problem(502,getattr(exc,"code","provider_unavailable"),"来源操作未完成，已保留原箱与错误状态。")
        with self.db.session.begin() as session:
            workspace=workspace_lock(session,box.workspace_id)
            current=session.get(Mailbox,box.id);job=session.get(SyncJob,box.id);op=session.get(Operation,operation.id)
            if job.lease_epoch!=epoch or job.lease_owner!=self.owner or not job.lease_until or aware(job.lease_until)<=now() or op.state!="running":
                return
            if operation.type!="delete_mailbox" and (current.state not in ("creating","active") or aware(current.expires_at)<=now()):
                error=problem(410,"mailbox_expired","执行期间邮箱已到期或停止。")
            if error:
                op.state="uncertain" if operation.type=="create_mailbox" else "failed"
                op.error=error;current.last_error=error
                if operation.type=="create_mailbox":
                    current.state="failed"
            elif operation.type=="create_mailbox":
                current.address=output["address"];current.vault_blob=vault;current.state="active"
                current.provider_expires_at=output["expires_at"]
                if output["expires_at"]:
                    current.expires_at=min(aware(current.expires_at),output["expires_at"])
                current.sync_state="scheduled";job.next_due_at=now();current.version+=1
                op.state="succeeded"
            elif operation.type=="fetch_body":
                if output.get("text_body") is None:
                    op.state="failed";op.error=problem(422,"body_unavailable","来源尚未返回可读取的正文。")
                else:
                    current.vault_blob=vault
                    upsert_messages(session,workspace,current,[output]);op.state="succeeded"
            elif operation.type=="delete_mailbox":
                session.execute(delete(Inbound).where(Inbound.mailbox_id==current.id))
                session.execute(delete(Message).where(Message.mailbox_id==current.id))
                for credential in session.scalars(select(Credential).where(Credential.mailbox_id==current.id)):
                    credential.revoked=True
                for wait in session.scalars(select(WaitJob).where(WaitJob.mailbox_id==current.id,WaitJob.state=="waiting")):
                    wait.state="cancelled";wait.version+=1
                    emit(session,workspace,"wait.terminated",current.id,wait_id=wait.id,version=wait.version)
                current.vault_blob=None;current.state="deleted";current.sync_state="idle";current.version+=1;op.state="succeeded"
            if operation.type in ("create_mailbox", "fetch_body"):
                self.record_provider_check(session, box.provider_id, "create" if operation.type=="create_mailbox" else "read_text", op.error)
            op.updated_at=now();op.lease_until=None
            job.lease_owner=None;job.lease_until=None
            emit(session,workspace,"operation.completed",current.id,version=current.version)

    def claim_sync(self):
        with self.db.session.begin() as session:
            workspace_lock(session,"ws_local")
            jobs=session.scalars(select(SyncJob).join(Mailbox).where(Mailbox.state=="active",Mailbox.expires_at>now(),
                Mailbox.sync_state!="blocked",SyncJob.next_due_at<=now(),or_(SyncJob.lease_until.is_(None),SyncJob.lease_until<now()))
                .order_by(SyncJob.next_due_at).with_for_update(skip_locked=True).limit(10)).all()
            for job in jobs:
                box=session.get(Mailbox,job.mailbox_id)
                if not self._budget(session,box.provider_id):
                    continue
                job.lease_epoch+=1;job.lease_owner=self.owner;job.lease_until=now()+timedelta(seconds=90)
                box.sync_state="syncing"
                session.flush()
                return box,job.lease_epoch
        return None

    def execute_sync(self, task):
        box,epoch=task
        error,items,vault=None,[],box.vault_blob
        try:
            if box.provider_id=="local":
                with self.db.session() as session:
                    for inbound in session.scalars(select(Inbound).where(Inbound.mailbox_id==box.id)):
                        item=dict(inbound.payload);item["received_at"]=parse_time(item.get("received_at"));items.append(item)
            else:
                items,vault=self.bridge.run(box,"list")
        except Exception as exc:
            error=problem(429 if getattr(exc,"status",None)==429 else 502,getattr(exc,"code","provider_unavailable"),"本次取件未成功；已有邮件保留。")
        with self.db.session.begin() as session:
            workspace=workspace_lock(session,box.workspace_id)
            current=session.get(Mailbox,box.id);job=session.get(SyncJob,box.id)
            if job.lease_epoch!=epoch or job.lease_owner!=self.owner or not job.lease_until or aware(job.lease_until)<=now():
                return
            if current.state!="active" or aware(current.expires_at)<=now():
                job.lease_owner=None;job.lease_until=None
                return
            if error:
                current.last_error=error;job.failures+=1
                if error["code"]=="mailbox_expired":
                    current.state="expired";current.expires_at=min(aware(current.expires_at),now());current.sync_state="idle";current.version+=1
                    emit(session,workspace,"mailbox.expired",current.id,version=current.version)
                else:
                    current.sync_state="blocked" if error["code"] in ("mailbox_identity_changed","mailbox_expired_or_changed","unexpected_response") else "backoff"
                job.next_due_at=now()+timedelta(seconds=min(300,15*2**min(job.failures,4))+random.uniform(0,3))
            else:
                current.vault_blob=vault
                upsert_messages(session,workspace,current,items)
                current.last_success_at=now();current.last_error=None;current.sync_state="idle";current.version+=1;job.failures=0
                waiting=session.scalar(select(WaitJob.id).where(WaitJob.mailbox_id==current.id,WaitJob.state=="waiting").limit(1))
                job.next_due_at=now()+timedelta(seconds=2 if box.provider_id=="local" else 10 if waiting else 30)
            self.record_provider_check(session, box.provider_id, "list", error)
            for operation in session.scalars(select(Operation).where(Operation.resource_id==box.id,Operation.type=="sync_mailbox",Operation.state.in_(["queued","running"]))):
                operation.state="failed" if error else "succeeded";operation.error=error;operation.updated_at=now()
            job.lease_owner=None;job.lease_until=None

    def tick(self):
        self.maintain()
        task=self.claim_operation()
        if task:
            self.execute_operation(task)
        sync=self.claim_sync()
        if sync:
            self.execute_sync(sync)

    def run(self):
        print(json.dumps({"service":"linsemail-worker","status":"ready","smtp_port":self.db.settings.smtp_port}),flush=True)
        # Keep scheduling/expiry/heartbeats responsive while an external source
        # waits on its network timeout. Do not claim leases into an unbounded queue.
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix="pickup") as pool:
            active=set()
            while not self.stop_event.is_set():
                try:
                    for future in list(active):
                        if future.done():
                            active.remove(future)
                            future.result()
                    self.maintain()
                    for claim,execute in ((self.claim_operation,self.execute_operation),(self.claim_sync,self.execute_sync)):
                        if len(active)>=4:
                            break
                        task=claim()
                        if task:
                            active.add(pool.submit(execute,task))
                except Exception as error:
                    # No response bodies, addresses or connection URLs in logs.
                    print(json.dumps({"status":"tick_error","type":type(error).__name__}),flush=True)
                self.stop_event.wait(0.75)


def main():
    from .smtp import start_smtp
    settings=Settings.load();database=Database(settings)
    worker=Worker(database)
    smtp=start_smtp(database)
    for signum in (signal.SIGINT,signal.SIGTERM):
        signal.signal(signum,lambda *_:worker.stop_event.set())
    try:
        worker.run()
    finally:
        smtp.stop();database.close()


if __name__=="__main__":
    main()
