import asyncio as aio
import json
import aiohttp
import pandas as pd
import datetime as dt
import urllib.parse
import re
from typing import Optional, Union, NamedTuple, Iterable, Sequence, Mapping, TypeVar, List, Awaitable
from types import TracebackType, MappingProxyType
import math

T = TypeVar('T')

exchanges = MappingProxyType({
    'A':'NYSE MKT',
    'B':'NASDAQ OMX BX',
    'C':'National Stock Exchange',
    'D':'FINRA ADF',
    'E':'Market Independent (Generated by Nasdaq SIP)',
    'F':'Mutual Funds/Money Markets (NASDAQ)',
    'I':'International Securities Exchange',
    'J':'Direct Edge A',
    'K':'Direct Edge X',
    'M':'Chicago Stock Exchange',
    'N':'NYSE',
    'P':'NYSE Arca',
    'Q':'NASDAQ OMX',
    'S':'NASDAQ Small Cap',
    'T':'NASDAQ Int',
    'U':'OTCBB',
    'V':'OTC other',
    'W':'CBOE',
    'X':'NASDAQ OMX PSX',
    'G':'GLOBEX',
    'Y':'BATS Y-Exchange',
    'Z':'BATS'
})

option_exchanges = MappingProxyType({
    'A':'NYSE Amex Options',
    'B':'BOX Options Exchange',
    'C':'Chicago Board Options Exchange (CBOE)',
    'H':'ISE Gemini',
    'I':'International Securities Exchange (ISE)',
    'M':'MIAX Options Exchange',
    'N':'NYSE Arca Options',
    'O':'Options Price Reporting Authority (OPRA)',
    'P':'MIAX PEARL',
    'Q':'NASDAQ Options Market',
    'T':'NASDAQ OMX BX',
    'W':'C2 Options Exchange',
    'X':'NASDAQ OMX PHLX',
    'Z':'BATS Options Market'
})


def _from_iso_time(t: Union[str, float]) -> dt.time:
    if pd.isna(t):
        return t
    return dt.time(*map(int, t.split(':')))


def _convert_datetime(datetime: Union[dt.date, dt.datetime, str]) -> str:
    if isinstance(datetime, str):
        return datetime
    return datetime.isoformat()


def _ensure_list(item: T) -> List[T]:
    if isinstance(item, Sequence):
        return item
    else:
        return [item]


def _synchronously(future: Awaitable[T]) -> T:
    loop = aio.get_event_loop()
    return loop.run_until_complete(future)


class Clock(NamedTuple):
    date: dt.datetime
    description: str
    next_change: dt.datetime
    next_state: str
    state: str


class TimeRange(NamedTuple):
    start: dt.time
    end: dt.time


class Calendar(NamedTuple):
    days: pd.DataFrame
    year: int
    month: int


