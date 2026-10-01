"""Immutable official resolution snapshots, separate from independently observed hits."""
from decimal import Decimal, InvalidOperation
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .evaluation import digest
from .evaluation_record import packed
from .semantics import timestamp
from .store import decode

GAMMA = 'https://gamma-api.polymarket.com/markets/'
RESOLUTIONS = 'https://data-api.polymarket.com/v2/resolutions'


def get_json(url):
    with urlopen(Request(url,headers={'User-Agent':'BASIS-research-evaluation/1.0'}),timeout=5) as response:
        return json.load(response)


def official_result(market, response):
    condition = str(market.get('conditionId','')).lower()
    state = next((r for r in response.get('data',[]) if str(r.get('condition_id','')).lower()==condition),None)
    if not state or str(state.get('status','')).lower() != 'resolved':
        return dict(kind='NOT_YET_RESOLVED',yes=None,review='PENDING',resolution_state=state)
    try:
        outcomes=market.get('outcomes',[])
        outcomes=json.loads(outcomes) if isinstance(outcomes,str) else outcomes
        if sorted(str(x).lower() for x in outcomes) != ['no','yes']:
            raise ValueError('Nonbinary outcome mapping')
        payouts=state.get('payouts')
        if isinstance(payouts,list) and len(payouts)==2:
            amounts=[Decimal(str(v)) for v in payouts]
            total=sum(amounts)
            result=amounts[next(i for i,x in enumerate(outcomes) if str(x).lower()=='yes')]/total if total else None
        else:
            # Native/UMA oracle price uses 18 decimals; 69 and fractional/void payouts are not YES/NO.
            price=Decimal(str(state.get('price')))
            result=Decimal(1) if price==10**18 else Decimal(0) if price==0 else None
        if result not in (Decimal(0),Decimal(1)):
            raise ValueError('Nonbinary/void oracle payout')
    except (InvalidOperation,ValueError,TypeError,ZeroDivisionError):
        return dict(kind='AMBIGUOUS',yes=None,review='REVIEW_REQUIRED',resolution_state=state)
    when=timestamp(state.get('resolved_at'))
    if when is None:
        when=timestamp(state.get('last_update_timestamp'))
        basis='PROVIDER_LAST_UPDATE; exact settlement time unavailable'
    else:
        basis='PROVIDER_RESOLVED_AT'
    return dict(kind='OFFICIAL_RESOLUTION',yes=int(result),review='OFFICIAL_BINARY_API',
        resolution_timestamp=when,resolution_timestamp_basis=basis,resolution_state=state,
        was_disputed=bool(state.get('was_disputed')),extended_review=bool(state.get('extended_review')))


