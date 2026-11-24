from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
import mcx_s3_bot as b
from mcx_recorder import Recorder, records, export_csv, analyse
T=datetime(2026,11,24,12,tzinfo=b.IST)

def contract(k=100,side='CE',key=None,expiry='2026-12-15'):
    end=datetime.fromisoformat(expiry).replace(hour=23,minute=59,tzinfo=b.IST)
    return {'segment':'MCX_FO','instrument_type':side,'instrument_key':key or f'MCX_FO|{k}{side}',
            'underlying_symbol':'GOLD','underlying_key':'MCX_FO|F1','expiry':int(end.timestamp()*1000),
            'lot_size':100,'qty_multiplier':100,'strike_price':k,'trading_symbol':f'GOLD {k} {side}'}

def quote(price=10,t=T,qty=1):
    return {'last_price':price,'last_trade_time':str(int(t.timestamp()*1000)),
            'timestamp':str(int(t.timestamp()*1000)), 'oi':1,'volume':999999,
            'depth':{'buy':[{'price':price*.8,'quantity':qty}], 'sell':[{'price':price*1.2,'quantity':qty}]}}

class MathTests(unittest.TestCase):
    def test_parity(self):
        import math
        self.assertAlmostEqual(b.black76(105,100,.2,.3,'CE')-b.black76(105,100,.2,.3,'PE'),math.exp(-.06*.2)*5)
    def test_iv_roundtrip_both_sides(self):
        for side in ['CE','PE']:
            for k in [90,100,110]:
                p=b.black76(100,k,.2,.4,side)
                self.assertAlmostEqual(b.implied_vol(p,100,k,.2,side),.4,places=7)
    def test_iv_impossible_price(self):
        self.assertIsNone(b.implied_vol(200,100,100,.1,'CE'))
        self.assertIsNone(b.implied_vol(0,100,100,.1,'PE'))
    def test_signed_delta_and_gamma(self):
        self.assertGreater(b.greeks(100,100,.2,.3,'CE')['delta'],0)
        self.assertLess(b.greeks(100,100,.2,.3,'PE')['delta'],0)
        self.assertGreater(b.greeks(100,100,.2,.3,'PE')['gamma'],0)
    def test_discover_exact_future_nearest_expiry_all_strikes(self):
        future={'instrument_type':'FUT','instrument_key':'MCX_FO|F1'}
        rows=[future]+[contract(k,s) for k in [80,90,100,110,120] for s in ['CE','PE']]+[contract(100,expiry='2027-01-15')]
        opts,futs,_=b.discover(rows,T,{'GOLD'})
        self.assertEqual(len(opts),10);self.assertEqual(set(futs),{'MCX_FO|F1'})
    def test_missing_underlying_never_guessed(self):
        opts,_,skips=b.discover([contract()],T,{'GOLD'})
        self.assertFalse(opts);self.assertEqual(skips['unmapped_future'],1)
    def test_expired_skipped(self):
        r=contract(expiry='2026-11-23')
        self.assertFalse(b.discover([r,{'instrument_type':'FUT','instrument_key':'MCX_FO|F1'}],T,{'GOLD'})[0])
    def test_missing_data_is_null(self):
        r=b.snapshot(contract(),{},T)
        self.assertIsNone(r['oi_raw']);self.assertIsNone(r['bid']);self.assertIn('missing_quote',r['quality_flags'])
    def test_peer_excludes_own_and_no_strategy_filters(self):
        rows=[]
        for k,vol in [(90,.3),(100,.2),(110,.3)]:
            c=contract(k);years=(b.expiry_at(c)-T).total_seconds()/(365*86400)
            q=quote(b.black76(100,k,years,vol,'CE'))
            rows.append(b.snapshot(c,q,T,(quote(100),T)))
        b.evaluate(rows,T)
        self.assertAlmostEqual(rows[1]['peer_iv_pct'],30)
        self.assertGreater(rows[1]['gap_pct'],0)
        self.assertEqual(rows[1]['oi_raw'],1)
        self.assertGreater(rows[1]['spread_pct_mid'],30)
        self.assertEqual(rows[0]['model_status'],'missing_bracketing_peer_iv')
    def test_previous_day_still_recordable_not_model_input(self):
        r=b.snapshot(contract(),quote(t=T-timedelta(days=1)),T,(quote(100),T))
        b.evaluate([r],T)
        self.assertEqual(r['model_status'],'no_same_day_trade_inputs')
        self.assertIsNone(r['model_target'])
    def test_close_schedule(self):
        self.assertEqual(b.close_time('2026-11-24'),b.walltime(23,55))
        self.assertEqual(b.close_time('2027-03-15'),b.walltime(23,30))
    def test_greek_request_limit_and_all_keys(self):
        api=b.API.__new__(b.API);sizes=[]
        def get(path,keys):
            sizes.append(len(keys))
            return {k:{'instrument_token':k,'delta':.5} for k in keys}
        api.get=get
        result=api.quotes([f'MCX_FO|{i}' for i in range(121)],greek=True)
        self.assertEqual(sizes,[50,50,21]);self.assertEqual(len(result),121)
    def test_api_maps_instrument_token(self):
        api=b.API.__new__(b.API)
        api.get=lambda *a:{'MCX:SAMPLE':{'instrument_token':'MCX_FO|123','last_price':1}}
        self.assertIn('MCX_FO|123',api.quotes(['MCX_FO|123']))

