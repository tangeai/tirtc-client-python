# TiRTC Raw Dump Format

TiRTC raw dumps preserve the bytes observed by Runtime before media normalization or decoding. A
successful `stop` returns a cache-managed ZIP path, size, SHA-256 digest, capture ID, completeness,
and stop reason. Copy the ZIP before a later capture or successful log upload if it must be kept.

The same archive can be attached internally by the existing logging upload API. The raw dump API
does not expose a second log-package export operation. A successful upload removes the associated
cache copy; a failed upload keeps it for retry.

## Archive layout

```text
raw-dump.zip
├── manifest.json
├── objects/                          # cloud_storage only
│   └── object-0001/attempt-0001/
│       ├── object.tstr               # complete response when available
│       ├── object.part               # contiguous evidence bytes for an incomplete attempt
│       └── ranges.csv
└── streams/
    ├── audio-13/0001/                # v1, or v2 downlink
    │   ├── stream.aac
    │   ├── index.csv
    │   └── codec-config.bin          # only when observed from the source
    └── uplink-audio-14/              # v2 uplink
        ├── captured.pcm
        ├── captured.index.csv
        ├── render-reference.pcm
        ├── render-reference.index.csv
        ├── processed.pcm
        ├── processed.index.csv
        ├── encoded.opus
        └── encoded.index.csv
```

`manifest.json` is UTF-8 JSON with `format="tirtc.raw-dump"`. Downlink-only RTC captures and Cloud
Storage captures use integer `version=1`. An RTC capture with at least one selected uplink audio
source uses `version=2`, including when no uplink samples were observed. The
[JSON schema](raw-dump-schema.json) defines both machine-readable contracts. Byte counts, packet
counters, durations, offsets, and sequence values that can exceed a 32-bit range are unsigned
decimal strings. V1 source timestamps are also unsigned; v2 uplink PTS is signed. Missing source
facts use `null` plus a reason when the
schema provides one; inferred media configuration is never labelled as source configuration.
When `runtime_version`, `sdk_version`, or `nano_version` is `null`, the matching
`identity_missing_reasons` entry explains which product boundary could not provide it.

The `source_kind` is `rtc` or `cloud_storage`. RTC stream IDs retain their received values in the
range 0 through 15. Cloud Storage channel IDs retain values in the range 0 through 255. Media kind
and ID together identify a selected source. An empty capture is valid: it contains `manifest.json`,
uses `empty=true`, and reports zero written packets.

In v2, every `selected_sources` item additionally has `direction="downlink"` or `"uplink"`.
Only audio can be selected for uplink. Existing downlink `streams` retain their v1 segments and add
`direction="downlink"`. Each observed uplink source has one stream with
`media_kind="audio"`, `direction="uplink"`, its `source_id`, and independently populated `stages`.
The manifest stage name is `render_reference`; its filename uses `render-reference`.

The `capture_complete` field only says whether Runtime saved every record admitted between the
capture start and its stopping barrier. It does not claim that the device, network, or complete
Cloud Storage recording was available. Cloud object completeness, response ranges, parse outcome,
and capture completeness are separate facts.

## Stream records

Each stream contains one or more segments. A segment changes when its source generation, codec
configuration, observed framing, or read epoch changes. `data` describes the byte-for-byte
concatenation of records in admission order. Bytes are not sorted, deduplicated, normalized,
reframed, or supplemented with headers. Known codec suffixes are descriptive and do not promise
direct playback. `codec_provenance` and `framing_provenance` distinguish source header facts,
facts verified from the original bitstream, and unknown values. In particular, an AAC packet is
labelled `adts` only when its complete ADTS header and declared frame length match the captured
packet; other AAC input remains `unknown` rather than being guessed as a raw access unit.

Every `index.csv` is UTF-8, comma-separated, uses LF line endings, and has this exact header:

```text
capture_seq,source_record_seq,offset,length,source_timestamp,source_timestamp_unit,arrival_offset_us,source_flags,object_id,download_attempt_id,read_epoch,object_record_offset
```

Integers are decimal without scientific notation. An unknown value is empty. `offset` and `length`
cover the corresponding stream file without gaps or overlaps. `capture_seq` is unique across the
archive and records successful FIFO admission order. `object_record_offset` points to the TSTR
frame header for Cloud Storage data; RTC leaves object-related columns empty. An arrival offset is
always below 300,000,000 microseconds. A local reparse of an already downloaded object has no
network arrival timestamp.

