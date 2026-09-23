from types import SimpleNamespace

from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.engine import AssistantEngine
from fastgpt_assistant.orchestration.inbound import (
    extract_quote_payload,
    normalize_media_xml,
    strip_cdata,
)
from fastgpt_assistant.orchestration.visual_candidates import is_image_dependent_query


def test_strip_cdata() -> None:
    assert strip_cdata("<![CDATA[hello world]]>") == "hello world"
    assert strip_cdata("plain text") == "plain text"


def test_normalize_group_media_xml_removes_sender_prefix() -> None:
    assert normalize_media_xml(
        "wxid-customer:\n<msg><img aeskey=\"opaque\" /></msg>",
        is_group=True,
    ) == '<msg><img aeskey="opaque" /></msg>'


def test_extract_quote_payload_dash_format() -> None:
    raw = "「张三: 原消息」\n- - - - - - - - - - - - - - -\n这款检测报告麻烦提供下"
    clean, quoted, q_type, media_xml, q_id = extract_quote_payload(raw)
    assert clean == "这款检测报告麻烦提供下"
    assert quoted == "「张三: 原消息」"
    assert q_type == ""
    assert media_xml == ""


def test_extract_quote_payload_xml_appmsg() -> None:
    raw = (
        "<appmsg><title><![CDATA[这两款检测报告麻烦提供下]]></title>"
        "<refermsg><type>3</type><svrid>987654321</svrid>"
        "<displayname>客户小李</displayname><content>[图片]</content></refermsg></appmsg>"
    )
    clean, quoted, q_type, media_xml, q_id = extract_quote_payload(raw)
    assert clean == "这两款检测报告麻烦提供下"
    assert q_type == "3"
    assert q_id == "987654321"
    assert quoted == "客户小李: [图片]"
    assert media_xml == ""


def test_extract_quote_payload_xml_with_embedded_image_payload() -> None:
    raw = (
        "<appmsg><title>帮我查下这个</title><refermsg>"
        "<type>3</type><svrid>112233</svrid>"
        "<content><![CDATA[<msg><img aeskey=\"test\" /></msg>]]></content></refermsg></appmsg>"
    )
    clean, quoted, q_type, media_xml, q_id = extract_quote_payload(raw)
    assert clean == "帮我查下这个"
    assert q_type == "3"
    assert q_id == "112233"
    assert '<msg><img aeskey="test" /></msg>' in media_xml


def test_parse_gewe_callback_with_quote_message() -> None:
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(gewe_app_id="bot-wxid")
    engine.groups_config = WechatGroupsConfig.from_mapping({"global": {"enabled": False}})

    payload = {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": "msg-quote-1",
            "FromUserName": {"string": "user-wxid"},
            "ToUserName": {"string": "bot-wxid"},
            "MsgType": 49,
            "Content": {
                "string": (
                    "<appmsg><title><![CDATA[这两款检测报告麻烦提供下]]></title>"
                    "<refermsg><type>3</type><content>[图片]</content></refermsg></appmsg>"
                )
            },
        },
    }
    event = engine.parse_gewe_callback(payload)
    assert event is not None
    assert event.msg_type == 49
    assert event.text == "这两款检测报告麻烦提供下"
    assert event.quoted_type == "3"
    assert event.quoted_text == "[图片]"


def test_is_image_dependent_query_covers_pronouns() -> None:
    assert is_image_dependent_query("这款检测报告麻烦提供下") is True
    assert is_image_dependent_query("这两款检测报告麻烦提供下") is True
    assert is_image_dependent_query("该产品的第三方报告") is True
    assert is_image_dependent_query("今天天气怎么样") is False
