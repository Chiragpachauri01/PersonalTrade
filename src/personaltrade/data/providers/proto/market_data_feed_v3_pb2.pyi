from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Type(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    initial_feed: _ClassVar[Type]
    live_feed: _ClassVar[Type]
    market_info: _ClassVar[Type]

class RequestMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ltpc: _ClassVar[RequestMode]
    full_d5: _ClassVar[RequestMode]
    option_greeks: _ClassVar[RequestMode]
    full_d30: _ClassVar[RequestMode]

class MarketStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PRE_OPEN_START: _ClassVar[MarketStatus]
    PRE_OPEN_END: _ClassVar[MarketStatus]
    NORMAL_OPEN: _ClassVar[MarketStatus]
    NORMAL_CLOSE: _ClassVar[MarketStatus]
    CLOSING_START: _ClassVar[MarketStatus]
    CLOSING_END: _ClassVar[MarketStatus]
initial_feed: Type
live_feed: Type
market_info: Type
ltpc: RequestMode
full_d5: RequestMode
option_greeks: RequestMode
full_d30: RequestMode
PRE_OPEN_START: MarketStatus
PRE_OPEN_END: MarketStatus
NORMAL_OPEN: MarketStatus
NORMAL_CLOSE: MarketStatus
CLOSING_START: MarketStatus
CLOSING_END: MarketStatus

class LTPC(_message.Message):
    __slots__ = ("ltp", "ltt", "ltq", "cp")
    LTP_FIELD_NUMBER: _ClassVar[int]
    LTT_FIELD_NUMBER: _ClassVar[int]
    LTQ_FIELD_NUMBER: _ClassVar[int]
    CP_FIELD_NUMBER: _ClassVar[int]
    ltp: float
    ltt: int
    ltq: int
    cp: float
    def __init__(self, ltp: _Optional[float] = ..., ltt: _Optional[int] = ..., ltq: _Optional[int] = ..., cp: _Optional[float] = ...) -> None: ...

class MarketLevel(_message.Message):
    __slots__ = ("bidAskQuote",)
    BIDASKQUOTE_FIELD_NUMBER: _ClassVar[int]
    bidAskQuote: _containers.RepeatedCompositeFieldContainer[Quote]
    def __init__(self, bidAskQuote: _Optional[_Iterable[_Union[Quote, _Mapping]]] = ...) -> None: ...

class MarketOHLC(_message.Message):
    __slots__ = ("ohlc",)
    OHLC_FIELD_NUMBER: _ClassVar[int]
    ohlc: _containers.RepeatedCompositeFieldContainer[OHLC]
    def __init__(self, ohlc: _Optional[_Iterable[_Union[OHLC, _Mapping]]] = ...) -> None: ...

class Quote(_message.Message):
    __slots__ = ("bidQ", "bidP", "askQ", "askP")
    BIDQ_FIELD_NUMBER: _ClassVar[int]
    BIDP_FIELD_NUMBER: _ClassVar[int]
    ASKQ_FIELD_NUMBER: _ClassVar[int]
    ASKP_FIELD_NUMBER: _ClassVar[int]
    bidQ: int
    bidP: float
    askQ: int
    askP: float
    def __init__(self, bidQ: _Optional[int] = ..., bidP: _Optional[float] = ..., askQ: _Optional[int] = ..., askP: _Optional[float] = ...) -> None: ...

class OptionGreeks(_message.Message):
    __slots__ = ("delta", "theta", "gamma", "vega", "rho")
    DELTA_FIELD_NUMBER: _ClassVar[int]
    THETA_FIELD_NUMBER: _ClassVar[int]
    GAMMA_FIELD_NUMBER: _ClassVar[int]
    VEGA_FIELD_NUMBER: _ClassVar[int]
    RHO_FIELD_NUMBER: _ClassVar[int]
    delta: float
    theta: float
    gamma: float
    vega: float
    rho: float
    def __init__(self, delta: _Optional[float] = ..., theta: _Optional[float] = ..., gamma: _Optional[float] = ..., vega: _Optional[float] = ..., rho: _Optional[float] = ...) -> None: ...

class OHLC(_message.Message):
    __slots__ = ("interval", "open", "high", "low", "close", "vol", "ts")
    INTERVAL_FIELD_NUMBER: _ClassVar[int]
    OPEN_FIELD_NUMBER: _ClassVar[int]
    HIGH_FIELD_NUMBER: _ClassVar[int]
    LOW_FIELD_NUMBER: _ClassVar[int]
    CLOSE_FIELD_NUMBER: _ClassVar[int]
    VOL_FIELD_NUMBER: _ClassVar[int]
    TS_FIELD_NUMBER: _ClassVar[int]
    interval: str
    open: float
    high: float
    low: float
    close: float
    vol: int
    ts: int
    def __init__(self, interval: _Optional[str] = ..., open: _Optional[float] = ..., high: _Optional[float] = ..., low: _Optional[float] = ..., close: _Optional[float] = ..., vol: _Optional[int] = ..., ts: _Optional[int] = ...) -> None: ...

class MarketFullFeed(_message.Message):
    __slots__ = ("ltpc", "marketLevel", "optionGreeks", "marketOHLC", "atp", "vtt", "oi", "iv", "tbq", "tsq")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    MARKETLEVEL_FIELD_NUMBER: _ClassVar[int]
    OPTIONGREEKS_FIELD_NUMBER: _ClassVar[int]
    MARKETOHLC_FIELD_NUMBER: _ClassVar[int]
    ATP_FIELD_NUMBER: _ClassVar[int]
    VTT_FIELD_NUMBER: _ClassVar[int]
    OI_FIELD_NUMBER: _ClassVar[int]
    IV_FIELD_NUMBER: _ClassVar[int]
    TBQ_FIELD_NUMBER: _ClassVar[int]
    TSQ_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    marketLevel: MarketLevel
    optionGreeks: OptionGreeks
    marketOHLC: MarketOHLC
    atp: float
    vtt: int
    oi: float
    iv: float
    tbq: float
    tsq: float
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., marketLevel: _Optional[_Union[MarketLevel, _Mapping]] = ..., optionGreeks: _Optional[_Union[OptionGreeks, _Mapping]] = ..., marketOHLC: _Optional[_Union[MarketOHLC, _Mapping]] = ..., atp: _Optional[float] = ..., vtt: _Optional[int] = ..., oi: _Optional[float] = ..., iv: _Optional[float] = ..., tbq: _Optional[float] = ..., tsq: _Optional[float] = ...) -> None: ...