Cloud Storage keeps each logical download attempt distinct. A successful attempt may produce a
complete `object.tstr`; an incomplete attempt keeps all received response chunks appended in
`object.part`. Different attempts are never joined and missing source ranges are never zero-filled.
The exact response and retry mapping is recorded in `ranges.csv`:

```text
response_id,range_offset,archive_offset,length,http_status,result
```

A `bytes` row maps an original object range to its contiguous offset in `object.part`. An `ok` or
`error:<TiError>` row summarizes one HTTP response. The final response ID `0` row records the
attempt result and total evidence bytes as `complete`, `partial`, or `error:<TiError>`. `partial`
means the capture window did not contain the whole object; it may have started after an earlier
range or ended during the final response. In the latter case, a `capture_window` attempt may have
bytes for one final response without its response summary. For a complete attempt, successful response summaries
must cover the object from offset zero through `expected_size` without holes or overrun; the
assembled `object.tstr` must match its declared size and SHA-256. `received_bytes` can exceed the
object size when retries overlap. The manifest also relates objects, parsed records, read epochs,
and whether an object was acquired before capture.

Every described file carries its exact byte length and lowercase SHA-256 digest. Consumers must
reject path traversal, symbolic links, duplicate ZIP entries, truncated indexes, invalid ranges,
hash mismatches, and archives that exceed their extraction budget. A reader may ignore unknown
optional fields within a supported version. It must reject an unknown major `version` instead of
interpreting it as v1 or v2.

## Uplink audio records (v2)

The four possible stage values are `captured`, `render_reference`, `processed`, and `encoded`.
Stages are independent observations. A selected source may have no stream, and an observed source
may omit any stage when no bytes reached it during the capture window. Consumers must not require
equal record counts, equal start/end times, or per-record pairing across stages.

Each stage has one or more segments. The first segment uses `<stage>.pcm` or
`<stage>.<codec-extension>` and `<stage>.index.csv`. A format change that makes continued bytes
ambiguous starts `<stage>.0002.*`, then `.0003`, without rotating other stages. `segment_id` is the
corresponding integer starting at 1. `source_instance_id` distinguishes actual input instances;
stop/start, rebinding, PTS changes, or an internal processing generation do not by themselves split
a segment. `record_count` and `byte_count` are unsigned decimal strings and must match the index and
data file.

All uplink segments declare the PCM context as:

```json
{"encoding":"s16le","sample_rate_hz":48000,"channels":1,"bits_per_sample":16}
```

For PCM stages this describes the data bytes directly, and every record length contains a whole
number of interleaved samples. An encoded segment also declares `codec`, `framing`, their
provenance, and `codec_config`. A config descriptor is present only when the observation point
provided the exact bytes; otherwise the value is `null`. Tools do not infer a missing config.

Every v2 uplink index is UTF-8 CSV with LF line endings and this exact header:

```text
capture_seq,offset,length,observation_offset_us,source_timestamp,source_timestamp_unit,far_volume
```

`capture_seq` is the archive-wide successful FIFO admission order. `offset` and `length` cover the
stage data file without gaps or overlaps. `observation_offset_us` is taken in the shared monotonic
clock domain at the observation point, relative to capture start; it is not a background write time.
`source_timestamp` is a canonical signed decimal string and `source_timestamp_unit` is `us`.
Timestamps may be negative, repeat, or move backwards. They are not a cross-stage join key.
`far_volume` is the actual integer percent (0 through 100) supplied with a `render_reference`
record; it is empty for all other stages.

## Diagnostic consumer

DevTools is the public consumer for archive inspection,
verification, packet extraction, conversion and bounded media analysis. Its
usage reference defines command options, execution status,
artifacts and external-tool requirements. The npm package carries its own readers and
format assets; Runtime packages and public API Samples do not project diagnostic readers.

Conversion writes a separate playback artifact and preserves the original archive. Raw AAC
requires observed AudioSpecificConfig or explicit caller configuration; unknown framing
cannot be inferred from codec configuration. PCM conversion uses the declared rate and
channels. TSTR inspection consumes a key file and does not include that key in its report.

Decoded media and a valid archive prove only the captured bytes and named observation points.
They do not prove physical presentation, audio quality or a root cause. Raw dumps may contain
customer media; applications control copying and sharing. The writer does not record access
keys, secret keys, tokens, signed URL queries or decryption keys.
