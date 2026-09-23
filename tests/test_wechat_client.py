from unittest.mock import Mock

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
