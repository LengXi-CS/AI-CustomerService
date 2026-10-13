"""WeChat adapter via GeWe API for sending messages, images, files, and quotes."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from fastgpt_assistant.core.wechat_quote import TextQuote

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_@.-]{1,128}$")


class GeweError(RuntimeError):
    """Base exception for GeWe communication failures."""


class WechatGeweClient:
    """Clean HTTP client for GeWe WeChat service."""

    def __init__(
        self,
        base_url: str = "http://api.geweapi.com",
        token: str = "",
        app_id: str = "",
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.app_id = app_id
        self.timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WechatGeweClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "X-GEWE-TOKEN": self.token,
            "Content-Type": "application/json",
        }

    def check_online(self) -> bool:
        """Check whether the WeChat bot account is online."""
        payload = {"appId": self.app_id}
        resp = self._client.post(
            "/gewe/v2/api/login/checkOnline",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return bool(data.get("ret") == 200 and data.get("data") is True)

    def set_callback(self, callback_url: str) -> dict[str, Any]:
        """Register the inbound webhook callback URL with GeWe cloud."""
        if not callback_url:
            raise ValueError("callback_url must be non-empty")
        payload = {
            "token": self.token,
            "appId": self.app_id,
            "callbackUrl": callback_url.strip(),
        }
        resp = self._client.post(
            "/gewe/v2/api/login/setCallback",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != 200:
            raise GeweError(f"GeWe setCallback failed: {data.get('msg')}")
        logger.info("Successfully registered GeWe callback: %s", callback_url)
        return data

    def send_text(self, to_wxid: str, content: str) -> dict[str, Any]:
        """Send plain text message to a user or room."""
        if not to_wxid or not content:
            raise ValueError("to_wxid and content must be non-empty")
        payload = {
            "appId": self.app_id,
            "toWxid": to_wxid.strip(),
            "content": content,
        }
        resp = self._client.post(
            "/gewe/v2/api/message/postText",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != 200:
            raise GeweError(f"GeWe postText failed: {data.get('msg')}")
        return data

    def send_quoted_text(
        self,
        to_wxid: str,
        content: str,
        *,
        quoted_msg_id: str,
        quoted_sender_id: str,
        quoted_chat_id: str,
        quoted_content: str,
        quoted_sender_name: str = "",
        quoted_created_at: int = 0,
        quoted_msg_source: str = "",
        quoted_str_id: str = "",
        quoted_imunion_sender_id: str = "",
    ) -> dict[str, Any]:
        """Send WeChat Type-57 native reference quote message."""
        appmsg_xml = TextQuote(
            message_id=quoted_msg_id.strip(), chat_id=quoted_chat_id.strip(),
            sender_id=quoted_sender_id.strip(), content=quoted_content,
            sender_name=quoted_sender_name, created_at=quoted_created_at,
            msg_source=quoted_msg_source, str_id=quoted_str_id,
            imunion_sender_id=quoted_imunion_sender_id,
        ).appmsg_xml(content)
        payload = {
            "appId": self.app_id,
            "toWxid": to_wxid.strip(),
            "appmsg": appmsg_xml,
        }
        resp = self._client.post(
            "/gewe/v2/api/message/postAppMsg",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != 200:
            raise GeweError(f"GeWe postAppMsg failed: {data.get('msg')}")
        return data

    def send_image(self, to_wxid: str, img_url: str) -> dict[str, Any]:
        """Send image attachment via publicly accessible URL."""
        payload = {
            "appId": self.app_id,
            "toWxid": to_wxid.strip(),
            "imgUrl": img_url,
        }
        resp = self._client.post(
            "/gewe/v2/api/message/postImage",
            headers=self._headers(),
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != 200:
            raise GeweError(f"GeWe postImage failed: {data.get('msg')}")
        return data

    def send_file(self, to_wxid: str, file_url: str, file_name: str) -> dict[str, Any]:
        """Send file attachment via publicly accessible URL."""
        payload = {
            "appId": self.app_id,
            "toWxid": to_wxid.strip(),
            "fileUrl": file_url,
            "fileName": file_name,
        }
        resp = self._client.post(
            "/gewe/v2/api/message/postFile",
            headers=self._headers(),
            json=payload,
            timeout=300.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != 200:
            raise GeweError(f"GeWe postFile failed: {data.get('msg')}")
        return data

    def download_inbound_media(
        self,
        xml: str,
        message_id: str,
        kind: str = "image",
    ) -> bytes:
        """Download incoming image or file using callback XML and message ID."""
        endpoint = (
            "/gewe/v2/api/message/downloadImage"
            if kind == "image"
            else "/gewe/v2/api/message/downloadFile"
        )
        payload = {
            "appId": self.app_id,
            "xml": xml,
        }
        if kind == "image":
            # GeWe's downloadImage contract uses type=2. downloadFile does not
            # use this field; the previous shared mapping inverted the image
            # value and caused the API to reject real image callback XML.
            payload["type"] = 2
        resp = self._client.post(
            endpoint,
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("ret") != 200 or not result.get("data", {}).get("fileUrl"):
            raise GeweError(f"GeWe media download URL request failed: {result.get('msg')}")

        file_url = result["data"]["fileUrl"]
        parsed = urlparse(file_url)
        if parsed.scheme not in {"http", "https"}:
            raise GeweError(f"Invalid media fileUrl returned: {file_url}")

        with httpx.Client(timeout=60.0, follow_redirects=True) as dl_client:
            media_resp = dl_client.get(file_url)
            media_resp.raise_for_status()
            return media_resp.content
