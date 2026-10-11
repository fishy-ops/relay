"""Small Python client for Relay. Covers upload, submit, wait, download, and webhook checks.

    client = RelayClient("https://relay.example.com", api_key)
    input_key = client.upload(open("clip.png", "rb").read(), "image/png")
    job = client.submit("upscale", input_key=input_key, webhook_url="https://app/hook")
    job = client.wait(job["id"])
    data = client.download(job["output_url"])

Retries are safe: submit() always sends an Idempotency-Key, so a retried request
returns the original job rather than creating a second one.
"""

import time
import uuid
from typing import Any

import httpx

from relay.webhooks import verify_signature as _verify

TERMINAL = {"succeeded", "failed"}


class RelayError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class RelayClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def _check(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RelayError(response.status_code, detail)
        return response.json()

    def upload(self, data: bytes, content_type: str) -> str:
        """Upload straight to object storage with the presigned POST. Returns the input_key."""
        presigned = self._check(self._http.post("/v1/uploads", json={"content_type": content_type}))
        files = {"file": ("upload", data, content_type)}
        response = httpx.post(presigned["url"], data=presigned["fields"], files=files, timeout=60)
        if response.status_code >= 400:
            raise RelayError(response.status_code, response.text[:300])
        return presigned["key"]

    def submit(
        self,
        kind: str,
        input: dict[str, Any] | None = None,
        input_key: str | None = None,
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": kind, "input": input or {}}
        if input_key:
            body["input_key"] = input_key
        if webhook_url:
            body["webhook_url"] = webhook_url
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        return self._check(self._http.post("/v1/jobs", json=body, headers=headers))

    def get(self, job_id: str) -> dict[str, Any]:
        return self._check(self._http.get(f"/v1/jobs/{job_id}"))

    def wait(self, job_id: str, timeout: float = 300, poll: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job["status"] in TERMINAL:
                return job
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} still {job['status']} after {timeout}s")
            time.sleep(poll)

    def download(self, output_url: str) -> bytes:
        response = httpx.get(output_url, timeout=60)
        if response.status_code >= 400:
            raise RelayError(response.status_code, "download failed (link may have expired)")
        return response.content


def verify_webhook(secret: str, headers: dict[str, str], body: bytes) -> bool:
    """Check a delivery's Relay-Signature. Also dedupe on Relay-Event-Id in your handler."""
    header = headers.get("Relay-Signature") or headers.get("relay-signature") or ""
    return _verify(secret, header, body)