class RecorderTests(unittest.TestCase):
    def test_append_restart_no_overwrite_and_export(self):
        with tempfile.TemporaryDirectory() as root:
            for n in range(2):
                r=Recorder(root);r.write({'kind':'option','observed_at':b.iso(T),'instrument_key':'X','ltp':n,'broker_greeks':{'delta':.5}});r.close()
            self.assertEqual(len(list(records(root))),2)
            file=Path(root)/'out.csv';export_csv(root,file)
            self.assertIn('broker_greeks_delta',file.read_text())
    def test_analyse_no_preentry_touch_and_future_touch(self):
        with tempfile.TemporaryDirectory() as root:
            rec=Recorder(root)
            for minute,price,lastminute in [(0,10,-1),(1,20,-1),(2,11,2),(3,15,3)]:
                rec.write({'kind':'option','instrument_key':'X','observed_at':b.iso(T+timedelta(minutes=minute)),
                           'last_trade_at':b.iso(T+timedelta(minutes=lastminute)),
                           'ltp':price,'model_target':14,'gap_pct':20})
            rec.close();out=Path(root)/'a.csv';result=analyse(root,out)
            self.assertEqual(result['TARGET_OBSERVED'],3)
            self.assertEqual(result['NO_LATER_DATA'],1)
    def test_raw_depth_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            row=b.snapshot(contract(),quote(),T)
            rec=Recorder(root);rec.write(row);rec.close()
            self.assertEqual(next(records(root))['raw_quote']['depth'],quote()['depth'])

class WatchTests(unittest.TestCase):
    def test_dedup_and_no_stale_touch_restart(self):
        with tempfile.TemporaryDirectory() as root:
            s=b.Scanner(None,root,telegram=False)
            row=b.snapshot(contract(),quote(),T,(quote(100),T))
            row.update(model_status='computed',model_target=12,gap_pct=100*(12-10)/12,ask_gap_pct=20,ask=9.6,futures_price=100)
            s.recorder.write(row);s.process([row],T,T.date().isoformat());s.save()
            s.process([row],T,T.date().isoformat());self.assertEqual(len(s.state['watches']),1)
            s.recorder.close();s=b.Scanner(None,root,telegram=False)
            row.update(ltp=20,observed_at=b.iso(T+timedelta(minutes=1)))
            s.process([row],T+timedelta(minutes=1),T.date().isoformat())
            self.assertEqual(s.state['watches'][0]['status'],'OPEN')
            row['last_trade_at']=b.iso(T+timedelta(minutes=1))
            s.process([row],T+timedelta(minutes=1),T.date().isoformat())
            self.assertEqual(s.state['watches'][0]['status'],'TARGET_OBSERVED')
    def test_report_no_later_data_not_claimed_full_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            s=b.Scanner(None,root,telegram=False);day=T.date().isoformat()
            s.state['days'][day]={'sweeps':1,'option_count':2}
            s.state['watches']=[{'day':day,'status':'OPEN'}]
            s.report(day)
            self.assertEqual(s.state['watches'][0]['status'],'NO_TOUCH_OBSERVED')


class GapTests(unittest.TestCase):
    def test_ltp_and_ask_15pct_boundaries(self):
        for price,ask,expected in [(85,85,True),(84,85,True),(85.01,85,False),(84,85.01,False),(80,101,False),(80,None,False)]:
            row={'model_status':'computed','model_target':100,'ltp':price,'ask':ask}
            self.assertEqual(b.alert_eligible(row),expected)
    def test_old_pending_alerts_suppressed_without_deleting_data(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            Path(root,'state.json').write_text(json.dumps({'version':b.VERSION,'days':{},'watches':[],
                'outbox':[{'id':'entry-old:0','sent':False,'text':'old'}]}))
            scanner=b.Scanner(None,root)
            self.assertEqual(scanner.state['outbox'][0]['suppressed'],'superseded_unfiltered_policy')
            scanner.emit('entry-new','new');scanner.save()
            reloaded=b.Scanner(None,root)
            self.assertNotIn('suppressed',reloaded.state['outbox'][1])

class IntegrationTests(unittest.TestCase):
    def test_sweep_records_all_and_falls_back_when_greeks_unavailable(self):
        from unittest.mock import patch
        future={'instrument_type':'FUT','instrument_key':'MCX_FO|F1'}
        contracts=[contract(k) for k in [90,100,110]]
        class API:
            def master(self):return [future]+contracts
            def quotes(self,keys,greek=False):
                if greek:raise RuntimeError('unsupported')
                result={}
                for key in keys:
                    if key=='MCX_FO|F1':q=quote(100)
                    else:
                        c=next(c for c in contracts if c['instrument_key']==key)
                        years=(b.expiry_at(c)-T).total_seconds()/(365*86400)
                        q=quote(b.black76(100,c['strike_price'],years,.15 if c['strike_price']==100 else .3,'CE'))
                    result[key]=(q,T)
                return result
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            scanner=b.Scanner(API(),root,telegram=False);scanner.sweep(T);scanner.recorder.close()
            rows=[r for r in records(Path(root)/'snapshots') if r['kind']=='option']
            self.assertEqual(len(rows),3)
            self.assertTrue(all(r['broker_greeks'] is None for r in rows))
            self.assertEqual(len(scanner.state['watches']),1)
            self.assertEqual(scanner.state['days']['2026-11-24']['last_quote_count'],3)
            scanner.report('2026-11-24')
    def test_telegram_queue_event_dedup(self):
        with tempfile.TemporaryDirectory() as root:
            scanner=b.Scanner(None,root)
            scanner.emit('entry-X','message');scanner.emit('entry-X','message')
            self.assertEqual(len(scanner.state['outbox']),1)

if __name__=='__main__':unittest.main()
