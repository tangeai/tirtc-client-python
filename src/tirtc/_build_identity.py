from __future__ import annotations

import threading

RELEASE_VERSION = '2.6.0-alpha.3'
BUILD_TIME_UTC = '2026-10-22T18:51:50Z'
SOURCE_REVISION = '3e607aa93bb48c47bff3dcd1e16ad20cba01eeb4'
SOURCE_DIRTY = 0

_lock = threading.Lock()
_logged_products: set[str] = set()

def log(product: str, native: object) -> None:
    with _lock:
        if product in _logged_products:
            return
        _logged_products.add(product)
    try:
        code = native.write_log(1, 'TIRTC_PYTHON', '[build_identity] summary: component build identity; component=python ' + f'product={product} release_version={RELEASE_VERSION} build_time_utc={BUILD_TIME_UTC} ' + f'source_revision={SOURCE_REVISION} source_dirty={SOURCE_DIRTY}')
    except Exception:
        code = -1
    if code != 0:
        with _lock:
            _logged_products.discard(product)
