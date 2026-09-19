from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TraceEventSourceType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRACE_EVENT_SOURCE_TYPE_UNSPECIFIED: _ClassVar[TraceEventSourceType]
    ISR: _ClassVar[TraceEventSourceType]
    TASK: _ClassVar[TraceEventSourceType]

class TraceEventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRACE_EVENT_TYPE_UNSPECIFIED: _ClassVar[TraceEventType]
    SPAN_START: _ClassVar[TraceEventType]
    SPAN_END: _ClassVar[TraceEventType]
    MARKER: _ClassVar[TraceEventType]
    NAME_REGISTERED: _ClassVar[TraceEventType]
    TRACE_START: _ClassVar[TraceEventType]

class TimeBase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NANOSECONDS: _ClassVar[TimeBase]
    CYCLES: _ClassVar[TimeBase]
TRACE_EVENT_SOURCE_TYPE_UNSPECIFIED: TraceEventSourceType
ISR: TraceEventSourceType
TASK: TraceEventSourceType
TRACE_EVENT_TYPE_UNSPECIFIED: TraceEventType
SPAN_START: TraceEventType
SPAN_END: TraceEventType
MARKER: TraceEventType
NAME_REGISTERED: TraceEventType
TRACE_START: TraceEventType
NANOSECONDS: TimeBase
CYCLES: TimeBase

class TraceFrame(_message.Message):
    __slots__ = ("timestamp_ticks", "name_id", "event_type", "sequence", "marker_value", "name", "source_type", "priority", "relative_deadline_ms", "timebase", "core_frequency_hz", "source_mask")
    TIMESTAMP_TICKS_FIELD_NUMBER: _ClassVar[int]
    NAME_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    MARKER_VALUE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    RELATIVE_DEADLINE_MS_FIELD_NUMBER: _ClassVar[int]
    TIMEBASE_FIELD_NUMBER: _ClassVar[int]
    CORE_FREQUENCY_HZ_FIELD_NUMBER: _ClassVar[int]
    SOURCE_MASK_FIELD_NUMBER: _ClassVar[int]
    timestamp_ticks: int
    name_id: int
    event_type: TraceEventType
    sequence: int
    marker_value: int
    name: str
    source_type: TraceEventSourceType
    priority: int
    relative_deadline_ms: float
    timebase: TimeBase
    core_frequency_hz: int
    source_mask: int
    def __init__(self, timestamp_ticks: _Optional[int] = ..., name_id: _Optional[int] = ..., event_type: _Optional[_Union[TraceEventType, str]] = ..., sequence: _Optional[int] = ..., marker_value: _Optional[int] = ..., name: _Optional[str] = ..., source_type: _Optional[_Union[TraceEventSourceType, str]] = ..., priority: _Optional[int] = ..., relative_deadline_ms: _Optional[float] = ..., timebase: _Optional[_Union[TimeBase, str]] = ..., core_frequency_hz: _Optional[int] = ..., source_mask: _Optional[int] = ...) -> None: ...