class IndexFullFeed(_message.Message):
    __slots__ = ("ltpc", "marketOHLC")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    MARKETOHLC_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    marketOHLC: MarketOHLC
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., marketOHLC: _Optional[_Union[MarketOHLC, _Mapping]] = ...) -> None: ...

class FullFeed(_message.Message):
    __slots__ = ("marketFF", "indexFF")
    MARKETFF_FIELD_NUMBER: _ClassVar[int]
    INDEXFF_FIELD_NUMBER: _ClassVar[int]
    marketFF: MarketFullFeed
    indexFF: IndexFullFeed
    def __init__(self, marketFF: _Optional[_Union[MarketFullFeed, _Mapping]] = ..., indexFF: _Optional[_Union[IndexFullFeed, _Mapping]] = ...) -> None: ...

class FirstLevelWithGreeks(_message.Message):
    __slots__ = ("ltpc", "firstDepth", "optionGreeks", "vtt", "oi", "iv")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    FIRSTDEPTH_FIELD_NUMBER: _ClassVar[int]
    OPTIONGREEKS_FIELD_NUMBER: _ClassVar[int]
    VTT_FIELD_NUMBER: _ClassVar[int]
    OI_FIELD_NUMBER: _ClassVar[int]
    IV_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    firstDepth: Quote
    optionGreeks: OptionGreeks
    vtt: int
    oi: float
    iv: float
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., firstDepth: _Optional[_Union[Quote, _Mapping]] = ..., optionGreeks: _Optional[_Union[OptionGreeks, _Mapping]] = ..., vtt: _Optional[int] = ..., oi: _Optional[float] = ..., iv: _Optional[float] = ...) -> None: ...

class Feed(_message.Message):
    __slots__ = ("ltpc", "fullFeed", "firstLevelWithGreeks", "requestMode")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    FULLFEED_FIELD_NUMBER: _ClassVar[int]
    FIRSTLEVELWITHGREEKS_FIELD_NUMBER: _ClassVar[int]
    REQUESTMODE_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    fullFeed: FullFeed
    firstLevelWithGreeks: FirstLevelWithGreeks
    requestMode: RequestMode
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., fullFeed: _Optional[_Union[FullFeed, _Mapping]] = ..., firstLevelWithGreeks: _Optional[_Union[FirstLevelWithGreeks, _Mapping]] = ..., requestMode: _Optional[_Union[RequestMode, str]] = ...) -> None: ...

class MarketInfo(_message.Message):
    __slots__ = ("segmentStatus",)
    class SegmentStatusEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: MarketStatus
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[MarketStatus, str]] = ...) -> None: ...
    SEGMENTSTATUS_FIELD_NUMBER: _ClassVar[int]
    segmentStatus: _containers.ScalarMap[str, MarketStatus]
    def __init__(self, segmentStatus: _Optional[_Mapping[str, MarketStatus]] = ...) -> None: ...

class FeedResponse(_message.Message):
    __slots__ = ("type", "feeds", "currentTs", "marketInfo")
    class FeedsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: Feed
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[Feed, _Mapping]] = ...) -> None: ...
    TYPE_FIELD_NUMBER: _ClassVar[int]
    FEEDS_FIELD_NUMBER: _ClassVar[int]
    CURRENTTS_FIELD_NUMBER: _ClassVar[int]
    MARKETINFO_FIELD_NUMBER: _ClassVar[int]
    type: Type
    feeds: _containers.MessageMap[str, Feed]
    currentTs: int
    marketInfo: MarketInfo
    def __init__(self, type: _Optional[_Union[Type, str]] = ..., feeds: _Optional[_Mapping[str, Feed]] = ..., currentTs: _Optional[int] = ..., marketInfo: _Optional[_Union[MarketInfo, _Mapping]] = ...) -> None: ...
