"""Measured provider health; delivery and HTTP success have separate denominators."""
from datetime import timedelta
import math
from sqlalchemy import select
from .models import ProviderObservation, now, uid
from .service import aware, iso, DomainError


def observe(session, provider_id, action, succeeded, latency_ms, error_code=None, identity=None):
    session.add(ProviderObservation(id=identity or uid('obs'), provider_id=provider_id,
        action=action, succeeded=succeeded, latency_ms=max(0, int(latency_ms)), error_code=error_code))


def snapshot(database, provider_ids):
    stamp = now()
    with database.session() as session:
        rows = session.scalars(select(ProviderObservation).where(
            ProviderObservation.occurred_at >= stamp-timedelta(hours=24))
            .order_by(ProviderObservation.occurred_at.desc()).limit(20000)).all()
    groups = {key: [] for key in provider_ids}
    for row in rows:
        if row.provider_id in groups and len(groups[row.provider_id]) < 1000:
            groups[row.provider_id].append(row)
    result = {}
    for key, values in groups.items():
        requests = [x for x in values if x.action != 'delivery']
        deliveries = [x for x in values if x.action == 'delivery']
        successes = sum(x.succeeded for x in requests)
        latencies = sorted(x.latency_ms for x in requests)
        recent = requests[:3]
        circuit = bool(len(recent)==3 and all(not x.succeeded for x in recent)
                       and aware(recent[0].occurred_at)>stamp-timedelta(minutes=5))
        p95 = latencies[max(0, math.ceil(len(latencies)*.95)-1)] if latencies else None
        rate = successes/len(requests) if requests else None
        score = (successes+2)/(len(requests)+4) * (sum(x.succeeded for x in deliveries)+2)/(len(deliveries)+4) / (1+(p95 or 0)/10000)
        result[key] = {'provider_id':key, 'window_hours':24, 'sample_limit':1000,
            'request_samples':len(requests), 'request_success_rate':round(rate,4) if rate is not None else None,
            'latency_p95_ms':p95, 'delivery_samples':len(deliveries),
            'delivery_success_rate':round(sum(x.succeeded for x in deliveries)/len(deliveries),4) if deliveries else None,
            'last_observed_at':iso(values[0].occurred_at) if values else None,
            'health':'cooldown' if circuit else 'unknown' if not requests else 'healthy' if rate>=.8 else 'degraded',
            'cooldown_until':iso(aware(recent[0].occurred_at)+timedelta(minutes=5)) if circuit else None,
            'routing_score':round(score,6)}
    return result


def rank(service, principal, address_prefix=''):
    providers = service.list_providers(principal)
    stats = snapshot(service.db, [x['id'] for x in providers])
    candidates = []
    for provider in providers:
        if provider['id']=='local' or provider['admission_state']!='enabled':
            continue
        if address_prefix and provider['capabilities'].get('custom_prefix')!='supported':
            continue
        metric = stats[provider['id']]
        if metric['health']=='cooldown':
            continue
        candidates.append({'provider_id':provider['id'], 'name':provider['name'], **metric,
                           'interface':provider['interface']})
    candidates.sort(key=lambda x:(-x['routing_score'], x['interface']!='official_api', x['provider_id']))
    return candidates


def choose(service, principal, address_prefix=''):
    candidates = rank(service, principal, address_prefix)
    if not candidates:
        raise DomainError(503, 'no_eligible_provider', '没有符合条件且未处于熔断期的外部来源；可显式选择本机测试箱。')
    return candidates[0]['provider_id']
