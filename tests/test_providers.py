import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager

import httpx
from agent_core.config import validate_config, model_profile
from agent_core.providers import discover_models, public_hierarchy, query_provider_billing, record_usage, usage_summary
from agent_core.security import CoreError
from agent_core.store import Store


def configuration(path):
    return {'database': str(path), 'services': [{'id': 'app', 'tenantId': 'tenant', 'tokenSha256': hashlib.sha256(b'app').hexdigest(),
             'modelProfiles': ['chat'], 'defaultModelProfile': 'chat'}],
            'providers': [{'id': 'p', 'baseUrl': 'https://provider.example/v1', 'providerType': 'OPENAI'}],
            'providerAccounts': [{'id': 'a', 'providerId': 'p'}],
            'providerKeys': [{'id': 'k', 'accountId': 'a', 'apiKeyEnv': 'TEST_GATEWAY_KEY'}],
            'models': [{'id': 'chat', 'keyId': 'k', 'model': 'upstream-model'}]}


@contextmanager
def mock_stream(handler, *args, **kwargs):
    kwargs.pop('trust_env', None); kwargs.pop('follow_redirects', None)
    kwargs.pop('proxy',None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with client.stream(*args, **kwargs) as response:
            yield response


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.config = configuration(Path(self.temp.name) / 'core.db')
        self.env = patch.dict('os.environ', {'TEST_GATEWAY_KEY': 'never-expose', 'TEST_BILLING_KEY': 'billing-only'}); self.env.start()

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_hierarchy_resolves_and_disabling_any_layer_denies(self):
        validate_config(self.config)
        profile = model_profile(self.config, self.config['services'][0])
        self.assertEqual((profile['providerId'], profile['accountId'], profile['keyId']), ('p', 'a', 'k'))
        self.assertEqual(profile['apiKeyEnv'], 'TEST_GATEWAY_KEY')
        self.assertNotIn('baseUrl', self.config['models'][0])
        for group in ('providers', 'providerAccounts', 'providerKeys', 'models'):
            self.config[group][0]['enabled'] = False
            with self.assertRaises(CoreError): model_profile(self.config, self.config['services'][0])
            self.config[group][0]['enabled'] = True
        self.assertNotIn('TEST_GATEWAY_KEY', json.dumps(public_hierarchy(self.config)))

    def test_references_and_prices_are_validated(self):
        for update in ({'apiKey': 'plaintext'}, {'apiKeyFile': 'path'}, {'accountId': 'missing'}):
            conf = copy.deepcopy(self.config); conf['providerKeys'][0].update(update)
            with self.assertRaises(CoreError): validate_config(conf)
        for pricing in ({'inputPerMillion': -1, 'currency': 'USD'}, {'inputPerMillion': float('nan'), 'currency': 'USD'}, {'inputPerMillion': 1}):
            conf = copy.deepcopy(self.config); conf['models'][0]['pricing'] = pricing
            with self.assertRaises(CoreError): validate_config(conf)

    def test_discovery_is_bounded_and_does_not_guess_prices(self):
        def handler(request):
            self.assertEqual(request.headers['authorization'], 'Bearer never-expose')
            self.assertEqual(str(request.url), 'https://provider.example/v1/models')
            return httpx.Response(200, json={'data': [{'id': 'vlm', 'context_length': 32000, 'capabilities': {'vision': True}, 'pricing': {'input': 'secret'}, 'description': 'Visual model'}]})
        with patch('agent_core.providers.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            result = discover_models(self.config, 'k')
        self.assertEqual(result['models'][0]['contextWindow'], 32000)
        self.assertEqual(result['models'][0]['capabilities'], {'vision': True})
        self.assertNotIn('pricing', result['models'][0])

    def test_billing_requires_explicit_adapter_and_separate_credential(self):
        self.assertFalse(query_provider_billing(self.config, 'a', 1, 86401)['supported'])
        self.config['providerAccounts'][0]['billing'] = {'adapter': 'openai-organization', 'apiKeyEnv': 'TEST_BILLING_KEY'}
        validate_config(self.config)
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers['authorization'], 'Bearer billing-only')
            self.assertEqual(request.url.path, '/v1/organization/costs')
            return httpx.Response(200, json={'data': [{'start_time': 1, 'end_time': 86401, 'results': [{'amount': {'value': 1.25, 'currency': 'usd'}, 'line_item': 'models', 'api_key': 'hidden'}]}], 'has_more': False})
        with patch('agent_core.providers.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            result = query_provider_billing(self.config, 'a', 1, 86401)
        self.assertEqual(result['currencyTotals'], {'USD': 1.25})
        self.assertNotIn('hidden', json.dumps(result)); self.assertEqual(len(calls), 1)
        with self.assertRaises(CoreError): query_provider_billing(self.config, 'a', 10, 1)

    def test_unknown_usage_and_unknown_prices_stay_null(self):
        store = Store(self.config['database']); service = self.config['services'][0]
        profile = model_profile(self.config, service)
        record_usage(store, service, profile, {}, 'failed')
        group = usage_summary(store)['groups'][0]
        self.assertIsNone(group['totalTokens']); self.assertIsNone(group['estimatedCost'])
        self.assertEqual(group['unknownUsageRequests'], 1)
        profile['pricing'] = {'inputPerMillion': 2, 'outputPerMillion': 4, 'currency': 'USD'}
        record_usage(store, service, profile, {'prompt_tokens': 100, 'completion_tokens': 50}, 'completed')
        priced = next(v for v in usage_summary(store)['groups'] if v['currency'] == 'USD')
        self.assertAlmostEqual(priced['estimatedCost'], .0004)
        self.assertEqual(priced['totalTokens'], 150)

    def test_agent_accounts_each_round_including_later_unknown_usage(self):
        from agent_core.agent import stream_agent
        class Runtime:
            tools = []; max_output_tokens = 256
            def check(self): pass
            def cancelled(self): return False
            def execute(self, *args): return {'data': {'answer': 'ok'}}
        store = Store(self.config['database']); service = self.config['services'][0]; profile = model_profile(self.config, service)
        rounds = [iter([{'type': 'tool_call', 'index': 0, 'id': 'call', 'function': {'name': 'lookup', 'arguments': '{}'}},
                        {'type': 'usage', 'prompt_tokens': 2, 'completion_tokens': 1}]),
                  iter([{'type': 'delta', 'content': 'complete'}])]
        def account(usage, status): record_usage(store, service, profile, usage, status, source='agent')
        with patch('agent_core.agent.OpenAICompatibleClient.stream', side_effect=rounds):
            list(stream_agent(profile, [{'role': 'user', 'content': 'hi'}], runtime=Runtime(), on_model_usage=account))
        group = usage_summary(store)['groups'][0]
        self.assertEqual(group['requests'], 2); self.assertEqual(group['completed'], 2)
        self.assertEqual(group['unknownUsageRequests'], 1)
        self.assertEqual(group['totalTokens'], 3)

    def test_agent_preserves_reported_usage_when_stream_later_fails(self):
        from agent_core.agent import stream_agent
        from agent_core.model import ProviderError
        class Runtime:
            tools = []; max_output_tokens = 256
            def check(self): pass
            def cancelled(self): return False
        def events(*args, **kwargs):
            yield {'type': 'usage', 'prompt_tokens': 5, 'completion_tokens': 1}
            raise ProviderError('INCOMPLETE_RESPONSE')
        records = []
        with patch('agent_core.agent.OpenAICompatibleClient.stream', side_effect=events):
            with self.assertRaises(ProviderError):
                list(stream_agent({}, [{'role': 'user', 'content': 'hi'}], runtime=Runtime(), on_model_usage=lambda usage, status: records.append((usage, status))))
        self.assertEqual(records, [({'prompt_tokens': 5, 'completion_tokens': 1}, 'failed')])


if __name__ == '__main__': unittest.main()
