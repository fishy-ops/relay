"""Job handlers, selected by job kind.

- echo: placeholder work for tests and chaos runs. It sleeps for input.sleep_seconds
  and can be told to fail with input.fail.
- upscale: reads an image from object storage, runs it through an ONNX model, and
  writes a PNG back to storage. Billed per input megapixel.
"""

import io
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np
from PIL import Image

from relay.storage import Storage, key_belongs_to, output_key

ALLOWED_KINDS = {"echo", "upscale"}
DEFAULT_MODEL_PATH = "models/upscale_2x.onnx"


class JobInputError(ValueError):
    """The job's input is unusable. Retrying will not help, so the job fails fast."""


@dataclass
class Result:
    output: dict[str, Any]
    output_key: str | None = None
    usage_units: int = 0


@dataclass
class HandlerContext:
    tenant_id: UUID
    storage: Storage | None
    model_path: str = DEFAULT_MODEL_PATH


_sessions: dict[str, Any] = {}
_session_lock = threading.Lock()


def _session(model_path: str) -> Any:
    """One ONNX session per model file, shared across jobs in this process."""
    import onnxruntime as ort

    with _session_lock:
        if model_path not in _sessions:
            if not os.path.exists(model_path):
                raise RuntimeError(f"model not found: {model_path}")
            _sessions[model_path] = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
        return _sessions[model_path]


def run_echo(job: dict[str, Any], ctx: HandlerContext) -> Result:
    payload = job["input"]
    time.sleep(float(payload.get("sleep_seconds", 0)))
    if payload.get("fail"):
        raise RuntimeError("requested failure")
    return Result(output={"echo": payload})


def run_upscale(job: dict[str, Any], ctx: HandlerContext) -> Result:
    if ctx.storage is None:
        raise RuntimeError("upscale needs object storage configured")
    input_key = job["input_key"]
    if not input_key or not key_belongs_to(input_key, ctx.tenant_id):
        raise JobInputError("input_key must reference an upload owned by this tenant")

    try:
        image = Image.open(io.BytesIO(ctx.storage.get_bytes(input_key))).convert("RGB")
    except Exception as exc:  # corrupt or non-image input
        raise JobInputError(f"input is not a readable image: {exc}") from exc

    width, height = image.size
    tensor = np.asarray(image, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    session = _session(ctx.model_path)
    upscaled = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
    pixels = (np.clip(upscaled, 0.0, 1.0) * 255.0).round().astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels.transpose(1, 2, 0)).save(buf, format="PNG")

    key = output_key(ctx.tenant_id, job["id"])
    ctx.storage.put_bytes(key, buf.getvalue(), "image/png")
    return Result(
        output={"width": width * 2, "height": height * 2},
        output_key=key,
        usage_units=max(1, math.ceil(width * height / 1_000_000)),
    )


HANDLERS = {"echo": run_echo, "upscale": run_upscale}


def execute(job: dict[str, Any], ctx: HandlerContext) -> Result:
    handler = HANDLERS.get(job["kind"])
    if handler is None:
        raise JobInputError(f"unknown job kind: {job['kind']}")
    return handler(job, ctx)
