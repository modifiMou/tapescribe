"""Speech recognition back ends.

"piece" providers transcribe one short clip at a time and return plain text.
"file" providers take the whole recording and return timestamped sentences.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from .settings import resolve_api_key

LANGUAGE_CODES = {
    "Chinese": "zh", "English": "en", "Japanese": "ja", "Korean": "ko", "Cantonese": "yue",
}
TIMEOUT = httpx.Timeout(connect=10, read=600, write=600, pool=10)


class ProviderError(RuntimeError):
    pass


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value
            if value:
                return f"HTTP {response.status_code}: {value}"
    return f"HTTP {response.status_code}: {str(body)[:300]}"


class Provider:
    mode = "piece"

    def __init__(self, config: dict):
        self.config = config
        self.base = config["base_url"].rstrip("/")
        self.model = config.get("model", "")
        self.extra = config.get("extra") or {}
        self.key = resolve_api_key(config)
        rate = int(config.get("rate_per_min") or 0)
        self.min_interval = 60.0 / rate if rate > 0 else 0.0
        self._last_submit = 0.0

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    async def _throttle(self) -> None:
        wait = self._last_submit + self.min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_submit = time.monotonic()

    async def test(self, client: httpx.AsyncClient) -> str:
        raise NotImplementedError

    async def transcribe_piece(
        self, client: httpx.AsyncClient, wav: bytes, language: str, context: str
    ) -> str:
        raise NotImplementedError

    async def transcribe_file(
        self,
        client: httpx.AsyncClient,
        path: Path,
        language: str,
        context: str,
        on_status: Callable[[str], None],
    ) -> list[dict]:
        raise NotImplementedError


class MlxProvider(Provider):
    """mlx-qwen3-asr server: POST /transcribe returns a job, GET /jobs/{id} polls it."""

    async def test(self, client: httpx.AsyncClient) -> str:
        response = await client.get(f"{self.base}/health", headers=self.headers)
        if response.status_code >= 400:
            raise ProviderError(_detail(response))
        body = response.json()
        model = Path(str(body.get("model", ""))).name
        return f"服务正常，模型 {model}，排队 {body.get('queued_jobs', 0)} 个任务"

    async def transcribe_piece(self, client, wav, language, context):
        data = {"timestamps": "false"}
        if language:
            data["language"] = language
        if context:
            data["context"] = context
        for attempt in range(6):
            await self._throttle()
            response = await client.post(
                f"{self.base}/transcribe",
                headers=self.headers,
                files={"audio": ("piece.wav", wav, "audio/wav")},
                data=data,
            )
            if response.status_code in (429, 503) and attempt < 5:
                retry_after = response.headers.get("Retry-After", "")
                await asyncio.sleep(float(retry_after) if retry_after.isdigit() else 5.0)
                continue
            if response.status_code >= 400:
                raise ProviderError(_detail(response))
            break
        job_id = response.json()["job_id"]
        while True:
            await asyncio.sleep(0.25)
            poll = await client.get(f"{self.base}/jobs/{job_id}", headers=self.headers)
            if poll.status_code >= 400:
                raise ProviderError(_detail(poll))
            body = poll.json()
            if body.get("status") == "completed":
                return str((body.get("result") or {}).get("text", "")).strip()
            if body.get("status") == "failed":
                raise ProviderError(str(body.get("error") or "识别失败"))


class OpenAIProvider(Provider):
    """Any OpenAI-compatible POST {base}/audio/transcriptions endpoint."""

    async def test(self, client: httpx.AsyncClient) -> str:
        response = await client.get(f"{self.base}/models", headers=self.headers)
        if response.status_code >= 400:
            raise ProviderError(_detail(response))
        return "接口可访问，密钥有效"

    async def transcribe_piece(self, client, wav, language, context):
        data = {"model": self.model, "response_format": "json"}
        if language:
            data["language"] = LANGUAGE_CODES.get(language, language)
        if context:
            data["prompt"] = context
        data.update({k: str(v) for k, v in self.extra.items()})
        for attempt in range(6):
            await self._throttle()
            response = await client.post(
                f"{self.base}/audio/transcriptions",
                headers=self.headers,
                files={"file": ("piece.wav", wav, "audio/wav")},
                data=data,
            )
            if response.status_code in (429, 503) and attempt < 5:
                retry_after = response.headers.get("Retry-After", "")
                await asyncio.sleep(float(retry_after) if retry_after.isdigit() else 5.0)
                continue
            if response.status_code >= 400:
                raise ProviderError(_detail(response))
            break
        try:
            return str(response.json().get("text", "")).strip()
        except ValueError:
            return response.text.strip()


class DashScopeFileTransProvider(Provider):
    """Alibaba Cloud Bailian recorded-file recognition (asynchronous task API).

    The recording is uploaded to DashScope's temporary storage, submitted as an
    async transcription task, polled, and the sentence list is downloaded.
    """

    mode = "file"

    def _require_key(self) -> None:
        if not self.key:
            raise ProviderError("未配置 API Key")

    async def _policy(self, client: httpx.AsyncClient) -> dict:
        self._require_key()
        response = await client.get(
            f"{self.base}/uploads",
            params={"action": "getPolicy", "model": self.model},
            headers=self.headers,
        )
        if response.status_code >= 400:
            raise ProviderError(_detail(response))
        return response.json()["data"]

    async def test(self, client: httpx.AsyncClient) -> str:
        await self._policy(client)
        return "密钥有效，可获取上传凭证（模型名称在提交识别任务时才会校验）"

    async def _upload(self, client: httpx.AsyncClient, path: Path) -> str:
        policy = await self._policy(client)
        key = f"{policy['upload_dir']}/{path.name}"
        with path.open("rb") as handle:
            response = await client.post(
                policy["upload_host"],
                data={
                    "OSSAccessKeyId": policy["oss_access_key_id"],
                    "Signature": policy["signature"],
                    "policy": policy["policy"],
                    "x-oss-object-acl": policy["x_oss_object_acl"],
                    "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
                    "key": key,
                    "success_action_status": "200",
                },
                files={"file": (path.name, handle, "application/octet-stream")},
            )
        if response.status_code >= 400:
            raise ProviderError(f"上传音频失败: {_detail(response)}")
        return f"oss://{key}"

    async def transcribe_file(self, client, path, language, context, on_status):
        on_status("上传音频到云端")
        file_url = await self._upload(client, path)

        parameters: dict = {"channel_id": [0]}
        if language:
            parameters["language"] = LANGUAGE_CODES.get(language, language)
        payload_input: dict = (
            {"file_url": file_url} if "filetrans" in self.model else {"file_urls": [file_url]}
        )
        extra = dict(self.extra)
        payload_input.update(extra.pop("input", {}) or {})
        parameters.update(extra.pop("parameters", {}) or {})
        parameters.update(extra)
        if context and "context" not in parameters:
            parameters["context"] = context

        on_status("提交云端识别任务")
        response = await client.post(
            f"{self.base}/services/audio/asr/transcription",
            headers={
                **self.headers,
                "X-DashScope-Async": "enable",
                "X-DashScope-OssResourceResolve": "enable",
            },
            json={"model": self.model, "input": payload_input, "parameters": parameters},
        )
        if response.status_code >= 400:
            raise ProviderError(_detail(response))
        task_id = (response.json().get("output") or {}).get("task_id")
        if not task_id:
            raise ProviderError(f"云端未返回任务编号: {response.text[:300]}")

        on_status("云端识别中")
        while True:
            await asyncio.sleep(3)
            poll = await client.get(f"{self.base}/tasks/{task_id}", headers=self.headers)
            if poll.status_code >= 400:
                raise ProviderError(_detail(poll))
            output = poll.json().get("output") or {}
            status = output.get("task_status")
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                message = output.get("message") or output.get("code") or status
                for item in output.get("results") or []:
                    message = item.get("message") or message
                raise ProviderError(f"云端识别失败: {message}")

        urls = []
        result = output.get("result") or {}
        if result.get("transcription_url"):
            urls.append(result["transcription_url"])
        for item in output.get("results") or []:
            if item.get("transcription_url"):
                urls.append(item["transcription_url"])
        if not urls:
            raise ProviderError("云端任务成功但没有返回识别结果地址")

        on_status("下载识别结果")
        sentences: list[dict] = []
        for url in urls:
            fetched = await client.get(url)
            if fetched.status_code >= 400:
                raise ProviderError(f"下载识别结果失败: HTTP {fetched.status_code}")
            for transcript in fetched.json().get("transcripts") or []:
                for sentence in transcript.get("sentences") or []:
                    text = str(sentence.get("text", "")).strip()
                    if text:
                        sentences.append(
                            {
                                "start": float(sentence.get("begin_time", 0)) / 1000.0,
                                "end": float(sentence.get("end_time", 0)) / 1000.0,
                                "text": text,
                            }
                        )
        sentences.sort(key=lambda s: s["start"])
        return sentences


_CLASSES = {
    "mlx": MlxProvider,
    "openai": OpenAIProvider,
    "dashscope_filetrans": DashScopeFileTransProvider,
}


def create(config: dict) -> Provider:
    cls: Optional[type] = _CLASSES.get(config.get("type", ""))
    if cls is None:
        raise ProviderError(f"不支持的接入类型: {config.get('type')}")
    return cls(config)