class ResolutionPoller:
    def __init__(self, engine, recorder, stop=None):
        self.engine,self.recorder=engine,recorder
        self.stop=stop
        self.next_poll=0;self.offset=0

    def opening_wording(self, forecast):
        ref=forecast.get('input_refs',{}).get('catalog')
        record=self.recorder.tape.raw_record(ref) if ref is not None else None
        markets=record.get('payload',{}).get('markets',[]) if record else []
        market=next((r for r in markets if str(r.get('id'))==forecast['event_id']),None)
        return market, ref

    def poll(self):
        now=time.time_ns()//1000000
        if now<self.next_poll:return
        self.next_poll=now+300000
        r=self.recorder;db=r.db.db
        events=[decode(x[0]) for x in db.execute('SELECT data FROM forecasts WHERE campaign_id=? ORDER BY event_id',(r.cid,))]
        # Include deferred rows whose target baseline is unavailable; lack of OPT is not lack of an official proposition.
        known={x['event_id'] for x in events}
        for row in db.execute('SELECT data FROM entries WHERE campaign_id=? AND kind=? ORDER BY opened_ms,id',(r.cid,'DEFERRED_RESPONSE')):
            entry=decode(row[0])
            if entry['event_id'] not in known:
                events.append(entry);known.add(entry['event_id'])
        events=[x for x in events if x['expiry']<=now and str(x['event_id']).isdigit()]
        if not events:return
        selected=[events[(self.offset+i)%len(events)] for i in range(min(4,len(events)))]
        self.offset=(self.offset+len(selected))%len(events)
        for forecast in selected:
            if self.stop and self.stop.is_set():return
            latest=db.execute('SELECT available_ms FROM resolutions WHERE event_id=? AND kind=? ORDER BY available_ms DESC LIMIT 1',
                (forecast['event_id'],'OFFICIAL_RESOLUTION')).fetchone()
            if latest and now-latest[0]<86400000:continue
            try:
                market=get_json(GAMMA+forecast['event_id'])
                if self.stop and self.stop.is_set():return
                condition=market.get('conditionId')
                if not condition:raise ValueError('Gamma omitted condition ID')
                response=get_json(RESOLUTIONS+'?'+urlencode(dict(condition=condition)))
                result=official_result(market,response)
                opening,catalog_ref=self.opening_wording(forecast)
                wording=dict(question=market.get('question'),description=market.get('description'),
                    outcomes=market.get('outcomes'),condition_id=condition)
                opening_wording=dict(question=opening.get('question'),description=opening.get('description'),
                    outcomes=opening.get('outcomes'),condition_id=opening.get('conditionId')) if opening else None
                if opening is None or str(opening.get('question','')).strip()!=str(market.get('question','')).strip() or str(opening.get('description','')).strip()!=str(market.get('description','')).strip():
                    result['review']='SETTLEMENT_WORDING_REVIEW_REQUIRED'
                if result['kind']=='NOT_YET_RESOLVED':
                    # Pending snapshots are still immutable evidence, deduplicated by content.
                    result['review']='PENDING'
                body=dict(market=market,resolution_response=response)
                fingerprint=digest(dict(event_id=forecast['event_id'],event_version=forecast['event_version'],body=body))
                if db.execute('SELECT 1 FROM resolutions WHERE id=?',(fingerprint,)).fetchone():continue
                if self.stop and self.stop.is_set():return
                # The existing single writer records the exact provider response. Unknown reducer kind has no research/policy effect.
                raw_id=self.engine.ingest('polymarket_resolution','resolution',forecast['event_id'],body)
                available=time.time_ns()//1000000
                data=dict(result,event_id=forecast['event_id'],event_version=forecast['event_version'],raw_id=raw_id,
                    available_ms=available,source='POLYMARKET_DATA_API_V2_RESOLUTIONS',
                    source_url=RESOLUTIONS+'?'+urlencode(dict(condition=condition)),integrity_hash=digest(body),
                    settlement_wording=wording,settlement_version=digest(wording),opening_settlement_wording=opening_wording,
                    opening_catalog_raw_id=catalog_ref,opening_mapping_hash=forecast.get('mapping_hash'),
                    distinction='Official proposition resolution; independent observed path hits never overwrite it')
                with r.db.transaction():
                    db.execute('INSERT INTO resolutions VALUES(?,?,?,?,?,?,?)',(fingerprint,forecast['event_id'],
                        forecast['event_version'],result['kind'],available,raw_id,packed(data)))
                    if result['kind']=='OFFICIAL_RESOLUTION':
                        for row in db.execute('SELECT data FROM entries WHERE campaign_id=? AND event_id=? '
                            'AND NOT EXISTS(SELECT 1 FROM outcomes WHERE entry_id=entries.id AND name=?)',
                            (r.cid,forecast['event_id'],'resolution')):
                            entry=decode(row[0]);r.record_outcome(entry,'resolution',available,dict(
                                status='OBSERVED' if result['review']=='OFFICIAL_BINARY_API' else 'MISSING',
                                reason=None if result['review']=='OFFICIAL_BINARY_API' else result['review'],
                                final_yes=result['yes'],resolution_id=fingerprint,raw_id=raw_id,
                                availability_ms=available,resolution_timestamp=result.get('resolution_timestamp'),
                                repricing_status='NOT_A_TARGET_QUOTE; settlement/calibration outcome only'))
            except Exception as error:
                # API failure cannot stop the research collector or masquerade as NO resolution.
                r.db.record(r.cid,'resolution_source_error',dict(event_id=forecast['event_id'],error=str(error),state='MISSING'))
