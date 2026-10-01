"""Public read-only feeds, independent of any browser. Bounded requests, reconnects, no credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import shutil
import threading
import time
import urllib.parse
import urllib.request
from .semantics import number, timestamp

GAMMA = 'https://gamma-api.polymarket.com'
DERIBIT = 'https://www.deribit.com/api/v2/public'


def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'BasisResearch/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def price_event_slugs(now):
    slugs=[]
    for offset in (0,1):
        month=(now.month-1+offset)%12+1
        year=now.year+(now.month-1+offset)//12
        period=datetime(year,month,1).strftime('%B-%Y').lower()
        day=now+timedelta(days=offset)
        daily=f'{day.strftime("%B").lower()}-{day.day}-{day.year}'
        for asset in ('bitcoin','ethereum'):
            slugs.extend((f'what-price-will-{asset}-hit-in-{period}',f'{asset}-above-on-{daily}'))
    return slugs


class Collector:
    def __init__(self, engine, monitor=None):
        self.engine = engine
        self.monitor = monitor
        self.stop = threading.Event()
        self.refresh = threading.Event()
        self.thread = None
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='basis-fetch')
        self.equity_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='basis-equity')
        self.last_health = {}
        self.failed = None
        self.failure_kind = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='basis-collector', daemon=True)
        self.thread.start()

    def run(self):
        try:
            asyncio.run(self.main())
        except BaseException as error:
            self.failed = str(error)
            self.failure_kind = 'BASIS_COLLECTOR_FAILURE'
            logging.exception('BASIS collector stopped')

    def health(self, name, state, detail='', force=False):
        now = time.monotonic()
        previous = self.last_health.get(name)
        if force or not previous or previous[0] != state or now-previous[1] > 30:
            self.engine.ingest('basis', 'health', name, dict(state=state, detail=detail))
            self.last_health[name] = state, now

    async def request(self, url):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, get_json, url)

    async def sleep(self, seconds):
        until = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < until:
            await asyncio.sleep(min(1, max(0, until-time.monotonic())))

    async def catalog(self):
        while not self.stop.is_set():
            try:
                rows, errors, urls = [], [], []
                async def page(url, events=False):
                    try:
                        result = await self.request(url); urls.append(url)
                        if events:
                            values = result.get('events', [result] if 'markets' in result and 'slug' in result else [])
                            for event in values:
                                rows.extend(dict(m, eventSlug=event.get('slug'), eventEndDate=event.get('endDate')) for m in event.get('markets', []))
                        else:
                            rows.extend(result if isinstance(result, list) else result.get('markets', result.get('data', [])))
                        return result
                    except Exception as error:
                        errors.append(dict(url=url, error=type(error).__name__))
                        return {}
                await page(GAMMA + '/markets?active=true&closed=false&limit=100&order=volume24hr&ascending=false')
                for tag_name in ('crypto', 'finance', 'stocks'):
                    try:
                        tag = await self.request(GAMMA + '/tags/slug/' + tag_name)
                        cursor = None
                        for _ in range(4 if tag_name == 'crypto' else 2):
                            params = dict(closed='false', limit=100, tag_id=tag['id'])
                            if cursor:
                                params['after_cursor'] = cursor
                            result = await page(GAMMA + '/markets/keyset?' + urllib.parse.urlencode(params))
                            cursor = result.get('next_cursor') if isinstance(result, dict) else None
                            if not cursor:
                                break
                    except Exception as error:
                        errors.append(dict(tag=tag_name, error=type(error).__name__))
                for slug in price_event_slugs(datetime.now(timezone.utc)):
                    await page(f'{GAMMA}/events/slug/{slug}',events=True)
                if not rows:
                    raise ValueError('No catalog rows returned')
                self.engine.ingest('polymarket_gamma', 'catalog', 'public', dict(markets=rows, requested_urls=urls, partial_errors=errors))
                self.health('catalog', 'PARTIAL' if errors else 'OK', f'{len(rows)} source rows; {len(errors)} failed requests', True)
            except Exception as error:
                self.health('catalog', 'ERROR', str(error)[:180], True)
            self.refresh.clear()
            elapsed = 0
            while elapsed < self.engine.config.catalog_seconds and not self.refresh.is_set() and not self.stop.is_set():
                await self.sleep(1); elapsed += 1

    async def options(self, asset):
        while not self.stop.is_set():
            try:
                data = await self.request(f'{DERIBIT}/get_book_summary_by_currency?currency={asset}&kind=option')
                if not isinstance(data.get('result'), list) or not data['result']:
                    raise ValueError('Empty option surface')
                self.engine.ingest('deribit', 'options', asset, data)
                self.health('options:' + asset, 'OK', 'Public option summary snapshot')
            except Exception as error:
                self.health('options:' + asset, 'ERROR', str(error)[:180])
            await self.sleep(self.engine.config.options_seconds)

    async def poly(self):
        import websockets
        while not self.stop.is_set():
            try:
                with self.engine.lock:
                    tokens = sorted({e['yes_token'] for e in self.engine.events.values() if e.get('yes_token')})
                if not tokens:
                    await self.sleep(1); continue
                async with websockets.connect('wss://ws-subscriptions-clob.polymarket.com/ws/market', open_timeout=15, close_timeout=5, ping_interval=20, max_size=16*1024*1024) as socket:
                    await socket.send(json.dumps(dict(assets_ids=tokens, type='market', custom_feature_enabled=True)))
                    subscribed, last_ping = set(tokens), time.monotonic()
                    while not self.stop.is_set():
                        if time.monotonic() - last_ping >= 8:
                            await socket.send('PING'); last_ping = time.monotonic()
                            with self.engine.lock:
                                desired = {e['yes_token'] for e in self.engine.events.values() if e.get('yes_token')}
                            for operation, assets in [('subscribe', desired-subscribed), ('unsubscribe', subscribed-desired)]:
                                if assets:
                                    await socket.send(json.dumps(dict(assets_ids=sorted(assets), operation=operation)))
                            subscribed = desired
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        if message in ('PONG', 'PING'):
                            continue
                        payload = json.loads(message)
                        self.engine.ingest('polymarket_clob', 'pm', 'tokens', payload)
                        self.health('pm', 'OK', f'{len(subscribed)} YES tokens')
            except Exception as error:
                self.health('pm', 'ERROR', f'Reconnecting: {type(error).__name__}: {str(error)[:140]}')
                await self.sleep(3)

    async def spot(self):
        import websockets
        while not self.stop.is_set():
            try:
                async with websockets.connect('wss://advanced-trade-ws.coinbase.com', open_timeout=15, close_timeout=5, ping_interval=20, max_size=4*1024*1024) as socket:
                    await socket.send(json.dumps(dict(type='subscribe', product_ids=['BTC-USD', 'ETH-USD'], channel='ticker')))
                    await socket.send(json.dumps(dict(type='subscribe', channel='heartbeats')))
                    while not self.stop.is_set():
                        try:
                            data = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
                        except asyncio.TimeoutError:
                            continue
                        if data.get('channel') == 'ticker':
                            for event in data.get('events', []):
                                for tick in event.get('tickers', []):
                                    asset = tick.get('product_id', '').split('-')[0]
                                    if asset in ('BTC', 'ETH'):
                                        payload = dict(tick, timestamp=data.get('timestamp'), envelope_sequence=data.get('sequence_num'))
                                        self.engine.ingest('coinbase', 'spot', asset, payload, source_ms=timestamp(data.get('timestamp')))
                            self.health('spot', 'OK', 'Coinbase public ticker')
            except Exception as error:
                self.health('spot', 'ERROR', f'Reconnecting: {type(error).__name__}: {str(error)[:140]}')
                await self.sleep(3)

    async def histories(self):
        while not self.stop.is_set():
            with self.engine.lock:
                windows = {(e['asset'], e.get('window_start')) for e in self.engine.events.values() if e['event_type'] == 'touch' and e['asset'] in ('BTC', 'ETH')}
            for asset, start in sorted(windows, key=str):
                if not start or not 0 <= time.time()*1000-start <= 299*86400000:
                    continue
                try:
                    params = dict(start=datetime.fromtimestamp(start/1000, timezone.utc).isoformat(), end=datetime.now(timezone.utc).isoformat(), granularity=86400)
                    candles = await self.request(f'https://api.exchange.coinbase.com/products/{asset}-USD/candles?' + urllib.parse.urlencode(params))
                    if not isinstance(candles, list):
                        raise ValueError('Invalid candle response')
                    self.engine.ingest('coinbase', 'history', asset, dict(start_ms=start, candles=candles))
                    self.health('history:' + asset, 'OK', 'Daily reference path; UTC boundaries only')
                except Exception as error:
                    self.health('history:' + asset, 'ERROR', str(error)[:180])
            await self.sleep(self.engine.config.history_seconds if windows else 5)

    def yahoo_snapshot(self, asset, cutoffs, starts=()):
        from .equities import snapshot
        return snapshot(asset,cutoffs,starts,self.engine.config.max_expiry_offset_hours)

    async def yahoo(self):
        while not self.stop.is_set():
            if not self.engine.config.yahoo_enabled:
                self.health('yahoo', 'DISABLED', 'Disabled in collection config'); await self.sleep(60); continue
            with self.engine.lock:
                targets = {}
                for e in self.engine.events.values():
                    if e['asset'] and e['asset'] not in ('BTC', 'ETH') and e['event_type'] in ('terminal','touch') and e['expiry']>time.time()*1000:
                        target=targets.setdefault(e['asset'],dict(cutoffs=set(),starts=set()))
                        target['cutoffs'].add(e['expiry'])
                        if e.get('window_start'):target['starts'].add(e['window_start'])
            async def collect(asset,target):
                try:
                    data = await asyncio.get_running_loop().run_in_executor(self.equity_executor, self.yahoo_snapshot, asset, target['cutoffs'], target['starts'])
                    self.engine.ingest('yahoo', 'options', asset, data)
                    for history in data.get('histories',[]):self.engine.ingest('yahoo','history',asset,history)
                    state='PARTIAL' if data.get('partial_errors') else 'OK' if data['chains'] else 'EMPTY'
                    self.health('yahoo:' + asset, state, f"{len(data['chains'])} chains, calls/puts; {data.get('underlying_context',{}).get('market_state','UNKNOWN')}; delay unknown; {data.get('partial_errors',[])}")
                    return state
                except Exception as error:
                    self.health('yahoo:' + asset, 'ERROR', f'{type(error).__name__}: {str(error)[:150]}')
                    return 'ERROR'
            states=await asyncio.gather(*(collect(asset,target) for asset,target in targets.items()))
            state='IDLE' if not targets else 'OK' if all(s=='OK' for s in states) else 'PARTIAL'
            self.health('yahoo', state, f'{len(targets)} equity symbols; {states.count("OK")} complete; delayed snapshots')
            await self.sleep(self.engine.config.yahoo_seconds)

    async def timer(self):
        next_analysis = 0
        next_checkpoint = time.monotonic()+300
        while not self.stop.is_set():
            now = time.monotonic()
            analyze = now >= next_analysis
            if analyze:
                next_analysis = now + self.engine.config.analysis_seconds
            if self.engine.store.path != ':memory:':
                free = shutil.disk_usage(self.engine.store.path).free / 1024**2
                if free < self.engine.config.min_free_mb:
                    self.health('recorder', 'ERROR', 'Disk reserve reached; collection stopped', True)
                    self.failed = 'Disk reserve reached'
                    self.failure_kind = 'DISK_SAFETY_STOP'
                    self.stop.set(); return
            self.engine.ingest('basis', 'timer', 'clock', dict(analyze=analyze))
            if now >= next_checkpoint:
                if self.monitor:
                    self.monitor.checkpoint()
                else:
                    self.engine.save_checkpoint()
                next_checkpoint = now+300
            await self.sleep(5)

    async def main(self):
        tasks = [asyncio.create_task(f()) for f in (self.catalog, self.poly, self.spot, self.histories, self.yahoo, self.timer)]
        tasks += [asyncio.create_task(self.options(asset)) for asset in ('BTC', 'ETH')]
        try:
            while not self.stop.is_set():
                for task in tasks:
                    if task.done():
                        error = task.exception()
                        raise RuntimeError(f'Collector task stopped: {error}')
                await asyncio.sleep(1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=20)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.equity_executor.shutdown(wait=False, cancel_futures=True)
