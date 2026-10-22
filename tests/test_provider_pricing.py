import json
import unittest
from unittest.mock import patch

import httpx
from agent_core import provider_pricing as pricing
from test_providers import configuration, mock_stream


def silicon_html(items):
    payload = '0:{"pricingApiItems":' + json.dumps(items, ensure_ascii=False) + '}'
    return '<script>self.__next_f.push(' + json.dumps([1, payload]) + ')</script>'


def silicon_item(kind='input-tokens', amount='0.001', condition='', scenario=''):
    return {'playgroundName': 'vendor/model', 'realTimePriceCnyUnit': amount, 'unitZhCnName': 'K tokens',
            'componentCode': kind, 'coordinateDesc': condition, 'price_scenario': scenario}


class PricingTests(unittest.TestCase):
    def setUp(self): pricing._CACHE.clear()
    def tearDown(self): pricing._CACHE.clear()

    def test_siliconflow_preserves_tiers_cache_prices_and_units(self):
        html = silicon_html([silicon_item(condition='context: <= 32K', scenario='standard'),
                             silicon_item(amount='0.002', condition='context: > 32K', scenario='standard'),
                             silicon_item('cached-input-tokens', '0'), silicon_item('output-tokens', '0.004')])
        rows = pricing.parse_siliconflow(html)['vendor/model']
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], {'kind': 'input', 'amount': '1', 'unit': 'million_tokens', 'condition': 'context: <= 32K', 'scenario': 'standard'})
        self.assertEqual(rows[1]['amount'], '2'); self.assertEqual(rows[2]['amount'], '0')
        self.assertNotIn('inputPerMillion', json.dumps(rows))

    def test_deepseek_preserves_time_and_cache_conditions(self):
        html = '''<table><tr><th>模型</th><th>条件</th><th>deepseek-chat</th><th>deepseek-reasoner</th></tr>
          <tr><td rowspan="2">百万 tokens 输入</td><td>缓存未命中 高峰时段 08:30-00:30</td><td>2 元</td><td>4 元</td></tr>
          <tr><td>缓存命中 空闲时段 00:30-08:30</td><td>0.1 元</td><td>0.2 元</td></tr>
          <tr><td>百万 tokens 输出</td><td>高峰时段</td><td>8 元</td><td>16 元</td></tr></table>'''
        rows = pricing.parse_deepseek(html)['deepseek-chat']
        self.assertEqual([v['kind'] for v in rows], ['input', 'cached_input', 'output'])
        self.assertIn('08:30-00:30', rows[0]['condition']); self.assertIn('缓存命中', rows[1]['condition'])
        self.assertEqual(rows[2]['amount'], '8')

    def test_discovery_uses_official_source_without_credentials_and_does_not_mutate(self):
        config = configuration('unused.db'); config['providers'][0]['baseUrl'] = 'https://api.siliconflow.cn/v1'
        config['models'][0]['model'] = 'vendor/model'; original = json.dumps(config, sort_keys=True)
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), pricing.SOURCES['siliconflow'])
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(200, text=silicon_html([silicon_item()]))
        with patch('agent_core.provider_pricing.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            first = pricing.discover_pricing(config, 'k')
            first['models']['vendor/model'][0]['amount'] = 'changed'
            second = pricing.discover_pricing(config, 'k')
        self.assertEqual(len(calls), 1); self.assertEqual(second['status'], 'available')
        self.assertEqual(second['models']['vendor/model'][0]['amount'], '1')
        self.assertEqual(second['profiles'][0]['prices'][0]['amount'], '1')
        self.assertEqual(second['currency'], 'CNY'); self.assertIsNone(second['effectiveDate'])
        self.assertIn('fetchedAt', second); self.assertEqual(json.dumps(config, sort_keys=True), original)

    def test_failed_refresh_retains_stale_source_date_with_backoff(self):
        with patch.object(pricing, '_fetch', return_value=silicon_html([silicon_item()])):
            first = pricing.catalog('https://api.siliconflow.cn/v1')
        pricing._CACHE['siliconflow'] = (0, first)
        with patch.object(pricing, '_fetch', side_effect=ValueError('provider-private-response')) as fetch:
            stale = pricing.catalog('https://api.siliconflow.cn/v1')
            pricing.catalog('https://api.siliconflow.cn/v1')
        self.assertEqual(fetch.call_count, 1); self.assertEqual(stale['status'], 'stale')
        self.assertEqual(stale['fetchedAt'], first['fetchedAt']); self.assertEqual(stale['models'], first['models'])
        self.assertNotIn('provider-private-response', json.dumps(stale))

    def test_unknown_provider_and_changed_page_report_explicit_unavailability(self):
        with patch.object(pricing, '_fetch', return_value='<html>Format changed</html>') as fetch:
            unsupported = pricing.catalog('https://api.siliconflow.cn.attacker.test/v1')
            self.assertEqual(fetch.call_count, 0)
            unavailable = pricing.catalog('https://api.deepseek.com/v1')
        self.assertEqual(unsupported['status'], 'unsupported'); self.assertEqual(unavailable['status'], 'unavailable')
        self.assertEqual(unavailable['models'], {})
        self.assertEqual(pricing.parse_siliconflow(silicon_html([silicon_item(amount='1e-999999999')])), {})

    def test_fetch_rejects_oversize_and_cross_host_redirect(self):
        for response in (httpx.Response(200, content=b'x' * (pricing.PAGE_LIMIT + 1)),
                         httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})):
            with patch('agent_core.provider_pricing.httpx.stream', side_effect=lambda *a, **kw: mock_stream(lambda request: response, *a, **kw)):
                with self.assertRaises(ValueError): pricing._fetch(pricing.SOURCES['siliconflow'])


if __name__ == '__main__': unittest.main()
