"""Dependency-free MCP stdio server over the same authenticated Unified Mail API."""
import json
import os
from pathlib import Path
import sys
import uuid
from urllib.parse import quote, urlsplit
import httpx

VERSION='0.4.0'
PROTOCOLS=('2025-06-18','2024-11-05')


def tool(name,description,properties=None,required=None,read_only=True):
    return {'name':name,'description':description,'inputSchema':{'type':'object','properties':properties or {},
        'required':required or [],'additionalProperties':False},
        'annotations':{'readOnlyHint':read_only,'destructiveHint':False,'openWorldHint':True}}


STR={'type':'string','minLength':1,'maxLength':128}
MAILBOX={'provider_id':{'type':'string','maxLength':80},'name':{'type':'string','maxLength':120},
         'address_prefix':{'type':'string','maxLength':48},'ttl_seconds':{'type':'integer','minimum':300,'maximum':86400},
         'idempotency_key':{'type':'string','minLength':8,'maxLength':128}}
PREDICATE={'type':'object','properties':{x:{'type':'string','maxLength':253 if x=='from_domain' else 200}
    for x in ('from_domain','subject_contains','text_contains')},'additionalProperties':False}
TOOLS=[tool('list_providers','List configured mailbox sources and measured health.'),
       tool('provider_intelligence','Read measured latency and separate request/delivery success rates.'),
       tool('routing_preview','Preview eligible external sources in routing order.'),
       tool('create_mailbox','Queue mailbox creation. Omit provider_id for smart routing.',MAILBOX,read_only=False),
       tool('get_operation','Read an asynchronous mailbox operation.',{'operation_id':STR},['operation_id']),
       tool('get_mailbox','Read mailbox state and address.',{'mailbox_id':STR},['mailbox_id']),
       tool('list_messages','Read the first page of normalized messages.',{'mailbox_id':STR},['mailbox_id']),
       tool('get_message','Read normalized text and extraction candidates.',{'mailbox_id':STR,'message_id':STR},['mailbox_id','message_id']),
       tool('create_workflow','Durably create a mailbox and wait for email matching the specified conditions.',
            MAILBOX|{'timeout_seconds':{'type':'integer','minimum':5,'maximum':300},'predicate':PREDICATE},read_only=False),
       tool('get_task','Read a durable workflow or health probe.',{'task_id':STR},['task_id']),
       tool('cancel_task','Cancel a durable workflow. Its mailbox is retained.',{'task_id':STR},['task_id'],False)]
TOOLS[-1]['annotations']['destructiveHint']=True


def api_request(method,path,payload=None,key=None):
    base=os.environ.get('LINSEMAIL_API_URL','http://127.0.0.1:8100/api/v1').rstrip('/')
    parsed=urlsplit(base)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path!='/api/v1':
        raise ValueError('LINSEMAIL_API_URL must be a loopback /api/v1 HTTP URL')
    token=os.environ.get('LINSEMAIL_API_TOKEN')
    if not token:
        runtime=Path(os.environ.get('LINSEMAIL_RUNTIME',str(Path(__file__).resolve().parents[1]/'.runtime')))
        token=json.loads((runtime/'admin.local.json').read_text(encoding='utf-8'))['access_token']
    headers={'Authorization':'Bearer '+token}
    if method=='POST':
        headers['Idempotency-Key']=key or str(uuid.uuid4())
    with httpx.Client(timeout=20,trust_env=False) as client:
        response=client.request(method,base+path,json=payload,headers=headers)
    return response.json(),response.is_error


def call(name,args):
    definition=next((x for x in TOOLS if x['name']==name),None)
    if not definition or not isinstance(args,dict):
        raise ValueError('Unknown tool or invalid arguments')
    schema=definition['inputSchema']
    if set(args)-set(schema['properties']) or not set(schema['required'])<=set(args):
        raise ValueError('Unknown or missing tool arguments')
    for key,value in args.items():
        field=schema['properties'][key]
        kind=field['type']
        if (kind=='string' and (not isinstance(value,str) or len(value)<field.get('minLength',0) or len(value)>field.get('maxLength',10000))) or \
           (kind=='integer' and (type(value)!=int or not field.get('minimum',-1e20)<=value<=field.get('maximum',1e20))) or \
           (kind=='object' and not isinstance(value,dict)):
            raise ValueError('Invalid tool argument type or bounds')
    encode=lambda key:quote(args[key],safe='')
    routes={'list_providers':'/providers','provider_intelligence':'/providers/intelligence','routing_preview':'/routing/preview'}
    if name in routes:
        method,path,payload='GET',routes[name],None
    elif name in ('create_mailbox','create_workflow'):
        method,path,payload='POST','/mailboxes' if name=='create_mailbox' else '/workflows',{k:v for k,v in args.items() if k!='idempotency_key'}
    elif name=='get_operation':
        method,path,payload='GET','/operations/'+encode('operation_id'),None
    elif name in ('get_task','cancel_task'):
        method,path,payload='DELETE' if name=='cancel_task' else 'GET','/tasks/'+encode('task_id'),None
    else:
        path='/mailboxes/'+encode('mailbox_id')
        if name in ('list_messages','get_message'):
            path+='/messages'
        if name=='get_message':
            path+='/'+encode('message_id')
        method,payload='GET',None
    value,is_error=api_request(method,path,payload,args.get('idempotency_key'))
    return {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],'structuredContent':value,'isError':is_error}


def handle(request):
    if not isinstance(request,dict) or request.get('jsonrpc')!='2.0' or not isinstance(request.get('method'),str):
        return {'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid Request'}}
    if 'id' not in request:
        return None
    response={'jsonrpc':'2.0','id':request['id']}
    method,params=request['method'],request.get('params',{})
    try:
        if not isinstance(params,dict):
            raise ValueError('params must be an object')
        if method=='initialize':
            version=params.get('protocolVersion')
            result={'protocolVersion':version if version in PROTOCOLS else PROTOCOLS[0],
                    'capabilities':{'tools':{}},'serverInfo':{'name':'linsemail','version':VERSION}}
        elif method=='ping':
            result={}
        elif method=='tools/list':
            result={'tools':TOOLS}
        elif method=='tools/call':
            result=call(params.get('name'),params.get('arguments',{}))
        else:
            return response|{'error':{'code':-32601,'message':'Method not found'}}
        return response|{'result':result}
    except ValueError as exc:
        return response|{'error':{'code':-32602,'message':str(exc)}}
    except Exception:
        return response|{'result':{'isError':True,'content':[{'type':'text','text':'Mail API unavailable; check the local service and workspace token.'}]}}


def main():
    while True:
        line=sys.stdin.buffer.readline(1048577)
        if not line:
            return
        try:
            if len(line)>1048576:
                # Drain the oversized frame before accepting another request.
                while line and not line.endswith(b'\n'):
                    line=sys.stdin.buffer.readline(1048577)
                raise ValueError()
            response=handle(json.loads(line))
        except (ValueError,UnicodeError):
            response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}}
        if response is not None:
            sys.stdout.write(json.dumps(response,ensure_ascii=True)+'\n');sys.stdout.flush()


if __name__=='__main__':
    main()
