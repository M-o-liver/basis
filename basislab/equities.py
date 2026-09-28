"""Public US stock snapshots with recorded session coverage and corporate context."""
from datetime import datetime, timezone
from functools import lru_cache
import time
from zoneinfo import ZoneInfo
from .semantics import number, timestamp

NY=ZoneInfo('America/New_York')
_catalysts={}


@lru_cache(maxsize=1)
def calendar():
    import exchange_calendars
    return exchange_calendars.get_calendar('XNYS')


def session_close(date):
    cal=calendar()
    return int(cal.session_close(date).timestamp()*1000) if cal.is_session(date) else None


def snapshot(asset,cutoffs,starts,offset_hours,now=None):
    import exchange_calendars
    import yfinance as yf
    now=now or time.time_ns()//1000000
    cal=calendar();today=datetime.fromtimestamp(now/1000,NY).date().isoformat()
    ticker=yf.Ticker(asset)
    daily=ticker.history(period='3mo',interval='1d',auto_adjust=False,prepost=False,actions=True,raise_errors=True,timeout=12)
    if daily.empty:raise ValueError('No stock history returned')
    metadata=ticker.history_metadata
    spot=number(metadata.get('regularMarketPrice'))
    source_ms=timestamp(metadata.get('regularMarketTime'))
    if spot is None:spot=number(daily['Close'].iloc[-1])
    if source_ms is None:source_ms=int(daily.index[-1].timestamp()*1000)
    session=cal.is_session(today)
    is_open=session and cal.session_open(today).timestamp()*1000<=now<cal.session_close(today).timestamp()*1000
    candles=[]
    for index,row in daily.iterrows():
        day=index.date().isoformat()
        if not cal.is_session(day):continue
        opened=int(cal.session_open(day).timestamp()*1000)
        if opened>now:continue
        candles.append(dict(session=day,candle=[opened//1000,number(row['Low']),number(row['High']),number(row['Open']),number(row['Close']),number(row['Volume'])],split=number(row.get('Stock Splits'))))
    histories=[]
    for start in starts:
        if start is None:continue
        first=datetime.fromtimestamp(start/1000,NY).date().isoformat()
        if first>today:continue
        required=[s.date().isoformat() for s in cal.sessions_in_range(first,today) if cal.session_open(s).timestamp()*1000<=now]
        selected=[c for c in candles if c['session'] in required]
        histories.append(dict(start_ms=start,candles=[c['candle'] for c in selected],sessions=[c['session'] for c in selected],
            required_sessions=required,session_calendar='XNYS',calendar_version=exchange_calendars.__version__,
            corporate_action=any(c['split'] not in (None,0) for c in selected),regular_hours_only=True))
    changes={}
    closes=[number(v) for v in daily['Close']]
    for days in (1,5,20):
        if len(closes)>days and closes[-days-1] and spot:changes[str(days)]=spot/closes[-days-1]-1
    catalyst=_catalysts.get(asset)
    if not catalyst or now-catalyst['retrieved_ms']>21600000:
        try:
            raw=ticker.calendar or {}
            earnings=[int(datetime.combine(d,datetime.min.time(),NY).timestamp()*1000) for d in raw.get('Earnings Date',[])]
            catalyst=dict(status='AVAILABLE' if earnings else 'NO_EARNINGS_DATE',earnings_dates=earnings,
                ex_dividend=str(raw.get('Ex-Dividend Date') or ''),retrieved_ms=now)
        except Exception as error:catalyst=dict(status='UNKNOWN',error=type(error).__name__,earnings_dates=[],retrieved_ms=now)
        _catalysts[asset]=catalyst
    chains=[];errors=[];dates=[]
    try:
        dates=list(ticker.options);choices=set()
        for cutoff in cutoffs:
            for date in dates:
                expiry=session_close(date)
                if expiry is not None and 0<=expiry-cutoff<=offset_hours*3600000:choices.add(date);break
        for date in sorted(choices)[:3]:
            chain=ticker.option_chain(date)
            def contracts(frame,kind):
                if frame is None:return []
                return [dict(instrument=str(r['contractSymbol']),strike=number(r['strike']),bid=number(r['bid']),ask=number(r['ask']),
                    mark=number(r['lastPrice']),iv=number(r['impliedVolatility']),option_type=kind,
                    last_trade=str(r['lastTradeDate']),volume=number(r['volume']),open_interest=number(r['openInterest']),
                    contract_size=str(r.get('contractSize','')),currency=str(r.get('currency',''))) for r in frame.to_dict(orient='records') if number(r.get('strike'))]
            chains.append(dict(expiry=session_close(date),calls=contracts(chain.calls,'call'),puts=contracts(chain.puts,'put')))
    except Exception as error:errors.append(dict(stage='options',error=type(error).__name__,detail=str(error)[:150]))
    return dict(chains=chains,available_expiries=dates,spot=spot,spot_source_ms=source_ms,adapter='yfinance',quote_delay='unknown',
        histories=histories,partial_errors=errors,underlying_context=dict(asset_class='equity',name=metadata.get('longName') or metadata.get('shortName') or asset,
            exchange=metadata.get('exchangeName'),session_calendar='XNYS',market_state='OPEN' if is_open else 'CLOSED',
            regular_hours_only=True,price_asof=source_ms,session_returns=changes,catalyst=catalyst,
            corporate_action=any(h['corporate_action'] for h in histories),quote_delay='unknown',daily_candles=[c['candle'] for c in candles[-22:]]))
