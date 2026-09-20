#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

#include "ti/error.h"
#include "ti/media.h"
#include "ti/rtc.h"
#include "ti/runtime.h"
#include "ti/storage.h"

#define TIRTC_CAPSULE_NAME "tirtc._native.Handle"

static _Atomic int interpreter_finalizing = 0;

static uint64_t current_process_id(void) {
#ifdef _WIN32
  return (uint64_t)_getpid();
#else
  return (uint64_t)getpid();
#endif
}

static int callback_gil_ensure(PyGILState_STATE* gil) {
  if (atomic_load(&interpreter_finalizing) || !Py_IsInitialized())
    return 0;
  *gil = PyGILState_Ensure();
  if (atomic_load(&interpreter_finalizing)) {
    PyGILState_Release(*gil);
    return 0;
  }
  return 1;
}

typedef enum HandleKind {
  HANDLE_CONNECTION = 1,
  HANDLE_RTC_CLIENT,
  HANDLE_CONNECT_ATTEMPT,
  HANDLE_AUDIO_OUTPUT,
  HANDLE_VIDEO_OUTPUT,
  HANDLE_ENCODED_AUDIO_OUTPUT,
  HANDLE_ENCODED_VIDEO_OUTPUT,
  HANDLE_RTC_RECORDING,
  HANDLE_STORAGE_CLIENT,
  HANDLE_STORAGE,
  HANDLE_RECORDING_REQUEST,
  HANDLE_DAYS_REQUEST,
  HANDLE_REPLAY,
  HANDLE_STORAGE_RECORDING,
  HANDLE_RAW_DUMP,
  HANDLE_EXPORT,
} HandleKind;

typedef struct NativeHandle {
  HandleKind kind;
  void* pointer;
  PyObject* callback;
  PyObject* parent;
  uint64_t creator_pid;
} NativeHandle;

typedef enum FrameKind {
  FRAME_AUDIO = 1,
  FRAME_VIDEO,
  FRAME_ENCODED_AUDIO,
  FRAME_ENCODED_VIDEO,
} FrameKind;

typedef struct FrameBuffer {
  PyObject_HEAD FrameKind kind;
  void* frame;
  const uint8_t* data;
  Py_ssize_t size;
  uint64_t creator_pid;
} FrameBuffer;

static PyObject* frame_buffer_type = NULL;

static const char* unicode_utf8(PyObject* value, PyObject** owner) {
  *owner = PyUnicode_AsEncodedString(value, "utf-8", "strict");
  if (*owner == NULL)
    return NULL;
  return PyBytes_AsString(*owner);
}

static void frame_release(FrameKind kind, void* frame) {
  if (frame == NULL)
    return;
  switch (kind) {
    case FRAME_AUDIO:
      ti_audio_frame_release((TiAudioFrame*)frame);
      break;
    case FRAME_VIDEO:
      ti_video_frame_release((TiVideoFrame*)frame);
      break;
    case FRAME_ENCODED_AUDIO:
      ti_encoded_audio_frame_release((TiEncodedAudioFrame*)frame);
      break;
    case FRAME_ENCODED_VIDEO:
      ti_encoded_video_frame_release((TiEncodedVideoFrame*)frame);
      break;
  }
}

static void* frame_retain(FrameKind kind, const void* frame) {
  switch (kind) {
    case FRAME_AUDIO:
      return ti_audio_frame_retain((const TiAudioFrame*)frame);
    case FRAME_VIDEO:
      return ti_video_frame_retain((const TiVideoFrame*)frame);
    case FRAME_ENCODED_AUDIO:
      return ti_encoded_audio_frame_retain((const TiEncodedAudioFrame*)frame);
    case FRAME_ENCODED_VIDEO:
      return ti_encoded_video_frame_retain((const TiEncodedVideoFrame*)frame);
  }
  return NULL;
}

static void frame_buffer_dealloc(PyObject* object) {
  FrameBuffer* buffer = (FrameBuffer*)object;
  if (!atomic_load(&interpreter_finalizing) && buffer->creator_pid == current_process_id())
    frame_release(buffer->kind, buffer->frame);
  PyTypeObject* type = Py_TYPE(object);
  freefunc free_object = (freefunc)PyType_GetSlot(type, Py_tp_free);
  free_object(object);
  Py_DECREF(type);
}

static int frame_buffer_getbuffer(PyObject* object, Py_buffer* view, int flags) {
  FrameBuffer* buffer = (FrameBuffer*)object;
  static const uint8_t empty = 0;
  void* data = (void*)(buffer->data == NULL ? &empty : buffer->data);
  return PyBuffer_FillInfo(view, object, data, buffer->size, 1, flags);
}

static PyType_Slot frame_buffer_slots[] = {
    {Py_tp_dealloc, frame_buffer_dealloc},
    {Py_tp_new, PyType_GenericNew},
    {Py_bf_getbuffer, frame_buffer_getbuffer},
    {0, NULL},
};

static PyType_Spec frame_buffer_spec = {
    .name = "tirtc._native._FrameBuffer",
    .basicsize = sizeof(FrameBuffer),
    .itemsize = 0,
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = frame_buffer_slots,
};

static PyObject* frame_memoryview(FrameKind kind, const void* frame, const uint8_t* data,
                                  uint64_t size) {
  if (size > (uint64_t)PY_SSIZE_T_MAX) {
    PyErr_SetString(PyExc_OverflowError, "native frame is too large");
    return NULL;
  }
  PyObject* owner = PyObject_CallNoArgs(frame_buffer_type);
  if (owner == NULL)
    return NULL;
  FrameBuffer* buffer = (FrameBuffer*)owner;
  buffer->kind = kind;
  buffer->frame = frame_retain(kind, frame);
  buffer->data = data;
  buffer->size = (Py_ssize_t)size;
  buffer->creator_pid = current_process_id();
  if (buffer->frame == NULL) {
    Py_DECREF(owner);
    PyErr_SetString(PyExc_RuntimeError, "failed to retain native frame");
    return NULL;
  }
  PyObject* view = PyMemoryView_FromObject(owner);
  Py_DECREF(owner);
  return view;
}

static TiError close_pointer(NativeHandle* handle) {
  if (handle == NULL || handle->pointer == NULL)
    return TI_ERROR_OK;
  switch (handle->kind) {
    case HANDLE_CONNECTION:
      return tirtc_conn_destroy((TiRtcConn*)handle->pointer);
    case HANDLE_RTC_CLIENT:
      return tirtc_client_destroy((TiRtcClient*)handle->pointer);
    case HANDLE_CONNECT_ATTEMPT:
      return tirtc_connect_attempt_destroy((TiRtcConnectAttempt*)handle->pointer);
    case HANDLE_AUDIO_OUTPUT:
      return ti_audio_output_destroy((TiAudioOutput*)handle->pointer);
    case HANDLE_VIDEO_OUTPUT:
      return ti_video_output_destroy((TiVideoOutput*)handle->pointer);
    case HANDLE_ENCODED_AUDIO_OUTPUT:
      return ti_encoded_audio_output_destroy((TiEncodedAudioOutput*)handle->pointer);
    case HANDLE_ENCODED_VIDEO_OUTPUT:
      return ti_encoded_video_output_destroy((TiEncodedVideoOutput*)handle->pointer);
    case HANDLE_RTC_RECORDING:
      return tirtc_recording_task_destroy((TiRtcRecordingTask*)handle->pointer);
    case HANDLE_STORAGE_CLIENT:
      return ti_cloud_storage_client_destroy((TiCloudStorageClient*)handle->pointer);
    case HANDLE_STORAGE:
      return ti_cloud_storage_destroy((TiCloudStorage*)handle->pointer);
    case HANDLE_RECORDING_REQUEST:
      return ti_cloud_storage_recording_request_destroy(
          (TiCloudStorageRecordingRequest*)handle->pointer);
    case HANDLE_DAYS_REQUEST:
      return ti_cloud_storage_recording_days_request_destroy(
          (TiCloudStorageRecordingDaysRequest*)handle->pointer);
    case HANDLE_REPLAY:
      return ti_cloud_storage_replay_destroy((TiCloudStorageReplay*)handle->pointer);
    case HANDLE_STORAGE_RECORDING:
      return ti_cloud_storage_recording_task_destroy((TiCloudStorageRecordingTask*)handle->pointer);
    case HANDLE_RAW_DUMP:
      return ti_raw_dump_destroy((TiRawDump*)handle->pointer);
    case HANDLE_EXPORT:
      return ti_cloud_storage_export_task_destroy((TiCloudStorageExportTask*)handle->pointer);
  }
  return TI_ERROR_INVALID_ARGUMENT;
}

static void mark_closed(NativeHandle* handle) {
  handle->pointer = NULL;
  Py_CLEAR(handle->callback);
  Py_CLEAR(handle->parent);
}

static void abandon_handle(NativeHandle* handle) {
  TiError error = TI_ERROR_OK;
  if (handle->pointer != NULL) {
    Py_BEGIN_ALLOW_THREADS error = close_pointer(handle);
    Py_END_ALLOW_THREADS
  }
  if (error != TI_ERROR_OK)
    return;
  Py_XDECREF(handle->callback);
  Py_XDECREF(handle->parent);
  free(handle);
}

static void discard_inherited_handle(NativeHandle* handle) {
  Py_XDECREF(handle->callback);
  Py_XDECREF(handle->parent);
  free(handle);
}

static const char* handle_kind_name(HandleKind kind) {
  switch (kind) {
    case HANDLE_CONNECTION:
      return "Connection";
    case HANDLE_RTC_CLIENT:
      return "Client";
    case HANDLE_CONNECT_ATTEMPT:
      return "ConnectAttempt";
    case HANDLE_AUDIO_OUTPUT:
      return "AudioOutput";
    case HANDLE_VIDEO_OUTPUT:
      return "VideoOutput";
    case HANDLE_ENCODED_AUDIO_OUTPUT:
      return "EncodedAudioOutput";
    case HANDLE_ENCODED_VIDEO_OUTPUT:
      return "EncodedVideoOutput";
    case HANDLE_RTC_RECORDING:
    case HANDLE_STORAGE_RECORDING:
      return "RecordingTask";
    case HANDLE_RAW_DUMP:
      return "RawDump";
    case HANDLE_STORAGE_CLIENT:
      return "CloudStorageClient";
    case HANDLE_STORAGE:
      return "CloudStorage";
    case HANDLE_RECORDING_REQUEST:
    case HANDLE_DAYS_REQUEST:
      return "ListRequest";
    case HANDLE_REPLAY:
      return "Replay";
    case HANDLE_EXPORT:
      return "ExportTask";
  }
  return "resource";
}

