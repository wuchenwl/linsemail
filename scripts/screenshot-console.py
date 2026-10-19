"""Render the real interface against synthetic API fixtures; never call a mail provider."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.registry import PROVIDERS
from playwright.sync_api import sync_playwright


def fixtures():
    clock = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda seconds=0: (clock + timedelta(seconds=seconds)).isoformat()
    sync = {'state': 'scheduled', 'last_success_at': stamp(-4), 'freshness': 'fresh', 'last_error': None, 'list_completeness': 'complete'}
    boxes = [{'id': 'mb_demo_product', 'provider_id': 'local', 'address': 'qa-product@local.linsemail.test',
              'name': 'Product QA', 'state': 'active', 'mailbox_access': 'owned_domain', 'labels': ['staging'],
              'created_at': stamp(-120), 'expires_at': stamp(3500), 'provider_expires_at': None,
              'last_ingest_seq': 2, 'version': 3, 'message_count': 2, 'sync': sync},
             {'id': 'mb_demo_webhooks', 'provider_id': 'local', 'address': 'qa-webhooks@local.linsemail.test',
              'name': 'Webhook checks', 'state': 'active', 'mailbox_access': 'owned_domain', 'labels': [],
              'created_at': stamp(-60), 'expires_at': stamp(21000), 'provider_expires_at': None,
              'last_ingest_seq': 0, 'version': 2, 'message_count': 0, 'sync': sync}]
    body = 'Your verification code is 482731.\n\nUse this example to explore the message reader and traceable extraction.\nThis is synthetic demonstration content. No external delivery is implied.\n\nThe Linsemail team'
    start = body.index('482731')
    messages = [{'id': 'msg_demo_code', 'mailbox_id': boxes[0]['id'], 'from_address': 'Product QA <hello@example.com>',
                 'from_domain': 'example.com', 'subject': 'Your workspace verification code', 'received_at': stamp(-20),
                 'first_seen_at': stamp(-18), 'ingest_seq': 2, 'body_state': 'ready', 'identity_strength': 'strong', 'version': 1},
                {'id': 'msg_demo_receipt', 'mailbox_id': boxes[0]['id'], 'from_address': 'Build system <builds@example.org>',
                 'from_domain': 'example.org', 'subject': 'Staging delivery check completed', 'received_at': stamp(-80),
                 'first_seen_at': stamp(-78), 'ingest_seq': 1, 'body_state': 'ready', 'identity_strength': 'strong', 'version': 1}]
    detail = messages[0] | {'text_body': body, 'sanitized_html': None, 'sanitization_version': None,
             'extractions': [{'type': 'otp', 'value': '482731', 'source_field': 'text_body',
                             'source_span': {'start': start, 'end': start + 6}, 'rule_id': 'contextual-numeric-v1', 'rule_strength': 'contextual'}],
             'extraction_state': 'unique'}
    waits = [{'id': 'wait_demo_match', 'mailbox_id': boxes[0]['id'], 'state': 'matched', 'after_seq': 1,
              'predicate': {'from_domain': 'example.com', 'subject_contains': 'verification'},
              'matched_message_id': messages[0]['id'], 'version': 2, 'created_at': stamp(-45), 'deadline_at': stamp(75), 'error': None},
             {'id': 'wait_demo_pending', 'mailbox_id': boxes[1]['id'], 'state': 'waiting', 'after_seq': 0,
              'predicate': {'subject_contains': 'deployment'}, 'matched_message_id': None, 'version': 1,
              'created_at': stamp(-10), 'deadline_at': stamp(290), 'error': None}]
    return boxes, messages, detail, waits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8100')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/screenshots')
    parser.add_argument('--browser-path')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    boxes, messages, detail, waits = fixtures()
    page_of = lambda items: {'items': items, 'next_cursor': None, 'has_more': False}
    provider_page = page_of(PROVIDERS) | {'summary': {'external_total': 127, 'adapter_total': 97, 'ready': 61, 'credential_required': 36, 'pending': 30, 'local': 1}}
    report = {'data': 'Synthetic API fixtures rendered by the real frontend; no provider requests.', 'screenshots': [], 'layouts': [], 'checks': [], 'errors': []}
    with sync_playwright() as playwright:
        launch = {'headless': True}
        if args.browser_path:
            launch['executable_path'] = args.browser_path
        browser = playwright.chromium.launch(**launch)
        for locale in ('en', 'zh-CN'):
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
            context.add_init_script("if (!localStorage.getItem('linsemail-language')) localStorage.setItem('linsemail-language'," + json.dumps(locale) + "); localStorage.setItem('linsemail-theme','light');")
            signed_in = [False]
            def route_api(route):
                path = urlsplit(route.request.url).path.removeprefix('/api/v1')
                session = {'workspace_id': 'ws_demo', 'workspace_name': '本地工作区', 'csrf_token': 'SYNTHETIC_NOT_A_TOKEN', 'local_mode': True}
                if path == '/session':
                    if route.request.method == 'POST': signed_in[0] = True
                    if not signed_in[0]:
                        return route.fulfill(status=401, json={'code': 'unauthorized', 'detail': 'Demo session not started'})
                    return route.fulfill(json=session)
                if path.endswith('/events') or path == '/events':
                    return route.fulfill(content_type='text/event-stream', body='retry: 60000\n\n')
                responses = {'/health': {'status': 'ok', 'database': 'postgresql', 'worker_online': True, 'smtp_port': 1025},
                             '/providers': provider_page, '/mailboxes': page_of(boxes), '/waits': page_of(waits),
                             '/mailboxes/mb_demo_product/messages': page_of(messages) | {'mailbox_id': boxes[0]['id'], 'sync': boxes[0]['sync']},
                             '/mailboxes/mb_demo_webhooks/messages': page_of([]) | {'mailbox_id': boxes[1]['id'], 'sync': boxes[1]['sync']},
                             '/mailboxes/mb_demo_product/messages/msg_demo_code': detail}
                if route.request.method not in ('GET', 'HEAD'):
                    report['errors'].append('Unexpected demo write: ' + route.request.method + ' ' + path)
                    return route.fulfill(status=409, json={'code': 'demo_read_only', 'detail': 'Synthetic screenshot session'})
                if path not in responses:
                    report['errors'].append('Unmapped demo read: ' + path)
                    return route.fulfill(status=404, json={'code': 'demo_not_found'})
                return route.fulfill(json=responses[path])
            context.route('**/api/v1/**', route_api)
            page = context.new_page()
            page.on('pageerror', lambda error: report['errors'].append(str(error)))
            page.goto(args.base_url, wait_until='domcontentloaded')
            names = {'login': 'Enter workspace' if locale == 'en' else '进入本地工作区',
                     'inbox': 'Inbox' if locale == 'en' else '收件工作台', 'waits': 'Wait tasks' if locale == 'en' else '取件任务',
                     'providers': 'Providers' if locale == 'en' else '邮箱来源', 'api': 'API',
                     'new': 'New mailbox' if locale == 'en' else '新建邮箱', 'cancel': 'Cancel' if locale == 'en' else '取消'}
            def navigate(view):
                button = page.get_by_role('navigation').get_by_role('button', name=re.compile('^'+re.escape(names[view])+r'(?:\s*\d+)?$'))
                assert names[view] in button.inner_text()
                button.click()
            def shot(view, theme='light'):
                repository = page.get_by_role('link', name='GitHub · wuchenwl/linsemail', exact=True)
                assert repository.count() == 2, (view, repository.count())
                for link in repository.all():
                    assert link.is_visible()
                    assert link.get_attribute('href') == 'https://github.com/wuchenwl/linsemail'
                    assert link.get_attribute('target') == '_blank'
                    assert set(link.get_attribute('rel').split()) >= {'noopener', 'noreferrer'}
                page.evaluate("document.documentElement.dataset.theme = " + json.dumps(theme))
                page.evaluate('document.fonts.ready')
                page.wait_for_timeout(100)
                if locale == 'en':
                    text = page.locator('body').inner_text()
                    for known in ('Language / 语言', '简体中文', '临时邮箱.net'):
                        text = text.replace(known, '')
                    residual = sorted(set(re.findall(r'[\u3400-\u9fff]+', text)))
                    assert not residual, (view, residual)
                name = view + '-' + locale + '-' + theme + '.png'
                page.screenshot(path=str(args.output / name), animations='disabled')
                data = (args.output / name).read_bytes()
                report['screenshots'].append({'file': name, 'locale': locale, 'view': view, 'theme': theme,
                    'width': page.viewport_size['width'], 'height': page.viewport_size['height'],
                    'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            page.get_by_role('button', name=names['login'], exact=True).wait_for()
            assert page.locator('html').get_attribute('lang') == locale
            shot('login')
            page.get_by_role('button', name=names['login'], exact=True).click()
            page.locator('.extraction .otp').wait_for()
            assert page.locator('.extraction .otp').inner_text() == '482731'
            shot('inbox')
            shot('inbox', 'dark')
            page.evaluate("document.documentElement.dataset.theme = 'light'")
            for view in ('waits', 'providers', 'api'):
                navigate(view)
                page.locator({'waits': '.task-row', 'providers': '.provider-card', 'api': '.api-intro'}[view]).first.wait_for()
                shot(view)
            page.get_by_role('button', name=names['new'], exact=True).click()
            page.locator('dialog select').first.select_option('grabmail')
            page.locator('input[name="address_prefix"]').fill('qa-')
            assert page.locator('input[name="address_prefix"]').evaluate('e=>e.checkValidity()')
            shot('create')
            page.get_by_role('button', name=names['cancel'], exact=True).click()
            navigate('inbox')
            for width in (320, 390, 768, 1024, 1440, 1920):
                page.set_viewport_size({'width': width, 'height': 1000})
                for view in ('inbox', 'waits', 'providers', 'api'):
                    navigate(view)
                    measure = page.evaluate('({width: innerWidth, scroll: document.documentElement.scrollWidth})')
                    assert measure['scroll'] <= width, (locale, view, measure)
                    report['layouts'].append({'locale': locale, 'view': view, **measure})
            navigate('inbox')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('.mailbox-item').first.click()
            page.locator('.message-item').first.click()
            shot('mobile', 'dark')
            # Switch in place and confirm state and user content survive.
            page.get_by_label('Language / 语言').select_option('zh-CN' if locale == 'en' else 'en')
            assert page.locator('.extraction .otp').inner_text() == '482731'
            assert 'synthetic demonstration content' in page.locator('.mail-body').inner_text()
            assert page.evaluate("localStorage.getItem('linsemail-language')") != locale
            page.reload(wait_until='domcontentloaded')
            page.get_by_role('navigation').wait_for()
            assert page.locator('html').get_attribute('lang') == ('zh-CN' if locale == 'en' else 'en')
            report['checks'].append({'locale': locale, 'switching_preserves_content': True, 'persistence': True})
            context.close()
        browser.close()
    assert len(report['screenshots']) == 16 and not report['errors'], report['errors']
    (args.output / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'screenshots': len(report['screenshots']), 'layout_checks': len(report['layouts']), 'errors': report['errors']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
