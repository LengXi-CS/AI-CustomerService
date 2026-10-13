import json
from unittest.mock import Mock
from xml.etree import ElementTree

import httpx

from fastgpt_assistant.adapters.wechat.client import WechatGeweClient


class _DownloadResponse:
    content = b"\xff\xd8\xffimage"

    def raise_for_status(self) -> None:
        return None


class _DownloadClient:
    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def get(self, _url: str) -> _DownloadResponse:
        return _DownloadResponse()


def test_download_image_uses_gewe_image_type_two(monkeypatch) -> None:
    client = WechatGeweClient(app_id="app")
    response = Mock()
    response.json.return_value = {
        "ret": 200,
        "data": {"fileUrl": "https://example.test/image.jpg"},
    }
    client._client.post = Mock(return_value=response)
    monkeypatch.setattr(
        "fastgpt_assistant.adapters.wechat.client.httpx.Client",
        _DownloadClient,
    )

    image = client.download_inbound_media(
        xml="<msg><img /></msg>",
        message_id="123",
        kind="image",
    )

    assert image.startswith(b"\xff\xd8\xff")
    payload = client._client.post.call_args.kwargs["json"]
    assert payload["type"] == 2
    assert payload["xml"] == "<msg><img /></msg>"
    assert "msgId" not in payload


def test_room_quote_preserves_conversation_member_and_long_message_id() -> None:
    """Preserve the room/member mapping used by the quote template under evaluation."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ret": 200, "msg": "操作成功"})

    with WechatGeweClient(base_url="https://example.test", app_id="test-app") as client:
        client._client.close()
        client._client = httpx.Client(
            base_url="https://example.test", transport=httpx.MockTransport(respond)
        )
        client.send_quoted_text(
            "123@chatroom",
            "回复 & <内容>",
            quoted_msg_id="3233375625312251145",
            quoted_sender_id="456@openim",
            quoted_chat_id="123@chatroom",
            quoted_content="原文 & <内容>",
        )

    assert len(requests) == 1
    assert requests[0].url.path == "/gewe/v2/api/message/postAppMsg"
    payload = json.loads(requests[0].content)
    xml = ElementTree.fromstring(payload["appmsg"])
    assert payload["toWxid"] == "123@chatroom"
    assert xml.findtext("type") == "57"
    assert xml.findtext("refermsg/type") == "1"
    assert xml.findtext("refermsg/fromusr") == "123@chatroom"
    assert xml.findtext("refermsg/chatusr") == "456@openim"
    assert xml.findtext("refermsg/svrid") == "3233375625312251145"
    assert xml.findtext("refermsg/content") == "原文 & <内容>"
    assert xml.findtext("title") == "回复 & <内容>"
