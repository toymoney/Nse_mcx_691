from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import mcx_s3_bot as b
from test_mcx import T, contract, quote
from mcx_recorder import records

class FakeAPI:
    def __init__(self,strikes=(90,100,110)):
        self.contracts=[contract(k) for k in strikes];self.calls=[];self.future=100
        self.stale_key=None;self.missing_key=None;self.rich=False
    def master(self):return [{'instrument_type':'FUT','instrument_key':'MCX_FO|F1'}]+self.contracts
    def quotes(self,keys,greek=False):
        self.calls.append((list(keys),greek))
        if greek:return {}
        out={}
        for key in keys:
            if key==self.missing_key:continue
            if key=='MCX_FO|F1':q=quote(self.future)
            else:
                c=next(c for c in self.contracts if c['instrument_key']==key)
                years=(b.expiry_at(c)-T).total_seconds()/(365*86400)
                p=b.black76(100,c['strike_price'],years,.3,'CE')
                if c['strike_price']==100 and not self.rich:p*=.97
                q=quote(p);q.update(oi=1000,volume=500)
                q['depth']['buy'][0]['price']=p*.999;q['depth']['sell'][0]['price']=p*1.001
            if key==self.stale_key:q['last_trade_time']=str(int((T-timedelta(seconds=31)).timestamp()*1000))
            out[key]=(q,T)
        return out