static void capsule_destructor(PyObject* capsule) {
  NativeHandle* handle = (NativeHandle*)PyCapsule_GetPointer(capsule, TIRTC_CAPSULE_NAME);
  if (handle == NULL) {
    PyErr_Clear();
    return;
  }
  if (atomic_load(&interpreter_finalizing))
    return;
  if (handle->creator_pid != current_process_id()) {
    discard_inherited_handle(handle);
    return;
  }
  if (handle->pointer != NULL) {
    char message[96];
    (void)snprintf(message, sizeof(message), "unclosed TiRTC %s", handle_kind_name(handle->kind));
    if (PyErr_WarnEx(PyExc_ResourceWarning, message, 1) < 0) {
      PyErr_WriteUnraisable(capsule);
    }
  }
  abandon_handle(handle);
}

static NativeHandle* handle_new(HandleKind kind, PyObject* callback, PyObject* parent) {
  if (callback != NULL && !PyCallable_Check(callback)) {
    PyErr_SetString(PyExc_TypeError, "callback must be callable");
    return NULL;
  }
  NativeHandle* handle = (NativeHandle*)calloc(1, sizeof(NativeHandle));
  if (handle == NULL) {
    PyErr_NoMemory();
    return NULL;
  }
  handle->kind = kind;
  handle->creator_pid = current_process_id();
  if (callback != NULL) {
    Py_INCREF(callback);
    handle->callback = callback;
  }
  if (parent != NULL) {
    Py_INCREF(parent);
    handle->parent = parent;
  }
  return handle;
}

static PyObject* handle_capsule(NativeHandle* handle) {
  PyObject* capsule = PyCapsule_New(handle, TIRTC_CAPSULE_NAME, capsule_destructor);
  if (capsule == NULL)
    abandon_handle(handle);
  return capsule;
}

static PyObject* handle_result(NativeHandle* handle) {
  PyObject* capsule = handle_capsule(handle);
  if (capsule == NULL)
    return NULL;
  PyObject* result = Py_BuildValue("(iO)", TI_ERROR_OK, capsule);
  Py_DECREF(capsule);
  return result;
}

static void discard_handle_result(PyObject* result, NativeHandle* handle) {
  TiError error = TI_ERROR_OK;
  if (handle->pointer != NULL) {
    Py_BEGIN_ALLOW_THREADS error = close_pointer(handle);
    Py_END_ALLOW_THREADS if (error == TI_ERROR_OK) mark_closed(handle);
  }
  Py_DECREF(result);
}

static NativeHandle* get_handle(PyObject* capsule, HandleKind kind) {
  NativeHandle* handle = (NativeHandle*)PyCapsule_GetPointer(capsule, TIRTC_CAPSULE_NAME);
  if (handle == NULL)
    return NULL;
  if (handle->kind != kind || handle->pointer == NULL) {
    PyErr_SetString(PyExc_RuntimeError, "native handle has the wrong type or is closed");
    return NULL;
  }
  return handle;
}

static int is_output(HandleKind kind) {
  return kind == HANDLE_AUDIO_OUTPUT || kind == HANDLE_VIDEO_OUTPUT ||
         kind == HANDLE_ENCODED_AUDIO_OUTPUT || kind == HANDLE_ENCODED_VIDEO_OUTPUT;
}

static NativeHandle* get_output(PyObject* capsule) {
  NativeHandle* handle = (NativeHandle*)PyCapsule_GetPointer(capsule, TIRTC_CAPSULE_NAME);
  if (handle == NULL)
    return NULL;
  if (!is_output(handle->kind) || handle->pointer == NULL) {
    PyErr_SetString(PyExc_RuntimeError, "native output handle is closed");
    return NULL;
  }
  return handle;
}

static void call_callback_locked(NativeHandle* handle, PyObject* arguments) {
  if (arguments == NULL) {
    PyErr_WriteUnraisable(handle->callback == NULL ? Py_None : handle->callback);
    return;
  }
  if (handle->callback != NULL) {
    PyObject* result = PyObject_CallObject(handle->callback, arguments);
    if (result == NULL) {
      PyErr_WriteUnraisable(handle->callback);
    } else {
      Py_DECREF(result);
    }
  }
  Py_DECREF(arguments);
}

#define TIRTC_CALLBACK_TASK_NAME "tirtc._native.CallbackTask"

typedef struct NativeCallbackTask {
  TiCallbackTaskFn function;
  void* data;
  uint64_t creator_pid;
} NativeCallbackTask;

/* The GIL protects the exactly-once handoff; Runtime owns task_data until run. */
static void run_callback_task_locked(NativeCallbackTask* task) {
  TiCallbackTaskFn function = task->function;
  task->function = NULL;
  if (function == NULL || task->creator_pid != current_process_id())
    return;
  Py_BEGIN_ALLOW_THREADS function(task->data);
  Py_END_ALLOW_THREADS
}

static void callback_task_destroy(PyObject* capsule) {
  NativeCallbackTask* task = PyCapsule_GetPointer(capsule, TIRTC_CALLBACK_TASK_NAME);
  if (task == NULL) {
    PyErr_Clear();
    return;
  }
  run_callback_task_locked(task);
  free(task);
}

static PyObject* py_run_callback_task(PyObject* module, PyObject* capsule) {
  (void)module;
  NativeCallbackTask* task = PyCapsule_GetPointer(capsule, TIRTC_CALLBACK_TASK_NAME);
  if (task == NULL)
    return NULL;
  run_callback_task_locked(task);
  Py_RETURN_NONE;
}

static void TI_CALL dispatch_callback_task(TiCallbackTaskFn function, void* data,
                                          void* user_data) {
  PyGILState_STATE state;
  if (!callback_gil_ensure(&state)) {
    function(data);
    return;
  }
  NativeCallbackTask* task = malloc(sizeof(*task));
  PyObject* capsule = NULL;
  if (task != NULL) {
    *task = (NativeCallbackTask){function, data, current_process_id()};
    capsule = PyCapsule_New(task, TIRTC_CALLBACK_TASK_NAME, callback_task_destroy);
  }
  if (capsule != NULL) {
    call_callback_locked((NativeHandle*)user_data, Py_BuildValue("(sO)", "native_task", capsule));
    /* A rejected dispatch still runs the task, releasing its Runtime barrier. */
    Py_DECREF(capsule);
  } else {
    free(task);
    PyErr_Clear();
    Py_BEGIN_ALLOW_THREADS function(data);
    Py_END_ALLOW_THREADS
  }
  PyGILState_Release(state);
}

static void report_callback_error(NativeHandle* handle) {
  if (PyErr_Occurred()) {
    PyErr_WriteUnraisable(handle->callback == NULL ? Py_None : handle->callback);
  }
}

static void emit_simple(NativeHandle* handle, const char* event) {
  PyGILState_STATE state;
  if (!callback_gil_ensure(&state))
    return;
  call_callback_locked(handle, Py_BuildValue("(s)", event));
  PyGILState_Release(state);
}

static void conn_on_state(TiRtcConn* connection, TiRtcConnState state, TiError error,
                          void* user_data) {
  (void)connection;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  call_callback_locked(handle, Py_BuildValue("(sIi)", "state", state, error));
  PyGILState_Release(gil);
}

static void conn_on_command(TiRtcConn* connection, uint32_t command, const uint8_t* data,
                            uint64_t data_size, void* user_data) {
  (void)connection;
  NativeHandle* handle = (NativeHandle*)user_data;
  if (data_size > (uint64_t)PY_SSIZE_T_MAX)
    return;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* payload = PyBytes_FromStringAndSize((const char*)data, (Py_ssize_t)data_size);
  if (payload != NULL) {
    PyObject* arguments = Py_BuildValue("(skO)", "command", (unsigned long)command, payload);
    call_callback_locked(handle, arguments);
  } else {
    report_callback_error(handle);
  }
  Py_XDECREF(payload);
  PyGILState_Release(gil);
}

static void conn_on_message(TiRtcConn* connection, uint8_t stream_id, uint32_t timestamp_ms,
                            const uint8_t* data, uint64_t data_size, void* user_data) {
  (void)connection;
  NativeHandle* handle = (NativeHandle*)user_data;
  if (data_size > (uint64_t)PY_SSIZE_T_MAX)
    return;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* payload = PyBytes_FromStringAndSize((const char*)data, (Py_ssize_t)data_size);
  if (payload != NULL) {
    PyObject* arguments =
        Py_BuildValue("(sIIO)", "message", (unsigned int)stream_id, timestamp_ms, payload);
    Py_DECREF(payload);
    call_callback_locked(handle, arguments);
  } else {
    report_callback_error(handle);
  }
  PyGILState_Release(gil);
}

static TiRtcConnCallbacks connection_callbacks(int command_enabled, int message_enabled) {
  TiRtcConnCallbacks callbacks = TI_RTC_CONN_CALLBACKS_INITIALIZER;
  callbacks.on_state_changed = conn_on_state;
  callbacks.on_command = command_enabled ? conn_on_command : NULL;
  callbacks.on_stream_message = message_enabled ? conn_on_message : NULL;
  return callbacks;
}

static void TI_CALL connect_attempt_on_complete(TiRtcConnectAttempt* attempt, TiError error,
                                                void* user_data) {
  (void)attempt;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  call_callback_locked(handle, Py_BuildValue("(i)", error));
  PyGILState_Release(gil);
}

static void output_on_state_audio(TiAudioOutput* output, TiOutputState state, void* user_data) {
  (void)output;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  call_callback_locked(handle, Py_BuildValue("(sI)", "state", state));
  PyGILState_Release(gil);
}

static void output_on_state_video(TiVideoOutput* output, TiOutputState state, void* user_data) {
  output_on_state_audio((TiAudioOutput*)output, state, user_data);
}

static void output_on_state_encoded_audio(TiEncodedAudioOutput* output, TiOutputState state,
                                          void* user_data) {
  output_on_state_audio((TiAudioOutput*)output, state, user_data);
}

static void output_on_state_encoded_video(TiEncodedVideoOutput* output, TiOutputState state,
                                          void* user_data) {
  output_on_state_audio((TiAudioOutput*)output, state, user_data);
}

static void output_error(NativeHandle* handle, TiError error) {
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  call_callback_locked(handle, Py_BuildValue("(si)", "error", error));
  PyGILState_Release(gil);
}

static void output_on_error_audio(TiAudioOutput* output, TiError error, const char* message,
                                  void* user_data) {
  (void)output;
  (void)message;
  output_error((NativeHandle*)user_data, error);
}

static void output_on_error_video(TiVideoOutput* output, TiError error, const char* message,
                                  void* user_data) {
  (void)output;
  (void)message;
  output_error((NativeHandle*)user_data, error);
}

static void output_on_error_encoded_audio(TiEncodedAudioOutput* output, TiError error,
                                          const char* message, void* user_data) {
  (void)output;
  (void)message;
  output_error((NativeHandle*)user_data, error);
}

static void output_on_error_encoded_video(TiEncodedVideoOutput* output, TiError error,
                                          const char* message, void* user_data) {
  (void)output;
  (void)message;
  output_error((NativeHandle*)user_data, error);
}

