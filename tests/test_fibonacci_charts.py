import base64
import copy
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
import structure_analysis_charts as report


def history(count=240, scale=1):
    x=np.arange(count)
    close=scale*(100+.04*x+12*np.sin(x*np.pi/12))
    return pd.DataFrame({'Open':close,'High':close+scale,'Low':close-scale,
                         'Close':close,'Volume':np.full(count,10000)},
                        index=pd.bdate_range('2025-01-01',periods=count))


class FibonacciChartTests(unittest.TestCase):
    def test_wave_confirmation_directions_and_scale(self):
        hist=history()
        directions=set()
        for count in range(80,len(hist)+1):
            c=report.fibonacci_context(hist.iloc[:count])
            if c['quality']!='OK':continue
            directions.add(c['direction'])
            for key in ('low','high'):
                p=c[key]
                i=hist.index.get_loc(pd.Timestamp(p['date']))
                self.assertEqual(p['confirmed_on'],hist.index[i+3].strftime('%Y-%m-%d'))
                self.assertLessEqual(p['confirmed_on'],hist.index[count-1].strftime('%Y-%m-%d'))
        self.assertEqual(directions,{'UP','DOWN'})
        a,b=report.fibonacci_context(hist),report.fibonacci_context(history(scale=100))
        self.assertEqual(a['daily_state'],b['daily_state'])
        for ratio,value in a['levels'].items():self.assertAlmostEqual(value*100,b['levels'][ratio])

    def test_left_image_is_independent_of_fibonacci_overlays(self):
        hist=history()
        a=report.make_chart_base64(hist,95,110,'TEST')
        legacy=dict(zone_lo=1,zone_hi=90000,swing_low=.01,swing_high=100000,fib50=100,fib618=120)
        b=report.make_chart_base64(hist,95,110,'TEST',fib20=legacy,fib60=legacy)
        self.assertIsNotNone(a)
        self.assertEqual(a,b)

    def test_dual_panel_render_keeps_published_signal_data(self):
        row=report.analyze('TEST',history())
        session=history().index[-1].strftime('%Y-%m-%d')
        before=report.build_structure_json([row],'2026-11-08 21:00',session,{})
        page=report.build_html([row],'2026-11-08 21:00')
        after=report.build_structure_json([row],'2026-11-08 21:00',session,{})
        self.assertEqual(before,after)
        self.assertEqual(page.count('class="chart-pair"'),1)
        self.assertIn('max-width: 800px',page)
        self.assertIn('Fibonacci 獨立圖',page)
        self.assertIn('0.786',page)
        self.assertIn('原區間參考',page)
        self.assertTrue(base64.b64decode(row['fibonacci_chart']).startswith(b'\x89PNG'))

    def test_short_history_retains_chart_but_explains_missing_wave(self):
        hist=history(40)
        row=report.analyze('SHORT',hist)
        row['fibonacci']=report.fibonacci_context(hist)
        row['fibonacci_chart']=report.make_fibonacci_chart(hist,row['fibonacci'],'SHORT')
        self.assertEqual(row['fibonacci']['quality'],'INSUFFICIENT_HISTORY')
        self.assertIn('不足 80 根',report.fibonacci_panel(row))
        self.assertIsNotNone(row['fibonacci_chart'])

    def test_future_bars_never_change_prior_context(self):
        hist=history()
        before=report.fibonacci_context(hist.iloc[:180])
        hist.iloc[200,hist.columns.get_loc('High')]=999999
        self.assertEqual(before,report.fibonacci_context(hist.iloc[:180]))


if __name__=='__main__':unittest.main()
