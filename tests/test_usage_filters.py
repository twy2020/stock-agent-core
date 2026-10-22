import tempfile
import unittest
from pathlib import Path
from agent_core.store import Store
from agent_core.providers import record_usage,usage_summary
from agent_core.security import CoreError


class UsageFilterTests(unittest.TestCase):
    def test_filters_exclude_other_callers_and_keep_unknown_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3')
            model={'id':'model','pricing':{'inputPerMillion':2,'outputPerMillion':4,'currency':'CNY'}}
            record_usage(store,{'id':'a','tenantId':'t'},model,{'prompt_tokens':100,'completion_tokens':20},'completed')
            record_usage(store,{'id':'b','tenantId':'t'},model,{},'failed')
            result=usage_summary(store,{'caller':'a','profile':'model'})
            self.assertEqual(len(result['recent']),1)
            self.assertEqual(result['groups'][0]['totalTokens'],120)
            self.assertAlmostEqual(result['groups'][0]['estimatedCost'],0.00028)
            self.assertEqual(usage_summary(store,{'caller':'b'})['groups'][0]['unknownCostRequests'],1)
            self.assertEqual(usage_summary(store,{'start':'1','end':'2'})['recent'],[])
            with self.assertRaises(CoreError):usage_summary(store,{'start':'8','end':'2'})
            with self.assertRaises(CoreError):usage_summary(store,{'groupBy':'caller;DELETE'})