class Session(object):

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def __aenter__(self) -> 'Session':
        return self

    async def __aexit__(
        self,
        exc_type: type,
        exc: BaseException,
        tb: TracebackType
    ):
        await self.close()
        if exc:
            raise exc

    async def close(self):
        await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Union[Iterable[Sequence[str]], Mapping[str, str]]]=None
    ) -> Optional[dict]:
        response = await self._session.request(
            method,
            'https://sandbox.tradier.com{}'.format(path),
            params=params
        )
        if not (200 <= response.status < 300):
            raise ValueError('Received status code: {}'.format(response.status))
        return await response.json(encoding='utf-8')

    async def quotes(self, symbols: Iterable[str]) -> Optional[pd.DataFrame]:
        symbol_str = ','.join(symbols)
        response = (await self._request(
            'GET',
            '/v1/markets/quotes?symbols={}'.format(symbol_str)
        )).get('quotes', None)
        if response is None:
            return None
        frame = pd.DataFrame(_ensure_list(response['quote']))
        frame.set_index(['symbol'], inplace=True)
        for key in ('trade_date', 'bid_date', 'ask_date'):
            if key in frame:
                frame[key] = pd.to_datetime(frame[key] * 1e6)
        if 'expiration_date' in frame:
            frame['expiration_date'] = pd.to_datetime(frame['expiration_date'])
        return frame

    async def timesales(
        self,
        symbol: str,
        interval: Optional[str]=None,
        start: Optional[Union[dt.datetime, str]]=None,
        end: Optional[Union[dt.datetime, str]]=None,
        session_filter: Optional[str]=None
    ) -> Optional[pd.DataFrame]:
        params = [
            (k, v) for k, v in (
                ('symbol', symbol),
                ('interval', interval),
                ('session_filter', session_filter),
                ('start', start and _convert_datetime(start)),
                ('end', end and _convert_datetime(end))
            ) if v is not None
        ]
        response = (await self._request(
            'GET',
            '/v1/markets/timesales',
            params
        )).get('series', None)
        if response is None:
            return None
        frame = pd.DataFrame(_ensure_list(response['data']))
        frame['time'] = pd.to_datetime(frame['timestamp'] * 1e9)
        frame.drop('timestamp', 1, inplace=True)
        frame.set_index(['time'], inplace=True)
        return frame

    async def option_chain(
        self,
        symbol: str,
        expiration: Union[dt.date, str]
    ) -> Optional[pd.DataFrame]:
        path = '/v1/markets/options/chains'
        params = (
            ('symbol', symbol),
            ('expiration', _convert_datetime(expiration))
        )
        response = (await self._request('GET', path, params)).get('options', None)
        if response is None:
            return None
        frame = pd.DataFrame(_ensure_list(response['option']))
        frame.set_index(['symbol'], inplace=True)
        return frame

    async def option_strikes(
        self, symbol: str,
        expiration: Union[dt.date, str]
    ) -> pd.Series:
        path = '/v1/markets/options/strikes'
        params = (
            ('symbol', symbol),
            ('expiration', _convert_datetime(expiration))
        )
        response = (await self._request('GET', path, params)).get('strikes', None)
        if response is None:
            return None
        return pd.Series(response['strike'], name='strikes')

    async def option_expirations(self, symbol: str) -> Optional[pd.Series]:
        path = '/v1/markets/options/expirations?symbol={}'.format(symbol)
        response = (await self._request('GET', path)).get('expirations', None)
        if response is None:
            return None
        return pd.Series(pd.to_datetime(response['date']), name='date')

    async def historical_pricing(
        self,
        symbol: str,
        interval: Optional[str]=None,
        start: Optional[Union[dt.date, str]]=None,
        end: Optional[Union[dt.date, str]]=None
    ) -> Optional[pd.DataFrame]:
        params = [
            (k, v) for k, v in (
                ('symbol', symbol),
                ('interval', interval),
                ('start', start and _convert_datetime(start)),
                ('end', end and _convert_datetime(end))
            ) if v is not None
        ]
        response = (await self._request('GET', '/v1/markets/history', params)).get('history', None)
        if response is None:
            return None
        frame = pd.DataFrame(_ensure_list(response['day']))
        frame['date'] = pd.to_datetime(frame['date'])
        frame.set_index(['date'], inplace=True)
        return frame

    async def clock(self) -> Optional[Clock]:
        response = (await self._request('GET', '/v1/markets/clock')).get('clock', None)
        if response is None:
            return None
        date = pd.to_datetime(response['timestamp'] * 1e9)
        next_time = dt.time(*map(int, response['next_change'].split(':')))
        next_date = date.replace(
            hour=next_time.hour,
            minute=next_time.minute,
            second=next_time.second,
            microsecond=next_time.microsecond
        )
        if next_date < date:
            next_date += dt.timedelta(days=1)
        return Clock(
            date=date,
            description=response['description'],
            next_change=next_date,
            next_state=response['state'],
            state=response['state'],
        )

    async def calendar(
        self,
        date: Optional[Union[dt.date, str, int, Sequence[int]]]
    ) -> Optional[Calendar]:
        if isinstance(date, dt.date):
            year = str(date.year)
            month = str(date.month)
        elif isinstance(date, str):
            ym = date.split('-', 2)
            year = ym[0]
            if len(ym) == 1:
                month = None
            else:
                month = ym[1]
        elif isinstance(date, Sequence):
            year = str(date[0])
            if len(date) == 1:
                month = None
            else:
                month = str(date[1])
        elif isinstance(date, int):
            year = str(date)
            month = None
        elif date is None:
            year = None
            month = None
        params = [
            (k, v)
            for k, v in (
                ('year', year),
                ('month', month)
            )
            if v is not None
        ]
        response = (await self._request('GET', '/v1/markets/calendar', params)).get('calendar', None)
        if response is None:
            return None
        days = pd.DataFrame(_ensure_list(response['days']['day']))
        days['date'] = pd.to_datetime(days['date'])

        def to_time_range(value):
            if pd.isna(value):
                return value
            return TimeRange(
                start=_from_iso_time(value['start']),
                end=_from_iso_time(value['end'])
            )

        for key in ('premarket', 'open', 'postmarket'):
            days[key] = days[key].apply(to_time_range)

        days.set_index(['date'], inplace=True)

        def convert(obj):
            if obj is None:
                return None
            return int(obj)

        return Calendar(
            days=days,
            year=convert(response.get('year', None)),
            month=convert(response.get('month', None))
        )

    async def search(self, query: str, indexes: Optional[bool]=False) -> Optional[pd.DataFrame]:
        params = [('q', query)]
        if indexes:
            params += ('indexes', 'true')
        response = (await self._request('GET', '/v1/markets/search', params)).get('securities', None)
        if response is None:
            return None
        frame = pd.DataFrame(_ensure_list(response['security']))
        frame.set_index(['symbol'], inplace=True)
        return frame

    async def lookup(
        self,
        symbol: Optional[str]=None,
        exchanges: Optional[Iterable[str]]=None,
        types: Optional[Iterable[str]]=None
    ) -> Optional[pd.DataFrame]:
        params = [
            (k, v) for k, v in (
                ('q', symbol),
                ('exchanges', exchanges and ','.join(exchanges)),
                ('types', types and ','.join(types))
            ) if v is not None
        ]
        if not kvs:
            raise ValueError('An argument must be provided')
        response = (await self._request('GET', '/v1/markets/lookup', params)).get('securities', None)
        if response is None:
            return None
        frame = pd.DataFrame(_ensure_list(response['security']))
        frame.set_index(['symbol'], inplace=True)
        return frame


