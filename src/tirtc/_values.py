from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class _SDKValue:
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        raise TypeError(f"{cls.__name__} values are created by the SDK")


class ConnectionState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class OutputState(StrEnum):
    IDLE = "idle"
    BUFFERING = "buffering"
    DELIVERING = "delivering"
    FAILED = "failed"
    PAUSED = "paused"
    COMPLETED = "completed"


class OutputBufferStrategy(StrEnum):
    AUTOMATIC = "automatic"
    NO_BUFFER = "no_buffer"


class AudioProcessingLevel(StrEnum):
    DISABLED = "disabled"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VideoDecoderPreference(StrEnum):
    AUTO = "auto"
    SOFTWARE = "software"
    HARDWARE = "hardware"


class AudioSampleFormat(StrEnum):
    NONE = "none"
    INT16_LE_INTERLEAVED = "int16_le_interleaved"


class PixelFormat(StrEnum):
    NONE = "none"
    I420 = "i420"
    NV12 = "nv12"
    RGBA8888 = "rgba8888"


class AudioCodec(StrEnum):
    NONE = "none"
    G711A = "g711a"
    AAC = "aac"
    PCM = "pcm"
    OPUS = "opus"
    AMR = "amr"


class VideoCodec(StrEnum):
    NONE = "none"
    H264 = "h264"
    H265 = "h265"
    MJPEG = "mjpeg"


class AudioBitstreamFormat(StrEnum):
    NONE = "none"
    G711A_PACKET = "g711a_packet"
    AAC_ADTS = "aac_adts"
    AAC_RAW_ACCESS_UNIT = "aac_raw_access_unit"
    PCM_INT16_LE_INTERLEAVED = "pcm_int16_le_interleaved"
    OPUS_PACKET = "opus_packet"
    AMR_NB_FRAME = "amr_nb_frame"


class VideoBitstreamFormat(StrEnum):
    NONE = "none"
    H264_ANNEX_B = "h264_annex_b"
    H265_ANNEX_B = "h265_annex_b"
    MJPEG_JFIF = "mjpeg_jfif"


@dataclass(frozen=True, slots=True)
class OutputBufferOptions:
    strategy: OutputBufferStrategy = OutputBufferStrategy.AUTOMATIC
    max_buffer_watermark: timedelta | None = None


@dataclass(frozen=True, slots=True, init=False)
class AudioFrame(_SDKValue):
    data: memoryview
    pts: timedelta
    source_time: datetime | None
    sample_format: AudioSampleFormat
    sample_rate_hz: int
    channels: int
    samples_per_channel: int
    discontinuity: bool


@dataclass(frozen=True, slots=True, init=False)
class VideoPlane(_SDKValue):
    stride: int
    data: memoryview


@dataclass(frozen=True, slots=True, init=False)
class VideoFrame(_SDKValue):
    pts: timedelta
    source_time: datetime | None
    pixel_format: PixelFormat
    width: int
    height: int
    planes: tuple[VideoPlane, ...]
    discontinuity: bool


@dataclass(frozen=True, slots=True, init=False)
class EncodedAudioFrame(_SDKValue):
    data: memoryview
    codec_config: memoryview
    pts: timedelta
    source_time: datetime | None
    codec: AudioCodec
    bitstream_format: AudioBitstreamFormat
    sample_rate_hz: int
    channels: int
    discontinuity: bool


@dataclass(frozen=True, slots=True, init=False)
class EncodedVideoFrame(_SDKValue):
    data: memoryview
    codec_config: memoryview
    pts: timedelta
    source_time: datetime | None
    codec: VideoCodec
    bitstream_format: VideoBitstreamFormat
    width: int
    height: int
    key_frame: bool
    discontinuity: bool


def _value(value_type: type, **fields: object):
    value = object.__new__(value_type)
    for name, field_value in fields.items():
        object.__setattr__(value, name, field_value)
    return value


_CONNECTION_STATES = tuple(ConnectionState)
_OUTPUT_STATES = tuple(OutputState)
_AUDIO_SAMPLE_FORMATS = tuple(AudioSampleFormat)
_PIXEL_FORMATS = tuple(PixelFormat)
_AUDIO_CODECS = (
    AudioCodec.NONE,
    AudioCodec.G711A,
    AudioCodec.AAC,
    AudioCodec.PCM,
    AudioCodec.OPUS,
    AudioCodec.AMR,
)
_VIDEO_CODECS_BY_NATIVE = {
    0: VideoCodec.NONE,
    65: VideoCodec.H264,
    66: VideoCodec.H265,
    67: VideoCodec.MJPEG,
}
_AUDIO_BITSTREAM_FORMATS = tuple(AudioBitstreamFormat)
_VIDEO_BITSTREAM_FORMATS = tuple(VideoBitstreamFormat)
