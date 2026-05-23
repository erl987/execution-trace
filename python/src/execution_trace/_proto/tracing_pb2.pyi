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
TRACE_EVENT_SOURCE_TYPE_UNSPECIFIED: TraceEventSourceType
ISR: TraceEventSourceType
TASK: TraceEventSourceType
TRACE_EVENT_TYPE_UNSPECIFIED: TraceEventType
SPAN_START: TraceEventType
SPAN_END: TraceEventType
MARKER: TraceEventType

class TraceEvent(_message.Message):
    __slots__ = ("timestamp_ns", "name", "source_type", "event_type", "sequence", "priority", "relative_deadline_ms", "marker_value")
    TIMESTAMP_NS_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    EVENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    RELATIVE_DEADLINE_MS_FIELD_NUMBER: _ClassVar[int]
    MARKER_VALUE_FIELD_NUMBER: _ClassVar[int]
    timestamp_ns: int
    name: str
    source_type: TraceEventSourceType
    event_type: TraceEventType
    sequence: int
    priority: int
    relative_deadline_ms: float
    marker_value: int
    def __init__(self, timestamp_ns: _Optional[int] = ..., name: _Optional[str] = ..., source_type: _Optional[_Union[TraceEventSourceType, str]] = ..., event_type: _Optional[_Union[TraceEventType, str]] = ..., sequence: _Optional[int] = ..., priority: _Optional[int] = ..., relative_deadline_ms: _Optional[float] = ..., marker_value: _Optional[int] = ...) -> None: ...
