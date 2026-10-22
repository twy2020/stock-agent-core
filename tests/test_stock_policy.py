import unittest
from agent_core.evidence import model_projection


class StockPolicyTests(unittest.TestCase):
    def test_stock_projection_never_exposes_chart_rows(self):
        result = {'status':'succeeded','data':{'stockArtifact':{
            'version':'stock-artifact/1','datasetRef':'ds_'+'a'*32,'summary':{'n':1000},
            'preview':[],'charts':[{'kind':'candlestick','rows':[{'close':i} for i in range(1000)]}]}}}
        projected = model_projection('stock_query',result,'op','run')
        self.assertNotIn('rows',projected['data']['stockArtifact']['charts'][0])


if __name__ == '__main__': unittest.main()
