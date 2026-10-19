"""Recoverable composite tasks reuse the existing idempotent mailbox and wait APIs."""
from datetime import timedelta
from sqlalchemy import or_, select
from .db import ALL_SCOPES
from .models import AutomationTask, Mailbox, now, uid
from .service import DomainError, Service, aware, iso, problem, workspace_lock

TERMINAL=frozenset({'succeeded','matched','failed','timed_out','cancelled'})


def task_dict(row):
    return {k:getattr(row,k) for k in ('id','kind','state','result','error','attempts')} | {
        'created_at':iso(row.created_at),'updated_at':iso(row.updated_at),'deadline_at':iso(row.deadline_at)}


class Automation:
    def __init__(self,database,owner=None):
        self.db=database;self.service=Service(database);self.owner=owner or uid('agent_worker')

    def create(self,principal,payload,key,kind='workflow'):
        self.service.require(principal,'mailbox:write',workspace_only=True)
        if kind=='probe':
            provider=self.service.get_provider(principal,payload['provider_id'])
            if provider['admission_state']!='enabled':
                raise DomainError(409,'provider_unavailable','该来源尚未配置或没有可执行适配器。')
        def create(session,workspace):
            count=len(session.scalars(select(AutomationTask.id).where(AutomationTask.workspace_id==workspace.id,
                AutomationTask.state.not_in(TERMINAL))).all())
            if count>=100:
                raise DomainError(429,'task_limit','工作区最多保留 100 个进行中的自动化任务。')
            row=AutomationTask(id=uid('task'),workspace_id=workspace.id,kind=kind,payload=payload,
                deadline_at=now()+timedelta(seconds=payload.get('timeout_seconds',180)),result={})
            session.add(row);session.flush();return task_dict(row)
        return self.service.idempotent(principal,'POST/'+kind,key,payload,create)

    def get(self,principal,task_id=None,cancel=False):
        self.service.require(principal,'mailbox:write' if cancel else 'mailbox:read',workspace_only=True)
        with self.db.session.begin() as session:
            if cancel:
                workspace_lock(session,principal['workspace_id'])
            if task_id is None:
                return {'items':[task_dict(row) for row in session.scalars(select(AutomationTask).where(
                    AutomationTask.workspace_id==principal['workspace_id']).order_by(AutomationTask.created_at.desc()).limit(100))]}
            query=select(AutomationTask).where(AutomationTask.id==task_id,AutomationTask.workspace_id==principal['workspace_id'])
            row=session.scalar(query.with_for_update() if cancel else query)
            if not row:
                raise DomainError(404,'resource_not_found','自动化任务不存在。')
            if cancel and row.state not in TERMINAL:
                row.state='cancelled';row.updated_at=now();row.lease_epoch+=1;row.lease_until=None
                wait_id=row.result.get('wait_id')
            else:
                wait_id=None
            result=task_dict(row)
        if wait_id:
            self.cancel_wait(principal,wait_id)
        return result

    def cancel_wait(self,principal,wait_id):
        try:
            self.service.get_wait(principal,wait_id,cancel=True)
        except DomainError as exc:
            if exc.code!='wait_already_terminal':
                raise

    def claim(self):
        with self.db.session.begin() as session:
            row=session.scalar(select(AutomationTask).where(AutomationTask.state.not_in(TERMINAL),
                AutomationTask.next_due_at<=now(),or_(AutomationTask.lease_until.is_(None),AutomationTask.lease_until<now()))
                .order_by(AutomationTask.next_due_at).with_for_update(skip_locked=True).limit(1))
            if not row:
                return None
            row.state='running';row.lease_owner=self.owner;row.lease_epoch+=1;row.lease_until=now()+timedelta(seconds=30)
            session.flush();return row

    @staticmethod
    def principal(workspace_id):
        return {'workspace_id':workspace_id,'kind':'workspace','mailbox_id':None,'scopes':ALL_SCOPES}

    def execute(self,row):
        with self.db.session() as session:
            current=session.get(AutomationTask,row.id)
            if current.state in TERMINAL or current.lease_epoch!=row.lease_epoch or current.lease_owner!=self.owner or not current.lease_until or aware(current.lease_until)<=now():
                return
        state,result,error='waiting',dict(row.result),None
        principal=self.principal(row.workspace_id)
        try:
            if aware(row.deadline_at)<=now():
                state='timed_out'
                if result.get('wait_id'):
                    waiting=self.service.get_wait(principal,result['wait_id'])
                    if waiting['state']=='matched':
                        state='matched';result['matched_message_id']=waiting['matched_message_id']
            else:
                if not result.get('operation_id'):
                    payload={k:row.payload[k] for k in ('provider_id','name','address_prefix','ttl_seconds','labels') if k in row.payload}
                    if row.kind=='probe':
                        payload.update(name='Provider health probe',ttl_seconds=300)
                    operation=self.service.create_mailbox(principal,payload,row.id+':create')
                    result.update(operation_id=operation['id'],mailbox_id=operation['resource_id'])
                operation=self.service.get_operation(principal,result['operation_id'])
                if operation['state'] in ('failed','uncertain'):
                    state='failed';error=operation['error']
                elif operation['state']=='succeeded':
                    box=self.service.get_mailbox(principal,result['mailbox_id'])
                    result.update(address=box['address'],provider_id=box['provider_id'])
                    if row.kind=='probe':
                        if box['sync']['last_success_at']:
                            state='succeeded';result.update(create=True,list=True,delivery_tested=False)
                    else:
                        if not result.get('wait_id'):
                            waiting=self.service.create_wait(principal,box['id'],{'after_seq':0,
                                'timeout_seconds':row.payload.get('timeout_seconds',120),'predicate':row.payload.get('predicate',{})},row.id+':wait')
                            result['wait_id']=waiting['id']
                        waiting=self.service.get_wait(principal,result['wait_id'])
                        if waiting['state']=='matched':
                            state='matched';result['matched_message_id']=waiting['matched_message_id']
                        elif waiting['state']!='waiting':
                            state='timed_out' if waiting['state'] in ('timed_out','expired') else 'cancelled'
        except DomainError as exc:
            state='failed';error=problem(exc.status,exc.code,exc.detail)
        except Exception:
            error=problem(503,'task_retry','任务将在租约恢复后重试。')
        with self.db.session.begin() as session:
            current=session.scalar(select(AutomationTask).where(AutomationTask.id==row.id).with_for_update())
            if current.lease_epoch!=row.lease_epoch or current.lease_owner!=self.owner or current.state in TERMINAL or not current.lease_until or aware(current.lease_until)<=now():
                return
            if error and state=='waiting':
                current.attempts+=1
                if current.attempts>=5:
                    state='failed'
            current.state=state;current.result=result;current.error=error;current.updated_at=now()
            current.next_due_at=now()+timedelta(seconds=2 if not error else min(60,2**current.attempts))
            current.lease_owner=None;current.lease_until=None
        if state in TERMINAL:
            if result.get('wait_id') and state!='matched':
                if state=='timed_out':
                    from .models import WaitJob
                    from .service import emit
                    with self.db.session.begin() as session:
                        workspace=workspace_lock(session,row.workspace_id)
                        waiting=session.get(WaitJob,result['wait_id'])
                        if waiting.state=='waiting':
                            waiting.state='timed_out';waiting.version+=1
                            emit(session,workspace,'wait.terminated',waiting.mailbox_id,wait_id=waiting.id,version=waiting.version)
                else:
                    self.cancel_wait(principal,result['wait_id'])
            if row.kind=='probe' and result.get('mailbox_id'):
                self.service.queue_operation(principal,result['mailbox_id'],'delete_mailbox')

    def schedule_probe(self):
        if not self.db.settings.provider_probes:
            return
        principal=self.principal('ws_local')
        providers=[x for x in self.service.list_providers(principal) if x['id']!='local' and x['admission_state']=='enabled']
        if not providers:
            return
        with self.db.session.begin() as session:
            workspace_lock(session,'ws_local')
            rows=session.scalars(select(AutomationTask).where(AutomationTask.kind=='probe').order_by(AutomationTask.created_at.desc()).limit(1000)).all()
            if rows and (now()-aware(rows[0].created_at)).total_seconds()<self.db.settings.provider_probe_interval:
                return
            last={}
            for row in rows:
                last.setdefault(row.payload['provider_id'],aware(row.created_at))
            selected=min(providers,key=lambda x:(last.get(x['id'],now()-timedelta(days=365)),x['id']))
            session.add(AutomationTask(id=uid('task'),workspace_id='ws_local',kind='probe',
                payload={'provider_id':selected['id'],'timeout_seconds':180},result={},deadline_at=now()+timedelta(seconds=180)))