static void output_on_audio_frame(TiAudioOutput* output, const TiAudioFrame* frame,
                                  void* user_data) {
  (void)output;
  TiAudioFrameInfo info = TI_AUDIO_FRAME_INFO_INITIALIZER;
  if (atomic_load(&interpreter_finalizing) || ti_audio_frame_get_info(frame, &info) != TI_ERROR_OK)
    return;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* data = frame_memoryview(FRAME_AUDIO, frame, info.data, info.data_size);
  if (data != NULL) {
    PyObject* arguments = Py_BuildValue(
        "(sOLLiIIIIO)", "audio_frame", data, info.pts_us, info.source_time_utc_us,
        (int)info.has_source_time, info.format.sample_format, info.format.sample_rate_hz,
        info.format.channels, info.samples_per_channel, info.discontinuity ? Py_True : Py_False);
    Py_DECREF(data);
    call_callback_locked((NativeHandle*)user_data, arguments);
  } else {
    report_callback_error((NativeHandle*)user_data);
  }
  PyGILState_Release(gil);
}

static void output_on_video_frame(TiVideoOutput* output, const TiVideoFrame* frame,
                                  void* user_data) {
  (void)output;
  TiVideoFrameInfo info = TI_VIDEO_FRAME_INFO_INITIALIZER;
  if (atomic_load(&interpreter_finalizing) || ti_video_frame_get_info(frame, &info) != TI_ERROR_OK)
    return;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* planes = PyTuple_New((Py_ssize_t)info.plane_count);
  if (planes != NULL) {
    for (uint32_t index = 0; index < info.plane_count; ++index) {
      PyObject* data = frame_memoryview(FRAME_VIDEO, frame, info.planes[index].data,
                                        info.planes[index].data_size);
      PyObject* plane =
          data == NULL ? NULL : Py_BuildValue("(IO)", info.planes[index].stride_bytes, data);
      Py_XDECREF(data);
      if (plane == NULL) {
        Py_DECREF(planes);
        planes = NULL;
        break;
      }
      PyTuple_SetItem(planes, (Py_ssize_t)index, plane);
    }
  }
  if (planes != NULL) {
    PyObject* arguments =
        Py_BuildValue("(sOLLiIIIO)", "video_frame", planes, info.pts_us, info.source_time_utc_us,
                      (int)info.has_source_time, info.pixel_format, info.width, info.height,
                      info.discontinuity ? Py_True : Py_False);
    Py_DECREF(planes);
    call_callback_locked((NativeHandle*)user_data, arguments);
  } else {
    report_callback_error((NativeHandle*)user_data);
  }
  PyGILState_Release(gil);
}

static void output_on_encoded_audio_frame(TiEncodedAudioOutput* output,
                                          const TiEncodedAudioFrame* frame, void* user_data) {
  (void)output;
  TiEncodedAudioFrameInfo info = TI_ENCODED_AUDIO_FRAME_INFO_INITIALIZER;
  if (atomic_load(&interpreter_finalizing) ||
      ti_encoded_audio_frame_get_info(frame, &info) != TI_ERROR_OK)
    return;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* data = frame_memoryview(FRAME_ENCODED_AUDIO, frame, info.data, info.data_size);
  PyObject* config = data == NULL ? NULL
                                  : frame_memoryview(FRAME_ENCODED_AUDIO, frame, info.codec_config,
                                                     info.codec_config_size);
  if (data != NULL && config != NULL) {
    PyObject* arguments = Py_BuildValue(
        "(sOOLLiIIIIO)", "encoded_audio_frame", data, config, info.pts_us, info.source_time_utc_us,
        (int)info.has_source_time, info.codec, info.bitstream_format, info.sample_rate_hz,
        info.channels, info.discontinuity ? Py_True : Py_False);
    call_callback_locked((NativeHandle*)user_data, arguments);
  } else {
    report_callback_error((NativeHandle*)user_data);
  }
  Py_XDECREF(data);
  Py_XDECREF(config);
  PyGILState_Release(gil);
}

static void output_on_encoded_video_frame(TiEncodedVideoOutput* output,
                                          const TiEncodedVideoFrame* frame, void* user_data) {
  (void)output;
  TiEncodedVideoFrameInfo info = TI_ENCODED_VIDEO_FRAME_INFO_INITIALIZER;
  if (atomic_load(&interpreter_finalizing) ||
      ti_encoded_video_frame_get_info(frame, &info) != TI_ERROR_OK)
    return;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* data = frame_memoryview(FRAME_ENCODED_VIDEO, frame, info.data, info.data_size);
  PyObject* config = data == NULL ? NULL
                                  : frame_memoryview(FRAME_ENCODED_VIDEO, frame, info.codec_config,
                                                     info.codec_config_size);
  if (data != NULL && config != NULL) {
    PyObject* arguments = Py_BuildValue(
        "(sOOLLiIIIIOO)", "encoded_video_frame", data, config, info.pts_us, info.source_time_utc_us,
        (int)info.has_source_time, info.codec, info.bitstream_format, info.width, info.height,
        info.key_frame ? Py_True : Py_False, info.discontinuity ? Py_True : Py_False);
    call_callback_locked((NativeHandle*)user_data, arguments);
  } else {
    report_callback_error((NativeHandle*)user_data);
  }
  Py_XDECREF(data);
  Py_XDECREF(config);
  PyGILState_Release(gil);
}

static void request_completed_recordings(TiCloudStorageRecordingRequest* request, void* user_data) {
  (void)request;
  emit_simple((NativeHandle*)user_data, "completed");
}

static void request_completed_days(TiCloudStorageRecordingDaysRequest* request, void* user_data) {
  (void)request;
  emit_simple((NativeHandle*)user_data, "completed");
}

static void replay_on_time(TiCloudStorageReplay* replay, int64_t time_ms, void* user_data) {
  (void)replay;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  call_callback_locked(handle, Py_BuildValue("(sL)", "time", time_ms));
  PyGILState_Release(gil);
}

static void replay_on_completed(TiCloudStorageReplay* replay, void* user_data) {
  (void)replay;
  emit_simple((NativeHandle*)user_data, "completed");
}

static void replay_on_error(TiCloudStorageReplay* replay, TiError error, void* user_data) {
  (void)replay;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  call_callback_locked(handle, Py_BuildValue("(si)", "error", error));
  PyGILState_Release(gil);
}

static PyObject* recording_gap_value(const TiCloudStorageRecordingGap* gap) {
  if (gap == NULL)
    return Py_BuildValue("(LLOO)", 0LL, 0LL, Py_None, Py_None);
  PyObject* tracks = PyTuple_New((Py_ssize_t)gap->track_count);
  PyObject* reasons = PyTuple_New((Py_ssize_t)gap->reason_count);
  if (tracks == NULL || reasons == NULL) {
    Py_XDECREF(tracks);
    Py_XDECREF(reasons);
    return NULL;
  }
  for (size_t index = 0; index < gap->track_count; ++index) {
    PyObject* track = Py_BuildValue("(II)", gap->tracks[index].kind,
                                    (unsigned int)gap->tracks[index].channel_id);
    if (track == NULL) {
      Py_DECREF(tracks);
      Py_DECREF(reasons);
      return NULL;
    }
    if (PyTuple_SetItem(tracks, (Py_ssize_t)index, track) != 0) {
      Py_DECREF(tracks);
      Py_DECREF(reasons);
      return NULL;
    }
  }
  for (size_t index = 0; index < gap->reason_count; ++index) {
    PyObject* reason = PyLong_FromUnsignedLong(gap->reasons[index]);
    if (reason == NULL) {
      Py_DECREF(tracks);
      Py_DECREF(reasons);
      return NULL;
    }
    if (PyTuple_SetItem(reasons, (Py_ssize_t)index, reason) != 0) {
      Py_DECREF(tracks);
      Py_DECREF(reasons);
      return NULL;
    }
  }
  return Py_BuildValue("(LLNN)", gap->range.start_time_ms, gap->range.end_time_ms, tracks,
                       reasons);
}

static void replay_on_gap(TiCloudStorageReplay* replay, const TiCloudStorageRecordingGap* gap,
                          void* user_data) {
  (void)replay;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* value = recording_gap_value(gap);
  if (value != NULL)
    call_callback_locked(handle, Py_BuildValue("(sN)", "gap", value));
  PyGILState_Release(gil);
}

static void export_on_progress(TiCloudStorageExportTask* task,
                               const TiCloudStorageExportProgress* progress, void* user_data) {
  (void)task;
  if (progress == NULL)
    return;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  call_callback_locked(handle, Py_BuildValue("(sdL)", "progress", progress->fraction,
                                              progress->covered_duration_ms));
  PyGILState_Release(gil);
}

static void export_on_gap(TiCloudStorageExportTask* task, const TiCloudStorageRecordingGap* gap,
                          void* user_data) {
  (void)task;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  PyObject* value = recording_gap_value(gap);
  if (value != NULL)
    call_callback_locked(handle, Py_BuildValue("(sN)", "gap", value));
  PyGILState_Release(gil);
}

static void export_on_completed(TiCloudStorageExportTask* task, TiError error,
                                const TiCloudStorageMp4File* file, void* user_data) {
  (void)task;
  NativeHandle* handle = (NativeHandle*)user_data;
  PyGILState_STATE gil;
  if (!callback_gil_ensure(&gil))
    return;
  const char* path = file == NULL || file->file_path == NULL ? "" : file->file_path;
  int64_t duration = file == NULL ? 0 : file->duration_ms;
  call_callback_locked(handle, Py_BuildValue("(sisL)", "completed", error, path, duration));
  PyGILState_Release(gil);
}

static PyObject* py_error_name(PyObject* module, PyObject* argument) {
  (void)module;
  long code = PyLong_AsLong(argument);
  if (code == -1 && PyErr_Occurred())
    return NULL;
  return PyUnicode_FromString(ti_error_to_string((TiError)code));
}

static PyObject* py_mark_finalizing(PyObject* module, PyObject* unused) {
  (void)module;
  (void)unused;
  atomic_store(&interpreter_finalizing, 1);
  Py_RETURN_NONE;
}