class FetchTests(unittest.TestCase):
    def scanner(self,root,api,telegram=False):
        s=b.Scanner(api,root,telegram=telegram);s.options=api.contracts;return s
    def seed(self):return dict(instrument_key='MCX_FO|100CE',underlying_key='MCX_FO|F1',peer_keys=['MCX_FO|90CE','MCX_FO|110CE'])
    def test_four_instruments_single_request_and_fixed_target_no_preentry_hit(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI();s=self.scanner(root,api);r=s.refresh_candidate(self.seed())
            self.assertEqual(len(api.calls),1);self.assertEqual(len(api.calls[0][0]),4)
            self.assertTrue(b.alert_eligible(r));self.assertFalse(r['input_freshness_reasons'])
            s.process([r],T,T.date().isoformat());w=s.state['watches'][0];target=w['target']
            r.update(model_target=target*1.01,last_trade_at=b.iso(T),ltp=target*2)
            s.process([r],T+timedelta(seconds=1),T.date().isoformat())
            self.assertEqual(w['target'],target);self.assertEqual(w['status'],'OPEN');s.recorder.close()
    def test_stale_candidate_peer_future_and_small_gap_diagnostics(self):
        for key in ['MCX_FO|100CE','MCX_FO|90CE','MCX_FO|F1']:
            with self.subTest(key=key),tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
                api=FakeAPI();api.stale_key=key;s=self.scanner(root,api);r=s.refresh_candidate(self.seed())
                self.assertFalse(b.alert_eligible(r));self.assertEqual(b.diagnostic_bucket(r),'blocked_stale_inputs');s.recorder.close()
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI();api.rich=True;s=self.scanner(root,api);r=s.refresh_candidate(self.seed())
            self.assertFalse(b.alert_eligible(r));self.assertEqual(b.diagnostic_bucket(r),'valid_pricing_gap_too_small');s.recorder.close()
    def test_missing_refresh_never_uses_cached_input(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI();api.missing_key='MCX_FO|90CE';s=self.scanner(root,api)
            self.assertIsNone(s.refresh_candidate(self.seed()));self.assertEqual(s.counts['refresh_missing_inputs'],1);s.recorder.close()
    def test_atm_recomputed_from_full_master_after_future_moves(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI(range(20,181,10));api.future=160;s=self.scanner(root,api)
            r=s.refresh_candidate(self.seed());self.assertEqual(r['strike_steps_from_atm'],-6)
            self.assertFalse(b.alert_eligible(r));s.recorder.close()
    def test_priority_before_background_and_greeks_all_raw_preserved(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI(range(20,181,10));s=b.Scanner(api,root,telegram=False);s.sweep(T,create_watches=False);s.recorder.close()
            calls=api.calls;priority=calls[1][0]
            self.assertIn('MCX_FO|50CE',priority);self.assertIn('MCX_FO|150CE',priority);self.assertNotIn('MCX_FO|20CE',priority)
            greek=next(i for i,c in enumerate(calls) if c[1]);far=next(i for i,c in enumerate(calls) if 'MCX_FO|20CE' in c[0])
            self.assertGreater(greek,far)
            saved=[r for r in records(Path(root)/'snapshots') if r['kind']=='option']
            self.assertEqual({r['instrument_key'] for r in saved},{c['instrument_key'] for c in api.contracts});self.assertEqual(len(saved),len(api.contracts))
    def test_send_recheck_rejects_disappeared_gap(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI();s=self.scanner(root,api,telegram=True);s.state['days'][T.date().isoformat()]={}
            r=s.refresh_candidate(self.seed());s.process([r],T,T.date().isoformat());api.rich=True
            with patch.dict(b.os.environ,{'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'test'}),patch.object(b.requests,'post') as send,patch.object(b.time,'sleep'):
                s.flush();send.assert_not_called()
            self.assertEqual(s.state['watches'][0]['status'],'ENTRY_RECHECK_REJECTED');s.recorder.close()
    def test_successful_send_confirms_refreshed_entry(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI();s=self.scanner(root,api,telegram=True);s.state['days'][T.date().isoformat()]={}
            r=s.refresh_candidate(self.seed());s.process([r],T,T.date().isoformat())
            with patch.dict(b.os.environ,{'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'test'}),patch.object(b.requests,'post') as send,patch.object(b.time,'sleep'):
                send.return_value.ok=True;send.return_value.status_code=200;send.return_value.json.return_value={'ok':True};s.flush();send.assert_called_once()
            self.assertTrue(s.state['watches'][0]['entry_confirmed']);self.assertTrue(s.state['outbox'][0]['sent']);s.recorder.close()

    def test_priority_alert_sent_before_far_strikes_or_greeks(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI(range(20,181,10));s=b.Scanner(api,root,telegram=True);sent_calls=[]
            def post(*args,**kwargs):
                if kwargs['json']['text'].startswith('🟢'):
                    sent_calls.append(list(api.calls))
                from unittest.mock import Mock
                r=Mock();r.ok=True;r.status_code=200;r.json.return_value={'ok':True};return r
            with patch.dict(b.os.environ,{'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'test'}),patch.object(b.requests,'post',side_effect=post),patch.object(b.time,'sleep'):
                s.sweep(T)
            self.assertEqual(len(sent_calls),1)
            self.assertFalse(any(greek for _,greek in sent_calls[0]))
            self.assertFalse(any('MCX_FO|20CE' in keys for keys,_ in sent_calls[0]))
            self.assertEqual(len(s.state['watches']),1);self.assertTrue(s.state['watches'][0]['entry_confirmed']);s.recorder.close()
    def test_rejected_delivery_can_retry_without_duplicate_watch(self):
        with tempfile.TemporaryDirectory() as root,patch.object(b,'now',return_value=T):
            api=FakeAPI();s=self.scanner(root,api,telegram=True);s.state['days'][T.date().isoformat()]={}
            r=s.refresh_candidate(self.seed());s.process([r],T,T.date().isoformat());api.rich=True
            with patch.dict(b.os.environ,{'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'test'}),patch.object(b.requests,'post') as send,patch.object(b.time,'sleep'):
                s.flush();send.assert_not_called();api.rich=False
                r=s.refresh_candidate(self.seed());s.process([r],T,T.date().isoformat())
                send.return_value.ok=True;send.return_value.status_code=200;send.return_value.json.return_value={'ok':True};s.flush();send.assert_called_once()
            self.assertEqual(len(s.state['watches']),1);self.assertEqual(len(s.state['outbox']),1)
            self.assertTrue(s.state['watches'][0]['entry_confirmed']);s.recorder.close()