class AsyncClient(object):

    def __init__(self, token: str):
        self._token = token

    def session(self) -> Session:
        return Session(
            aiohttp.ClientSession(
                headers=[
                    ('Authorization', 'Bearer {}'.format(self._token)),
                    ('Accept', 'application/json')
                ]
            )
        )

    async def quotes(self, symbols: Iterable[str]) -> Optional[pd.DataFrame]:
        async with self.session() as session:
            return await session.quotes(symbols)

    async def timesales(
        self,
        symbol: str,
        interval: Optional[str]=None,
        start: Optional[Union[dt.datetime, str]]=None,
        end: Optional[Union[dt.datetime, str]]=None,
        session_filter: Optional[str]=None
    ) -> Optional[pd.DataFrame]:
        async with self.session() as session:
            return await session.timesales(symbol, interval, start, end, session_filter)

    async def option_chain(self, symbol: str, expiration: dt.date) -> Optional[pd.DataFrame]:
        async with self.session() as session:
            return await session.option_chain(symbol, expiration)

    async def option_strikes(self, symbol: str, expiration: dt.date) -> Optional[pd.Series]:
        async with self.session() as session:
            return await session.option_strikes(symbol, expiration)

    async def option_expirations(self, symbol: str) -> Optional[pd.Series]:
        async with self.session() as session:
            return await session.option_expirations(symbol)

    async def historical_pricing(
        self,
        symbol: str,
        interval: Optional[str]=None,
        start: Optional[dt.date]=None,
        end: Optional[dt.date]=None
    ) -> Optional[pd.DataFrame]:
        async with self.session() as session:
            return await session.historical_pricing(
                symbol,
                interval,
                start,
                end
            )

    async def clock(self) -> Optional[Clock]:
        async with self.session() as session:
            return await session.clock()

    async def calendar(
        self,
        date: Optional[Union[dt.date, dt.datetime, str, int, Sequence[int]]]
    ) -> Optional[Calendar]:
        async with self.session() as session:
            return await session.calendar(date)

    async def search(
        self,
        query: str,
        indexes: Optional[bool]=None
    ) -> Optional[pd.DataFrame]:
        async with self.session() as session:
            return await session.search(query, indexes)

    async def lookup(
        self,
        symbol: Optional[str]=None,
        exchanges: Optional[Iterable[str]]=None,
        types: Optional[Iterable[str]]=None
    ) -> Optional[pd.DataFrame]:
        async with self.session() as session:
            return await session.lookup(symbol, exchanges, types)


class SyncClient(object):

    def __init__(self, token: Union[str, bytes]):
        self._tradier = AsyncClient(token)

    def quotes(self, symbols: Iterable[str]) -> Optional[pd.DataFrame]:
        return _synchronously(self._tradier.quotes(symbols))

    def timesales(
        self,
        symbol: str,
        interval: Optional[str]=None,
        start: Optional[Union[dt.datetime, str]]=None,
        end: Optional[Union[dt.datetime, str]]=None,
        session_filter: Optional[str]=None
    ) -> Optional[pd.DataFrame]:
        return _synchronously(self._tradier.timesales(
            symbol, interval, start, end, session_filter
        ))

    def option_chain(
        self,
        symbol: str,
        expiration: Union[dt.date, str]
    ) -> Optional[pd.DataFrame]:
        return _synchronously(
            self._tradier.option_chain(symbol, expiration)
        )

    def option_strikes(
        self,
        symbol: str,
        expiration: Union[dt.date, str]
    ) -> Optional[pd.Series]:
        return _synchronously(
            self._tradier.option_strikes(symbol, expiration)
        )

    def option_expirations(self, symbol: str) -> Optional[pd.Series]:
        return _synchronously(self._tradier.option_expirations(symbol))

    def historical_pricing(
        self,
        symbol: str,
        interval: Optional[str]=None,
        start: Optional[Union[dt.date, str]]=None,
        end: Optional[Union[dt.date, str]]=None
    ) -> Optional[pd.DataFrame]:
        return _synchronously(self._tradier.historical_pricing(
            symbol, interval, start, end
        ))

    def clock(self) -> Optional[Clock]:
        return _synchronously(self._tradier.clock())

    def calendar(
        self,
        date: Optional[Union[dt.date, dt.datetime, str, int, Sequence[int]]]
    ) -> Optional[Calendar]:
        return _synchronously(self._tradier.calendar(date))

    def search(
        self,
        query: str,
        indexes: Optional[bool]=False
    ) -> Optional[pd.DataFrame]:
        return _synchronously(self._tradier.search(query, indexes))

    def lookup(
        self,
        symbol: Optional[str]=None,
        exchanges: Optional[Iterable[str]]=None,
        types: Optional[Iterable[str]]=None
    ) -> Optional[pd.DataFrame]:
        return _synchronously(
            self._tradier.lookup(symbol, exchanges, types)
        )