static PyObject* py_rtc_initialize(PyObject* module, PyObject* arguments) {
  (void)module;
  const char* app_id;
  const char* endpoint;
  const char* cache_dir;
  int console_log_enabled;
  if (!PyArg_ParseTuple(arguments, "szsp", &app_id, &endpoint, &cache_dir, &console_log_enabled))
    return NULL;
  TiRtcInitOptions options = TI_RTC_INIT_OPTIONS_INITIALIZER;
  options.app_id = app_id;
  options.endpoint = endpoint;
  options.cache_root_dir = cache_dir;
  options.console_log_enabled = (uint8_t)console_log_enabled;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = tirtc_init(&options);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_rtc_shutdown(PyObject* module, PyObject* unused) {
  (void)module;
  (void)unused;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = tirtc_uninit();
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_storage_initialize(PyObject* module, PyObject* arguments) {
  (void)module;
  const char* app_id;
  const char* endpoint;
  const char* cache_dir;
  int console_log_enabled;
  if (!PyArg_ParseTuple(arguments, "szsp", &app_id, &endpoint, &cache_dir, &console_log_enabled))
    return NULL;
  TiCloudStorageInitOptions options = TI_CLOUD_STORAGE_INIT_OPTIONS_INITIALIZER;
  options.app_id = app_id;
  options.endpoint = endpoint;
  options.cache_root_dir = cache_dir;
  options.console_log_enabled = (uint8_t)console_log_enabled;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_init(&options);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_storage_shutdown(PyObject* module, PyObject* unused) {
  (void)module;
  (void)unused;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_uninit();
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_upload_logs(PyObject* module, PyObject* unused) {
  (void)module;
  (void)unused;
  char log_id[TI_LOG_ID_CAPACITY] = {0};
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_logging_upload(log_id, sizeof(log_id));
  Py_END_ALLOW_THREADS return Py_BuildValue("(is)", error, error == TI_ERROR_OK ? log_id : "");
}

static PyObject* py_write_log(PyObject* module, PyObject* arguments) {
  (void)module;
  unsigned int level;
  const char* tag;
  const char* message;
  if (!PyArg_ParseTuple(arguments, "Iss", &level, &tag, &message))
    return NULL;
  return PyLong_FromLong(ti_logging_write((TiLogLevel)level, tag, message));
}

static PyObject* py_delete_media_file(PyObject* module, PyObject* argument) {
  (void)module;
  PyObject* encoded = NULL;
  const char* path = unicode_utf8(argument, &encoded);
  if (path == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_local_media_file_delete(path);
  Py_END_ALLOW_THREADS PyObject* result = PyLong_FromLong(error);
  Py_DECREF(encoded);
  return result;
}

static PyObject* py_rtc_client_create(PyObject* module, PyObject* arguments) {
  (void)module;
  const char* app_id;
  const char* access_key_id;
  const char* access_key_secret;
  const char* endpoint;
  const char* cache_dir;
  int console_log_enabled;
  if (!PyArg_ParseTuple(arguments, "ssszsp", &app_id, &access_key_id, &access_key_secret, &endpoint,
                        &cache_dir, &console_log_enabled))
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_RTC_CLIENT, NULL, NULL);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiRtcClientOptions options = TI_RTC_CLIENT_OPTIONS_INITIALIZER;
  options.app_id = app_id;
  options.access_key_id = access_key_id;
  options.access_key_secret = access_key_secret;
  options.endpoint = endpoint;
  options.cache_root_dir = cache_dir;
  options.console_log_enabled = (uint8_t)console_log_enabled;
  TiRtcClient* client = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = tirtc_client_create(&options, &client);
  Py_END_ALLOW_THREADS handle->pointer = client;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_rtc_client_create_connection(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* client_capsule;
  PyObject* callback;
  int command_enabled;
  int message_enabled;
  if (!PyArg_ParseTuple(arguments, "OOpp", &client_capsule, &callback, &command_enabled,
                        &message_enabled))
    return NULL;
  NativeHandle* client = get_handle(client_capsule, HANDLE_RTC_CLIENT);
  if (client == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_CONNECTION, callback, client_capsule);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiRtcConnCallbacks callbacks = connection_callbacks(command_enabled, message_enabled);
  TiRtcConnCreateOptions options = TI_RTC_CONN_CREATE_OPTIONS_INITIALIZER;
  options.callbacks = &callbacks;
  options.user_data = handle;
  TiRtcConn* connection = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      tirtc_client_create_connection((TiRtcClient*)client->pointer, &options, &connection);
  Py_END_ALLOW_THREADS handle->pointer = connection;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_conn_create(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* callback;
  int command_enabled;
  int message_enabled;
  if (!PyArg_ParseTuple(arguments, "Opp", &callback, &command_enabled, &message_enabled))
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_CONNECTION, callback, NULL);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiRtcConnCallbacks callbacks = connection_callbacks(command_enabled, message_enabled);
  TiRtcConnCreateOptions options = TI_RTC_CONN_CREATE_OPTIONS_INITIALIZER;
  options.callbacks = &callbacks;
  options.user_data = handle;
  TiRtcConn* connection = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = tirtc_conn_create(&options, &connection);
  Py_END_ALLOW_THREADS handle->pointer = connection;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_conn_connect(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  const char* remote_id;
  const char* token;
  if (!PyArg_ParseTuple(arguments, "Oss", &capsule, &remote_id, &token))
    return NULL;
  NativeHandle* handle = get_handle(capsule, HANDLE_CONNECTION);
  if (handle == NULL)
    return NULL;
  TiRtcConnConnectOptions options = TI_RTC_CONN_CONNECT_OPTIONS_INITIALIZER;
  options.remote_id = remote_id;
  options.token = token;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = tirtc_conn_connect((TiRtcConn*)handle->pointer, &options);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_conn_connect_managed(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* connection_capsule;
  const char* device_id;
  long long timeout_ms;
  PyObject* callback;
  if (!PyArg_ParseTuple(arguments, "OsLO", &connection_capsule, &device_id, &timeout_ms, &callback))
    return NULL;
  NativeHandle* connection = get_handle(connection_capsule, HANDLE_CONNECTION);
  if (connection == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_CONNECT_ATTEMPT, callback, connection_capsule);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiRtcManagedConnectOptions options = TI_RTC_MANAGED_CONNECT_OPTIONS_INITIALIZER;
  options.device_id = device_id;
  options.timeout_ms = (int64_t)timeout_ms;
  TiRtcConnectAttemptCallbacks callbacks = TI_RTC_CONNECT_ATTEMPT_CALLBACKS_INITIALIZER;
  callbacks.on_complete = connect_attempt_on_complete;
  TiRtcConnectAttempt* attempt = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = tirtc_conn_connect_managed((TiRtcConn*)connection->pointer,
                                                            &options, &callbacks, handle, &attempt);
  Py_END_ALLOW_THREADS handle->pointer = attempt;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_connect_attempt_cancel(PyObject* module, PyObject* argument) {
  (void)module;
  NativeHandle* attempt = get_handle(argument, HANDLE_CONNECT_ATTEMPT);
  if (attempt == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      tirtc_connect_attempt_cancel((TiRtcConnectAttempt*)attempt->pointer);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_conn_simple(PyObject* arguments, TiError (*operation)(TiRtcConn*)) {
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* handle = get_handle(capsule, HANDLE_CONNECTION);
  if (handle == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = operation((TiRtcConn*)handle->pointer);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_conn_disconnect(PyObject* module, PyObject* arguments) {
  (void)module;
  return py_conn_simple(arguments, tirtc_conn_disconnect);
}

static PyObject* py_conn_send_command(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  unsigned long command_id;
  PyObject* payload;
  if (!PyArg_ParseTuple(arguments, "OkO", &capsule, &command_id, &payload))
    return NULL;
  NativeHandle* handle = get_handle(capsule, HANDLE_CONNECTION);
  if (handle == NULL)
    return NULL;
  Py_buffer view;
  if (PyObject_GetBuffer(payload, &view, PyBUF_CONTIG_RO) != 0)
    return NULL;
  TiRtcConnCommand command = {(uint32_t)command_id, (const uint8_t*)view.buf, (uint64_t)view.len};
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = tirtc_conn_send_command((TiRtcConn*)handle->pointer, &command);
  Py_END_ALLOW_THREADS PyBuffer_Release(&view);
  return PyLong_FromLong(error);
}

static PyObject* py_conn_send_message(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  unsigned int stream_id;
  unsigned long timestamp_ms;
  PyObject* payload;
  if (!PyArg_ParseTuple(arguments, "OIkO", &capsule, &stream_id, &timestamp_ms, &payload))
    return NULL;
  NativeHandle* handle = get_handle(capsule, HANDLE_CONNECTION);
  if (handle == NULL)
    return NULL;
  Py_buffer view;
  if (PyObject_GetBuffer(payload, &view, PyBUF_CONTIG_RO) != 0)
    return NULL;
  TiRtcStreamMessage message = {(uint32_t)timestamp_ms, (const uint8_t*)view.buf,
                                (uint64_t)view.len};
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      tirtc_conn_send_stream_message((TiRtcConn*)handle->pointer, (uint8_t)stream_id, &message);
  Py_END_ALLOW_THREADS PyBuffer_Release(&view);
  return PyLong_FromLong(error);
}

static PyObject* py_conn_stream_operation(PyObject* arguments,
                                          TiError (*operation)(TiRtcConn*, uint8_t)) {
  PyObject* capsule;
  unsigned int stream_id;
  if (!PyArg_ParseTuple(arguments, "OI", &capsule, &stream_id))
    return NULL;
  NativeHandle* handle = get_handle(capsule, HANDLE_CONNECTION);
  if (handle == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = operation((TiRtcConn*)handle->pointer, (uint8_t)stream_id);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

#define CONN_STREAM_METHOD(name, native_name)                    \
  static PyObject* name(PyObject* module, PyObject* arguments) { \
    (void)module;                                                \
    return py_conn_stream_operation(arguments, native_name);     \
  }

CONN_STREAM_METHOD(py_conn_subscribe_audio, tirtc_conn_subscribe_audio)
CONN_STREAM_METHOD(py_conn_unsubscribe_audio, tirtc_conn_unsubscribe_audio)
CONN_STREAM_METHOD(py_conn_subscribe_video, tirtc_conn_subscribe_video)
CONN_STREAM_METHOD(py_conn_unsubscribe_video, tirtc_conn_unsubscribe_video)
CONN_STREAM_METHOD(py_conn_request_video_keyframe, tirtc_conn_request_video_key_frame)

static TiOutputBufferOptions make_buffer_options(unsigned int strategy, long watermark_ms) {
  TiOutputBufferOptions options = TI_OUTPUT_BUFFER_OPTIONS_INITIALIZER;
  options.strategy = (TiOutputBufferStrategy)strategy;
  if (watermark_ms >= 0) {
    options.has_max_buffer_watermark_ms = 1;
    options.max_buffer_watermark_ms = (int32_t)watermark_ms;
  }
  return options;
}

static PyObject* py_output_create(PyObject* module, PyObject* arguments) {
  (void)module;
  const char* kind;
  PyObject* callback;
  int state_enabled;
  int error_enabled;
  unsigned int agc;
  unsigned int ans;
  unsigned int decoder;
  unsigned int strategy;
  long watermark_ms;
  if (!PyArg_ParseTuple(arguments, "sOppIIIIl", &kind, &callback, &state_enabled, &error_enabled,
                        &agc, &ans, &decoder, &strategy, &watermark_ms))
    return NULL;
  HandleKind handle_kind;
  if (strcmp(kind, "audio") == 0)
    handle_kind = HANDLE_AUDIO_OUTPUT;
  else if (strcmp(kind, "video") == 0)
    handle_kind = HANDLE_VIDEO_OUTPUT;
  else if (strcmp(kind, "encoded_audio") == 0)
    handle_kind = HANDLE_ENCODED_AUDIO_OUTPUT;
  else if (strcmp(kind, "encoded_video") == 0)
    handle_kind = HANDLE_ENCODED_VIDEO_OUTPUT;
  else {
    PyErr_SetString(PyExc_ValueError, "unknown output kind");
    return NULL;
  }
  NativeHandle* handle = handle_new(handle_kind, callback, NULL);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiError error = TI_ERROR_OK;
  TiOutputBufferOptions buffer = make_buffer_options(strategy, watermark_ms);
  Py_BEGIN_ALLOW_THREADS if (handle_kind == HANDLE_AUDIO_OUTPUT) {
    TiAudioOutput* output = NULL;
    error = ti_audio_output_create(&output);
    if (error == TI_ERROR_OK) {
      TiAudioOutputOptions options = TI_AUDIO_OUTPUT_OPTIONS_INITIALIZER;
      options.agc_level = (TiAudioAgcLevel)agc;
      options.ans_level = (TiAudioAnsLevel)ans;
      error = ti_audio_output_set_options(output, &options);
    }
    if (error == TI_ERROR_OK)
      error = ti_audio_output_set_buffer_options(output, &buffer);
    if (error == TI_ERROR_OK) {
      TiAudioOutputCallbacks callbacks = TI_AUDIO_OUTPUT_CALLBACKS_INITIALIZER;
      callbacks.on_frame = output_on_audio_frame;
      callbacks.on_state_changed = state_enabled ? output_on_state_audio : NULL;
      callbacks.on_error = error_enabled ? output_on_error_audio : NULL;
      error = ti_audio_output_set_callbacks(output, &callbacks, handle);
    }
    handle->pointer = output;
  }
  else if (handle_kind == HANDLE_VIDEO_OUTPUT) {
    TiVideoOutput* output = NULL;
    error = ti_video_output_create(&output);
    if (error == TI_ERROR_OK) {
      TiVideoOutputOptions options = TI_VIDEO_OUTPUT_OPTIONS_INITIALIZER;
      options.decoder_preference = (TiVideoDecoderPreference)decoder;
      error = ti_video_output_set_options(output, &options);
    }
    if (error == TI_ERROR_OK)
      error = ti_video_output_set_buffer_options(output, &buffer);
    if (error == TI_ERROR_OK) {
      TiVideoOutputCallbacks callbacks = TI_VIDEO_OUTPUT_CALLBACKS_INITIALIZER;
      callbacks.on_frame = output_on_video_frame;
      callbacks.on_state_changed = state_enabled ? output_on_state_video : NULL;
      callbacks.on_error = error_enabled ? output_on_error_video : NULL;
      error = ti_video_output_set_callbacks(output, &callbacks, handle);
    }
    handle->pointer = output;
  }
  else if (handle_kind == HANDLE_ENCODED_AUDIO_OUTPUT) {
    TiEncodedAudioOutput* output = NULL;
    error = ti_encoded_audio_output_create(&output);
    if (error == TI_ERROR_OK) {
      TiEncodedAudioOutputCallbacks callbacks = TI_ENCODED_AUDIO_OUTPUT_CALLBACKS_INITIALIZER;
      callbacks.on_frame = output_on_encoded_audio_frame;
      callbacks.on_state_changed = state_enabled ? output_on_state_encoded_audio : NULL;
      callbacks.on_error = error_enabled ? output_on_error_encoded_audio : NULL;
      error = ti_encoded_audio_output_set_callbacks(output, &callbacks, handle);
    }
    handle->pointer = output;
  }
  else {
    TiEncodedVideoOutput* output = NULL;
    error = ti_encoded_video_output_create(&output);
    if (error == TI_ERROR_OK) {
      TiEncodedVideoOutputCallbacks callbacks = TI_ENCODED_VIDEO_OUTPUT_CALLBACKS_INITIALIZER;
      callbacks.on_frame = output_on_encoded_video_frame;
      callbacks.on_state_changed = state_enabled ? output_on_state_encoded_video : NULL;
      callbacks.on_error = error_enabled ? output_on_error_encoded_video : NULL;
      error = ti_encoded_video_output_set_callbacks(output, &callbacks, handle);
    }
    handle->pointer = output;
  }
  Py_END_ALLOW_THREADS if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_output_attach_rtc(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* output_capsule;
  PyObject* connection_capsule;
  unsigned int stream_id;
  if (!PyArg_ParseTuple(arguments, "OOI", &output_capsule, &connection_capsule, &stream_id))
    return NULL;
  NativeHandle* output = get_output(output_capsule);
  NativeHandle* connection = get_handle(connection_capsule, HANDLE_CONNECTION);
  if (output == NULL || connection == NULL)
    return NULL;
  TiError error = TI_ERROR_INVALID_ARGUMENT;
  Py_BEGIN_ALLOW_THREADS switch (output->kind) {
    case HANDLE_AUDIO_OUTPUT:
      error = tirtc_audio_output_attach((TiAudioOutput*)output->pointer,
                                        (TiRtcConn*)connection->pointer, (uint8_t)stream_id);
      break;
    case HANDLE_VIDEO_OUTPUT:
      error = tirtc_video_output_attach((TiVideoOutput*)output->pointer,
                                        (TiRtcConn*)connection->pointer, (uint8_t)stream_id);
      break;
    case HANDLE_ENCODED_AUDIO_OUTPUT:
      error =
          tirtc_encoded_audio_output_attach((TiEncodedAudioOutput*)output->pointer,
                                            (TiRtcConn*)connection->pointer, (uint8_t)stream_id);
      break;
    case HANDLE_ENCODED_VIDEO_OUTPUT:
      error =
          tirtc_encoded_video_output_attach((TiEncodedVideoOutput*)output->pointer,
                                            (TiRtcConn*)connection->pointer, (uint8_t)stream_id);
      break;
    default:
      break;
  }
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static TiError output_detach_rtc(NativeHandle* output) {
  switch (output->kind) {
    case HANDLE_AUDIO_OUTPUT:
      return tirtc_audio_output_detach((TiAudioOutput*)output->pointer);
    case HANDLE_VIDEO_OUTPUT:
      return tirtc_video_output_detach((TiVideoOutput*)output->pointer);
    case HANDLE_ENCODED_AUDIO_OUTPUT:
      return tirtc_encoded_audio_output_detach((TiEncodedAudioOutput*)output->pointer);
    case HANDLE_ENCODED_VIDEO_OUTPUT:
      return tirtc_encoded_video_output_detach((TiEncodedVideoOutput*)output->pointer);
    default:
      return TI_ERROR_INVALID_ARGUMENT;
  }
}

static PyObject* py_output_detach_rtc(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* output = get_output(capsule);
  if (output == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = output_detach_rtc(output);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_output_state(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* output = get_output(capsule);
  if (output == NULL)
    return NULL;
  TiOutputState state = TI_OUTPUT_STATE_IDLE;
  TiError error = TI_ERROR_INVALID_ARGUMENT;
  switch (output->kind) {
    case HANDLE_AUDIO_OUTPUT:
      error = ti_audio_output_get_state((TiAudioOutput*)output->pointer, &state);
      break;
    case HANDLE_VIDEO_OUTPUT:
      error = ti_video_output_get_state((TiVideoOutput*)output->pointer, &state);
      break;
    case HANDLE_ENCODED_AUDIO_OUTPUT:
      error = ti_encoded_audio_output_get_state((TiEncodedAudioOutput*)output->pointer, &state);
      break;
    case HANDLE_ENCODED_VIDEO_OUTPUT:
      error = ti_encoded_video_output_get_state((TiEncodedVideoOutput*)output->pointer, &state);
      break;
    default:
      break;
  }
  return Py_BuildValue("(iI)", error, state);
}

static PyObject* py_output_snapshot(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* output = get_handle(capsule, HANDLE_VIDEO_OUTPUT);
  if (output == NULL)
    return NULL;
  TiVideoSnapshotFile file = TI_VIDEO_SNAPSHOT_FILE_INITIALIZER;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      ti_video_output_take_snapshot((TiVideoOutput*)output->pointer, &file);
  Py_END_ALLOW_THREADS return Py_BuildValue(
      "(is)", error, error == TI_ERROR_OK && file.file_path != NULL ? file.file_path : "");
}

static PyObject* py_close(PyObject* module, PyObject* argument) {
  (void)module;
  NativeHandle* handle = (NativeHandle*)PyCapsule_GetPointer(argument, TIRTC_CAPSULE_NAME);
  if (handle == NULL)
    return NULL;
  if (handle->pointer == NULL)
    return PyLong_FromLong(TI_ERROR_OK);
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = close_pointer(handle);
  Py_END_ALLOW_THREADS if (error == TI_ERROR_OK) mark_closed(handle);
  return PyLong_FromLong(error);
}

static PyObject* py_rtc_recording_start(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  int video;
  int audio;
  if (!PyArg_ParseTuple(arguments, "Oii", &capsule, &video, &audio))
    return NULL;
  NativeHandle* connection = get_handle(capsule, HANDLE_CONNECTION);
  if (connection == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_RTC_RECORDING, NULL, NULL);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiRtcStartRecordingOptions options = {(int32_t)video, (int32_t)audio};
  TiRtcRecordingTask* task = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      tirtc_conn_start_recording((TiRtcConn*)connection->pointer, &options, &task);
  Py_END_ALLOW_THREADS handle->pointer = task;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_storage_recording_start(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  int video;
  int audio;
  if (!PyArg_ParseTuple(arguments, "Oii", &capsule, &video, &audio))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_STORAGE_RECORDING, NULL, NULL);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiCloudStorageStartRecordingOptions options = {(int32_t)video, (int32_t)audio};
  TiCloudStorageRecordingTask* task = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_replay_start_recording(
      (TiCloudStorageReplay*)replay->pointer, &options, &task);
  Py_END_ALLOW_THREADS handle->pointer = task;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_recording_stop(PyObject* module, PyObject* argument) {
  (void)module;
  NativeHandle* handle = (NativeHandle*)PyCapsule_GetPointer(argument, TIRTC_CAPSULE_NAME);
  if (handle == NULL)
    return NULL;
  if (handle->kind != HANDLE_RTC_RECORDING && handle->kind != HANDLE_STORAGE_RECORDING) {
    PyErr_SetString(PyExc_TypeError, "not a recording task");
    return NULL;
  }
  TiError stop_error;
  const char* file_path = "";
  int64_t duration_ms = 0;
  if (handle->kind == HANDLE_RTC_RECORDING) {
    TiRtcMp4File file = {NULL, 0};
    Py_BEGIN_ALLOW_THREADS stop_error =
        tirtc_recording_task_stop((TiRtcRecordingTask*)handle->pointer, &file);
    Py_END_ALLOW_THREADS if (stop_error == TI_ERROR_OK) {
      file_path = file.file_path == NULL ? "" : file.file_path;
      duration_ms = file.duration_ms;
    }
  } else {
    TiCloudStorageMp4File file = {NULL, 0};
    Py_BEGIN_ALLOW_THREADS stop_error =
        ti_cloud_storage_recording_task_stop((TiCloudStorageRecordingTask*)handle->pointer, &file);
    Py_END_ALLOW_THREADS if (stop_error == TI_ERROR_OK) {
      file_path = file.file_path == NULL ? "" : file.file_path;
      duration_ms = file.duration_ms;
    }
  }
  PyObject* path = PyUnicode_FromString(file_path);
  if (path == NULL)
    return NULL;
  PyObject* result = Py_BuildValue("(iOL)", stop_error, path, duration_ms);
  Py_DECREF(path);
  return result;
}

static PyObject* py_rtc_raw_dump_start(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  const char* audio;
  const char* video;
  Py_ssize_t audio_size;
  Py_ssize_t video_size;
  if (!PyArg_ParseTuple(arguments, "Oy#y#", &capsule, &audio, &audio_size, &video, &video_size))
    return NULL;
  NativeHandle* connection = get_handle(capsule, HANDLE_CONNECTION);
  if (connection == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_RAW_DUMP, NULL, capsule);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiRtcRawDumpOptions options = TI_RTC_RAW_DUMP_OPTIONS_INITIALIZER;
  options.audio_stream_ids = (const uint8_t*)audio;
  options.audio_stream_id_count = (size_t)audio_size;
  options.video_stream_ids = (const uint8_t*)video;
  options.video_stream_id_count = (size_t)video_size;
  options.uplink_audio_stream_ids = NULL;
  options.uplink_audio_stream_id_count = 0;
  TiRawDump* dump = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      tirtc_conn_start_raw_dump((TiRtcConn*)connection->pointer, &options, &dump);
  Py_END_ALLOW_THREADS handle->pointer = dump;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_storage_raw_dump_start(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  const char* audio;
  const char* video;
  Py_ssize_t audio_size;
  Py_ssize_t video_size;
  if (!PyArg_ParseTuple(arguments, "Oy#y#", &capsule, &audio, &audio_size, &video, &video_size))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_RAW_DUMP, NULL, capsule);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiCloudStorageRawDumpOptions options = TI_CLOUD_STORAGE_RAW_DUMP_OPTIONS_INITIALIZER;
  options.audio_channel_ids = (const uint8_t*)audio;
  options.audio_channel_id_count = (size_t)audio_size;
  options.video_channel_ids = (const uint8_t*)video;
  options.video_channel_id_count = (size_t)video_size;
  TiRawDump* dump = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_replay_start_raw_dump(
      (TiCloudStorageReplay*)replay->pointer, &options, &dump);
  Py_END_ALLOW_THREADS handle->pointer = dump;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_raw_dump_stop(PyObject* module, PyObject* argument) {
  (void)module;
  NativeHandle* handle = get_handle(argument, HANDLE_RAW_DUMP);
  if (handle == NULL)
    return NULL;
  TiRawDumpArchive value = TI_RAW_DUMP_ARCHIVE_INITIALIZER;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_raw_dump_stop((TiRawDump*)handle->pointer, &value);
  Py_END_ALLOW_THREADS return Py_BuildValue(
      "(issKsKiIKKi)", error,
      error == TI_ERROR_OK && value.capture_id != NULL ? value.capture_id : "",
      error == TI_ERROR_OK && value.file_path != NULL ? value.file_path : "",
      error == TI_ERROR_OK ? value.file_size : 0,
      error == TI_ERROR_OK && value.sha256 != NULL ? value.sha256 : "",
      error == TI_ERROR_OK ? value.captured_duration_ms : 0,
      error == TI_ERROR_OK ? value.capture_complete != 0 : 0,
      error == TI_ERROR_OK ? value.stop_reason : TI_RAW_DUMP_STOP_REASON_UNKNOWN,
      error == TI_ERROR_OK ? value.unsaved_packet_count : 0,
      error == TI_ERROR_OK ? value.unsaved_byte_count : 0,
      error == TI_ERROR_OK ? value.empty != 0 : 1);
}

static PyObject* py_storage_client_create(PyObject* module, PyObject* arguments) {
  (void)module;
  const char* app_id;
  const char* access_key_id;
  const char* access_key_secret;
  const char* endpoint;
  const char* cache_dir;
  int console_log_enabled;
  if (!PyArg_ParseTuple(arguments, "ssszsp", &app_id, &access_key_id, &access_key_secret, &endpoint,
                        &cache_dir, &console_log_enabled))
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_STORAGE_CLIENT, NULL, NULL);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiCloudStorageClientOptions options = TI_CLOUD_STORAGE_CLIENT_OPTIONS_INITIALIZER;
  options.app_id = app_id;
  options.access_key_id = access_key_id;
  options.access_key_secret = access_key_secret;
  options.endpoint = endpoint;
  options.cache_root_dir = cache_dir;
  options.console_log_enabled = (uint8_t)console_log_enabled;
  TiCloudStorageClient* client = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_client_create(&options, &client);
  Py_END_ALLOW_THREADS handle->pointer = client;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_storage_client_open_device(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* client_capsule;
  const char* device_id;
  if (!PyArg_ParseTuple(arguments, "Os", &client_capsule, &device_id))
    return NULL;
  NativeHandle* client = get_handle(client_capsule, HANDLE_STORAGE_CLIENT);
  if (client == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_STORAGE, NULL, client_capsule);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiCloudStorage* storage = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_client_open_device(
      (TiCloudStorageClient*)client->pointer, device_id, &storage);
  Py_END_ALLOW_THREADS handle->pointer = storage;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_storage_create(PyObject* module, PyObject* argument) {
  (void)module;
  PyObject* encoded = NULL;
  const char* token = unicode_utf8(argument, &encoded);
  if (token == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_STORAGE, NULL, NULL);
  if (handle == NULL) {
    Py_DECREF(encoded);
    return NULL;
  }
  PyObject* result = handle_result(handle);
  if (result == NULL) {
    Py_DECREF(encoded);
    return NULL;
  }
  TiCloudStorage* storage = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_create(token, &storage);
  Py_END_ALLOW_THREADS handle->pointer = storage;
  Py_DECREF(encoded);
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_storage_update_token(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  const char* token;
  if (!PyArg_ParseTuple(arguments, "Os", &capsule, &token))
    return NULL;
  NativeHandle* storage = get_handle(capsule, HANDLE_STORAGE);
  if (storage == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      ti_cloud_storage_update_token((TiCloudStorage*)storage->pointer, token);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_storage_list_start(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* storage_capsule;
  const char* kind;
  PyObject* first;
  PyObject* second;
  PyObject* timezone;
  PyObject* callback;
  if (!PyArg_ParseTuple(arguments, "OsOOOO", &storage_capsule, &kind, &first, &second, &timezone,
                        &callback))
    return NULL;
  NativeHandle* storage = get_handle(storage_capsule, HANDLE_STORAGE);
  if (storage == NULL)
    return NULL;
  HandleKind request_kind =
      strcmp(kind, "recordings") == 0 ? HANDLE_RECORDING_REQUEST : HANDLE_DAYS_REQUEST;
  NativeHandle* request = handle_new(request_kind, callback, NULL);
  if (request == NULL)
    return NULL;
  PyObject* result = handle_result(request);
  if (result == NULL)
    return NULL;
  TiError error;
  if (request_kind == HANDLE_RECORDING_REQUEST) {
    int64_t start = PyLong_AsLongLong(first);
    int64_t end = PyLong_AsLongLong(second);
    if (PyErr_Occurred()) {
      Py_DECREF(result);
      return NULL;
    }
    TiCloudStorageRecordingRequestCallbacks callbacks =
        TI_CLOUD_STORAGE_RECORDING_REQUEST_CALLBACKS_INITIALIZER;
    callbacks.on_completed = request_completed_recordings;
    TiCloudStorageRecordingRequest* pointer = NULL;
    Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_list_recordings(
        (TiCloudStorage*)storage->pointer, start, end, &callbacks, request, &pointer);
    Py_END_ALLOW_THREADS request->pointer = pointer;
  } else {
    PyObject* encoded_start = NULL;
    PyObject* encoded_end = NULL;
    PyObject* encoded_zone = NULL;
    const char* start = unicode_utf8(first, &encoded_start);
    const char* end = unicode_utf8(second, &encoded_end);
    const char* zone = unicode_utf8(timezone, &encoded_zone);
    if (start == NULL || end == NULL || zone == NULL) {
      Py_XDECREF(encoded_start);
      Py_XDECREF(encoded_end);
      Py_XDECREF(encoded_zone);
      Py_DECREF(result);
      return NULL;
    }
    TiCloudStorageRecordingDaysRequestCallbacks callbacks =
        TI_CLOUD_STORAGE_RECORDING_DAYS_REQUEST_CALLBACKS_INITIALIZER;
    callbacks.on_completed = request_completed_days;
    TiCloudStorageRecordingDaysRequest* pointer = NULL;
    Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_list_recording_days(
        (TiCloudStorage*)storage->pointer, start, end, zone, &callbacks, request, &pointer);
    Py_END_ALLOW_THREADS request->pointer = pointer;
    Py_DECREF(encoded_start);
    Py_DECREF(encoded_end);
    Py_DECREF(encoded_zone);
  }
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, request);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_request_cancel(PyObject* module, PyObject* argument) {
  (void)module;
  NativeHandle* request = (NativeHandle*)PyCapsule_GetPointer(argument, TIRTC_CAPSULE_NAME);
  if (request == NULL)
    return NULL;
  TiError error;
  if (request->kind != HANDLE_RECORDING_REQUEST && request->kind != HANDLE_DAYS_REQUEST) {
    PyErr_SetString(PyExc_TypeError, "not a list request");
    return NULL;
  }
  Py_BEGIN_ALLOW_THREADS if (request->kind == HANDLE_RECORDING_REQUEST) {
    error = ti_cloud_storage_recording_request_cancel(
        (TiCloudStorageRecordingRequest*)request->pointer);
  }
  else {
    error = ti_cloud_storage_recording_days_request_cancel(
        (TiCloudStorageRecordingDaysRequest*)request->pointer);
  }
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_request_result(PyObject* module, PyObject* argument) {
  (void)module;
  NativeHandle* request = (NativeHandle*)PyCapsule_GetPointer(argument, TIRTC_CAPSULE_NAME);
  if (request == NULL)
    return NULL;
  TiError error = TI_ERROR_OK;
  size_t count = 0;
  PyObject* values = NULL;
  if (request->kind == HANDLE_RECORDING_REQUEST) {
    TiError terminal = TI_ERROR_OK;
    error = ti_cloud_storage_recording_request_get_error(
        (TiCloudStorageRecordingRequest*)request->pointer, &terminal);
    if (error == TI_ERROR_OK)
      error = terminal;
    if (error == TI_ERROR_OK)
      error = ti_cloud_storage_recording_request_get_count(
          (TiCloudStorageRecordingRequest*)request->pointer, &count);
    if (error == TI_ERROR_OK && count > (size_t)PY_SSIZE_T_MAX) {
      error = TI_ERROR_RESOURCE_EXHAUSTED;
    }
    values = PyList_New(error == TI_ERROR_OK ? (Py_ssize_t)count : 0);
    if (values == NULL)
      return NULL;
    for (size_t index = 0; error == TI_ERROR_OK && index < count; ++index) {
      TiCloudStorageRecordingRange range;
      error = ti_cloud_storage_recording_request_get_recording(
          (TiCloudStorageRecordingRequest*)request->pointer, index, &range);
      if (error == TI_ERROR_OK) {
        PyObject* item = Py_BuildValue("(LL)", range.start_time_ms, range.end_time_ms);
        if (item == NULL) {
          Py_DECREF(values);
          return NULL;
        }
        PyList_SetItem(values, (Py_ssize_t)index, item);
      }
    }
  } else if (request->kind == HANDLE_DAYS_REQUEST) {
    TiError terminal = TI_ERROR_OK;
    error = ti_cloud_storage_recording_days_request_get_error(
        (TiCloudStorageRecordingDaysRequest*)request->pointer, &terminal);
    if (error == TI_ERROR_OK)
      error = terminal;
    if (error == TI_ERROR_OK)
      error = ti_cloud_storage_recording_days_request_get_count(
          (TiCloudStorageRecordingDaysRequest*)request->pointer, &count);
    if (error == TI_ERROR_OK && count > (size_t)PY_SSIZE_T_MAX) {
      error = TI_ERROR_RESOURCE_EXHAUSTED;
    }
    values = PyList_New(error == TI_ERROR_OK ? (Py_ssize_t)count : 0);
    if (values == NULL)
      return NULL;
    for (size_t index = 0; error == TI_ERROR_OK && index < count; ++index) {
      TiCloudStorageRecordingDay day;
      error = ti_cloud_storage_recording_days_request_get_day(
          (TiCloudStorageRecordingDaysRequest*)request->pointer, index, &day);
      if (error == TI_ERROR_OK) {
        PyObject* item = Py_BuildValue("(sO)", day.date, day.has_recording ? Py_True : Py_False);
        if (item == NULL) {
          Py_DECREF(values);
          return NULL;
        }
        PyList_SetItem(values, (Py_ssize_t)index, item);
      }
    }
  } else {
    PyErr_SetString(PyExc_TypeError, "not a list request");
    return NULL;
  }
  if (values == NULL)
    return NULL;
  if (error != TI_ERROR_OK) {
    Py_DECREF(values);
    values = PyList_New(0);
    if (values == NULL)
      return NULL;
  }
  PyObject* result = Py_BuildValue("(iO)", error, values);
  Py_DECREF(values);
  return result;
}

static PyObject* py_replay_create(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* storage_capsule;
  PyObject* callback;
  int time_enabled;
  int gap_enabled;
  if (!PyArg_ParseTuple(arguments, "OOpp", &storage_capsule, &callback, &time_enabled,
                        &gap_enabled))
    return NULL;
  NativeHandle* storage = get_handle(storage_capsule, HANDLE_STORAGE);
  if (storage == NULL)
    return NULL;
  NativeHandle* replay = handle_new(HANDLE_REPLAY, callback, NULL);
  if (replay == NULL)
    return NULL;
  PyObject* result = handle_result(replay);
  if (result == NULL)
    return NULL;
  TiCloudStorageReplay* pointer = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      ti_cloud_storage_replay_create((TiCloudStorage*)storage->pointer, &pointer);
  if (error == TI_ERROR_OK) {
    TiCloudStorageReplayCallbacks callbacks = TI_CLOUD_STORAGE_REPLAY_CALLBACKS_INITIALIZER;
    callbacks.on_time_changed = time_enabled ? replay_on_time : NULL;
    callbacks.on_completed = replay_on_completed;
    callbacks.on_error = replay_on_error;
    callbacks.on_recording_gap = gap_enabled ? replay_on_gap : NULL;
    callbacks.dispatcher.dispatch = dispatch_callback_task;
    callbacks.dispatcher.user_data = replay;
    error = ti_cloud_storage_replay_set_callbacks(pointer, &callbacks, replay);
  }
  Py_END_ALLOW_THREADS replay->pointer = pointer;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, replay);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_replay_play(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  int64_t start;
  int64_t end;
  int64_t initial;
  if (!PyArg_ParseTuple(arguments, "OLLL", &capsule, &start, &end, &initial))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      ti_cloud_storage_replay_play_at((TiCloudStorageReplay*)replay->pointer, start, end, initial);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* replay_simple(PyObject* arguments, TiError (*operation)(TiCloudStorageReplay*)) {
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = operation((TiCloudStorageReplay*)replay->pointer);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

#define REPLAY_SIMPLE_METHOD(name, native_name)                  \
  static PyObject* name(PyObject* module, PyObject* arguments) { \
    (void)module;                                                \
    return replay_simple(arguments, native_name);                \
  }

REPLAY_SIMPLE_METHOD(py_replay_pause, ti_cloud_storage_replay_pause)
REPLAY_SIMPLE_METHOD(py_replay_resume, ti_cloud_storage_replay_resume)
REPLAY_SIMPLE_METHOD(py_replay_stop, ti_cloud_storage_replay_stop)

static PyObject* py_replay_seek(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  int64_t value;
  if (!PyArg_ParseTuple(arguments, "OL", &capsule, &value))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      ti_cloud_storage_replay_seek((TiCloudStorageReplay*)replay->pointer, value);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_replay_set_speed(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  unsigned int value;
  if (!PyArg_ParseTuple(arguments, "OI", &capsule, &value))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_replay_set_speed(
      (TiCloudStorageReplay*)replay->pointer, (TiCloudStorageReplaySpeed)value);
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_replay_get_speed(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  TiCloudStorageReplaySpeed value = TI_CLOUD_STORAGE_REPLAY_SPEED_1X;
  TiError error = ti_cloud_storage_replay_get_speed((TiCloudStorageReplay*)replay->pointer, &value);
  return Py_BuildValue("(iI)", error, value);
}

static PyObject* py_replay_current_time(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* replay = get_handle(capsule, HANDLE_REPLAY);
  if (replay == NULL)
    return NULL;
  uint8_t present = 0;
  int64_t value = 0;
  TiError error = ti_cloud_storage_replay_get_current_time_ms(
      (TiCloudStorageReplay*)replay->pointer, &present, &value);
  return Py_BuildValue("(iOL)", error, present ? Py_True : Py_False, value);
}

static PyObject* py_output_attach_storage(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* output_capsule;
  PyObject* replay_capsule;
  unsigned int channel_id;
  if (!PyArg_ParseTuple(arguments, "OOI", &output_capsule, &replay_capsule, &channel_id))
    return NULL;
  NativeHandle* output = get_output(output_capsule);
  NativeHandle* replay = get_handle(replay_capsule, HANDLE_REPLAY);
  if (output == NULL || replay == NULL)
    return NULL;
  TiError error = TI_ERROR_INVALID_ARGUMENT;
  Py_BEGIN_ALLOW_THREADS switch (output->kind) {
    case HANDLE_AUDIO_OUTPUT:
      error = ti_cloud_storage_audio_output_attach((TiAudioOutput*)output->pointer,
                                                   (TiCloudStorageReplay*)replay->pointer,
                                                   (uint8_t)channel_id);
      break;
    case HANDLE_VIDEO_OUTPUT:
      error = ti_cloud_storage_video_output_attach((TiVideoOutput*)output->pointer,
                                                   (TiCloudStorageReplay*)replay->pointer,
                                                   (uint8_t)channel_id);
      break;
    case HANDLE_ENCODED_AUDIO_OUTPUT:
      error = ti_cloud_storage_encoded_audio_output_attach((TiEncodedAudioOutput*)output->pointer,
                                                           (TiCloudStorageReplay*)replay->pointer,
                                                           (uint8_t)channel_id);
      break;
    case HANDLE_ENCODED_VIDEO_OUTPUT:
      error = ti_cloud_storage_encoded_video_output_attach((TiEncodedVideoOutput*)output->pointer,
                                                           (TiCloudStorageReplay*)replay->pointer,
                                                           (uint8_t)channel_id);
      break;
    default:
      break;
  }
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_output_detach_storage(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* output = get_output(capsule);
  if (output == NULL)
    return NULL;
  TiError error = TI_ERROR_INVALID_ARGUMENT;
  Py_BEGIN_ALLOW_THREADS switch (output->kind) {
    case HANDLE_AUDIO_OUTPUT:
      error = ti_cloud_storage_audio_output_detach((TiAudioOutput*)output->pointer);
      break;
    case HANDLE_VIDEO_OUTPUT:
      error = ti_cloud_storage_video_output_detach((TiVideoOutput*)output->pointer);
      break;
    case HANDLE_ENCODED_AUDIO_OUTPUT:
      error = ti_cloud_storage_encoded_audio_output_detach((TiEncodedAudioOutput*)output->pointer);
      break;
    case HANDLE_ENCODED_VIDEO_OUTPUT:
      error = ti_cloud_storage_encoded_video_output_detach((TiEncodedVideoOutput*)output->pointer);
      break;
    default:
      break;
  }
  Py_END_ALLOW_THREADS return PyLong_FromLong(error);
}

static PyObject* py_export_create(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* storage_capsule;
  int64_t start;
  int64_t end;
  int video;
  int audio;
  PyObject* callback;
  int progress_enabled;
  int gap_enabled;
  if (!PyArg_ParseTuple(arguments, "OLLiiOpp", &storage_capsule, &start, &end, &video, &audio,
                        &callback, &progress_enabled, &gap_enabled))
    return NULL;
  NativeHandle* storage = get_handle(storage_capsule, HANDLE_STORAGE);
  if (storage == NULL)
    return NULL;
  NativeHandle* handle = handle_new(HANDLE_EXPORT, callback, NULL);
  if (handle == NULL)
    return NULL;
  PyObject* result = handle_result(handle);
  if (result == NULL)
    return NULL;
  TiCloudStorageExportOptions options = {start, end, video, audio};
  TiCloudStorageExportCallbacks callbacks = TI_CLOUD_STORAGE_EXPORT_CALLBACKS_INITIALIZER;
  callbacks.on_progress_detail = progress_enabled ? export_on_progress : NULL;
  callbacks.on_recording_gap = gap_enabled ? export_on_gap : NULL;
  callbacks.on_completed = export_on_completed;
  callbacks.dispatcher.dispatch = dispatch_callback_task;
  callbacks.dispatcher.user_data = handle;
  TiCloudStorageExportTask* task = NULL;
  TiError error;
  Py_BEGIN_ALLOW_THREADS error = ti_cloud_storage_export_recording(
      (TiCloudStorage*)storage->pointer, &options, &callbacks, handle, &task);
  Py_END_ALLOW_THREADS handle->pointer = task;
  if (error != TI_ERROR_OK) {
    discard_handle_result(result, handle);
    return Py_BuildValue("(iO)", error, Py_None);
  }
  return result;
}

static PyObject* py_export_progress(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* task = get_handle(capsule, HANDLE_EXPORT);
  if (task == NULL)
    return NULL;
  TiCloudStorageExportProgress progress = {0, 0};
  TiError error = ti_cloud_storage_export_task_get_progress_detail(
      (TiCloudStorageExportTask*)task->pointer, &progress);
  return Py_BuildValue("(idL)", error, progress.fraction, progress.covered_duration_ms);
}

static PyObject* py_export_report(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* task = get_handle(capsule, HANDLE_EXPORT);
  if (task == NULL)
    return NULL;
  TiCloudStorageExportReport report = {0};
  TiError error =
      ti_cloud_storage_export_task_get_report((TiCloudStorageExportTask*)task->pointer, &report);
  if (error != TI_ERROR_OK)
    return Py_BuildValue("(iO)", error, Py_None);
  PyObject* segments = PyTuple_New((Py_ssize_t)report.segment_count);
  PyObject* gaps = PyTuple_New((Py_ssize_t)report.gap_count);
  PyObject* unprocessed = PyTuple_New((Py_ssize_t)report.unprocessed_range_count);
  if (segments == NULL || gaps == NULL || unprocessed == NULL) {
    Py_XDECREF(segments);
    Py_XDECREF(gaps);
    Py_XDECREF(unprocessed);
    return NULL;
  }
  for (size_t index = 0; index < report.segment_count; ++index) {
    TiCloudStorageExportSegment segment = {0};
    error = ti_cloud_storage_export_task_get_segment((TiCloudStorageExportTask*)task->pointer,
                                                     index, &segment);
    PyObject* value = error == TI_ERROR_OK
                          ? Py_BuildValue("(LLLL)", segment.source_range.start_time_ms,
                                          segment.source_range.end_time_ms, segment.output_start_ms,
                                          segment.output_end_ms)
                          : NULL;
    if (value == NULL) {
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return error == TI_ERROR_OK ? NULL : Py_BuildValue("(iO)", error, Py_None);
    }
    if (PyTuple_SetItem(segments, (Py_ssize_t)index, value) != 0) {
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return NULL;
    }
  }
  for (size_t index = 0; index < report.gap_count; ++index) {
    TiCloudStorageRecordingGap native_gap = {0};
    error = ti_cloud_storage_export_task_get_gap((TiCloudStorageExportTask*)task->pointer, index,
                                                 &native_gap);
    if (error != TI_ERROR_OK) {
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return Py_BuildValue("(iO)", error, Py_None);
    }
    TiCloudStorageRecordingTrack* tracks =
        (TiCloudStorageRecordingTrack*)calloc(native_gap.track_count, sizeof(*tracks));
    TiCloudStorageRecordingGapReason* reasons = (TiCloudStorageRecordingGapReason*)calloc(
        native_gap.reason_count, sizeof(*reasons));
    if ((native_gap.track_count != 0 && tracks == NULL) ||
        (native_gap.reason_count != 0 && reasons == NULL)) {
      free(tracks);
      free(reasons);
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return PyErr_NoMemory();
    }
    for (size_t item = 0; item < native_gap.track_count && error == TI_ERROR_OK; ++item)
      error = ti_cloud_storage_export_task_get_gap_track(
          (TiCloudStorageExportTask*)task->pointer, index, item, &tracks[item]);
    for (size_t item = 0; item < native_gap.reason_count && error == TI_ERROR_OK; ++item)
      error = ti_cloud_storage_export_task_get_gap_reason(
          (TiCloudStorageExportTask*)task->pointer, index, item, &reasons[item]);
    native_gap.tracks = tracks;
    native_gap.reasons = reasons;
    PyObject* value = error == TI_ERROR_OK ? recording_gap_value(&native_gap) : NULL;
    free(tracks);
    free(reasons);
    if (value == NULL) {
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return error == TI_ERROR_OK ? NULL : Py_BuildValue("(iO)", error, Py_None);
    }
    if (PyTuple_SetItem(gaps, (Py_ssize_t)index, value) != 0) {
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return NULL;
    }
  }
  for (size_t index = 0; index < report.unprocessed_range_count; ++index) {
    TiCloudStorageRecordingRange range = {0};
    error = ti_cloud_storage_export_task_get_unprocessed_range(
        (TiCloudStorageExportTask*)task->pointer, index, &range);
    PyObject* value = error == TI_ERROR_OK
                          ? Py_BuildValue("(LL)", range.start_time_ms, range.end_time_ms)
                          : NULL;
    if (value == NULL) {
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return error == TI_ERROR_OK ? NULL : Py_BuildValue("(iO)", error, Py_None);
    }
    if (PyTuple_SetItem(unprocessed, (Py_ssize_t)index, value) != 0) {
      Py_DECREF(segments);
      Py_DECREF(gaps);
      Py_DECREF(unprocessed);
      return NULL;
    }
  }
  PyObject* value = Py_BuildValue(
      "(LLLiiiNNN)", report.requested_range.start_time_ms, report.requested_range.end_time_ms,
      report.covered_duration_ms, report.complete != 0, report.termination, report.cause, segments,
      gaps, unprocessed);
  if (value == NULL)
    return NULL;
  return Py_BuildValue("(iN)", TI_ERROR_OK, value);
}

static PyObject* py_export_stop(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* task = get_handle(capsule, HANDLE_EXPORT);
  if (task == NULL)
    return NULL;
  TiCloudStorageMp4File file = {NULL, 0};
  TiError error;
  Py_BEGIN_ALLOW_THREADS error =
      ti_cloud_storage_export_task_stop((TiCloudStorageExportTask*)task->pointer, &file);
  Py_END_ALLOW_THREADS return Py_BuildValue(
      "(isL)", error, error == TI_ERROR_OK && file.file_path != NULL ? file.file_path : "",
      error == TI_ERROR_OK ? file.duration_ms : 0);
}

static PyObject* py_export_cancel(PyObject* module, PyObject* arguments) {
  (void)module;
  PyObject* capsule;
  if (!PyArg_ParseTuple(arguments, "O", &capsule))
    return NULL;
  NativeHandle* task = get_handle(capsule, HANDLE_EXPORT);
  if (task == NULL)
    return NULL;
  return PyLong_FromLong(
      ti_cloud_storage_export_task_cancel((TiCloudStorageExportTask*)task->pointer));
}

static PyMethodDef module_methods[] = {
    {"run_callback_task", py_run_callback_task, METH_O, NULL},
    {"mark_finalizing", py_mark_finalizing, METH_NOARGS, NULL},
    {"error_name", py_error_name, METH_O, NULL},
    {"rtc_initialize", py_rtc_initialize, METH_VARARGS, NULL},
    {"rtc_shutdown", py_rtc_shutdown, METH_NOARGS, NULL},
    {"storage_initialize", py_storage_initialize, METH_VARARGS, NULL},
    {"storage_shutdown", py_storage_shutdown, METH_NOARGS, NULL},
    {"upload_logs", py_upload_logs, METH_NOARGS, NULL},
    {"write_log", py_write_log, METH_VARARGS, NULL},
    {"delete_media_file", py_delete_media_file, METH_O, NULL},
    {"rtc_client_create", py_rtc_client_create, METH_VARARGS, NULL},
    {"rtc_client_create_connection", py_rtc_client_create_connection, METH_VARARGS, NULL},
    {"conn_create", py_conn_create, METH_VARARGS, NULL},
    {"conn_connect", py_conn_connect, METH_VARARGS, NULL},
    {"conn_connect_managed", py_conn_connect_managed, METH_VARARGS, NULL},
    {"connect_attempt_cancel", py_connect_attempt_cancel, METH_O, NULL},
    {"conn_disconnect", py_conn_disconnect, METH_VARARGS, NULL},
    {"conn_send_command", py_conn_send_command, METH_VARARGS, NULL},
    {"conn_send_message", py_conn_send_message, METH_VARARGS, NULL},
    {"conn_subscribe_audio", py_conn_subscribe_audio, METH_VARARGS, NULL},
    {"conn_unsubscribe_audio", py_conn_unsubscribe_audio, METH_VARARGS, NULL},
    {"conn_subscribe_video", py_conn_subscribe_video, METH_VARARGS, NULL},
    {"conn_unsubscribe_video", py_conn_unsubscribe_video, METH_VARARGS, NULL},
    {"conn_request_video_keyframe", py_conn_request_video_keyframe, METH_VARARGS, NULL},
    {"output_create", py_output_create, METH_VARARGS, NULL},
    {"output_attach_rtc", py_output_attach_rtc, METH_VARARGS, NULL},
    {"output_detach_rtc", py_output_detach_rtc, METH_VARARGS, NULL},
    {"output_attach_storage", py_output_attach_storage, METH_VARARGS, NULL},
    {"output_detach_storage", py_output_detach_storage, METH_VARARGS, NULL},
    {"output_state", py_output_state, METH_VARARGS, NULL},
    {"output_snapshot", py_output_snapshot, METH_VARARGS, NULL},
    {"close", py_close, METH_O, NULL},
    {"rtc_recording_start", py_rtc_recording_start, METH_VARARGS, NULL},
    {"storage_recording_start", py_storage_recording_start, METH_VARARGS, NULL},
    {"recording_stop", py_recording_stop, METH_O, NULL},
    {"rtc_raw_dump_start", py_rtc_raw_dump_start, METH_VARARGS, NULL},
    {"storage_raw_dump_start", py_storage_raw_dump_start, METH_VARARGS, NULL},
    {"raw_dump_stop", py_raw_dump_stop, METH_O, NULL},
    {"storage_client_create", py_storage_client_create, METH_VARARGS, NULL},
    {"storage_client_open_device", py_storage_client_open_device, METH_VARARGS, NULL},
    {"storage_create", py_storage_create, METH_O, NULL},
    {"storage_update_token", py_storage_update_token, METH_VARARGS, NULL},
    {"storage_list_start", py_storage_list_start, METH_VARARGS, NULL},
    {"request_cancel", py_request_cancel, METH_O, NULL},
    {"request_result", py_request_result, METH_O, NULL},
    {"replay_create", py_replay_create, METH_VARARGS, NULL},
    {"replay_play", py_replay_play, METH_VARARGS, NULL},
    {"replay_pause", py_replay_pause, METH_VARARGS, NULL},
    {"replay_resume", py_replay_resume, METH_VARARGS, NULL},
    {"replay_seek", py_replay_seek, METH_VARARGS, NULL},
    {"replay_set_speed", py_replay_set_speed, METH_VARARGS, NULL},
    {"replay_get_speed", py_replay_get_speed, METH_VARARGS, NULL},
    {"replay_current_time", py_replay_current_time, METH_VARARGS, NULL},
    {"replay_stop", py_replay_stop, METH_VARARGS, NULL},
    {"export_create", py_export_create, METH_VARARGS, NULL},
    {"export_progress", py_export_progress, METH_VARARGS, NULL},
    {"export_report", py_export_report, METH_VARARGS, NULL},
    {"export_cancel", py_export_cancel, METH_VARARGS, NULL},
    {"export_stop", py_export_stop, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT, "_native", NULL, 0, module_methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit__native(void) {
  frame_buffer_type = PyType_FromSpec(&frame_buffer_spec);
  if (frame_buffer_type == NULL)
    return NULL;
  PyObject* module = PyModule_Create(&module_definition);
  if (module == NULL) {
    Py_DECREF(frame_buffer_type);
    frame_buffer_type = NULL;
    return NULL;
  }
  return module;
}
