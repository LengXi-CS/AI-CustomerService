"""Per-message metadata and the WeChat/WeCom text quote envelope."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_@.-]{1,128}$")


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("string", "")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class TextQuote:
    message_id: str
    chat_id: str
    sender_id: str
    content: str
    msg_type: int = 1
    sender_name: str = ""
    created_at: int = 0
    msg_source: str = ""
    str_id: str = ""
    imunion_sender_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Any) -> TextQuote | None:
        if not isinstance(value, dict):
            return None
        fields = {}
        for name in cls.__dataclass_fields__:
            item = value.get(name)
            if name in {"created_at", "msg_type"}:
                if isinstance(item, int) and not isinstance(item, bool):
                    fields[name] = item
            elif isinstance(item, str):
                fields[name] = item
        if any(name not in fields for name in ("message_id", "chat_id", "sender_id", "content")):
            return None
        return cls(**fields)

    @classmethod
    def from_callback(
        cls, *, data: dict, message_id: str, chat_id: str, sender_id: str,
        content: str, msg_type: int, sender_name: str = "",
    ) -> TextQuote:
        source = _text(data.get("MsgSource"))
        str_id = ""
        try:
            source_root = ET.fromstring(source)
            str_id = source_root.findtext("strid", "")
        except ET.ParseError:
            pass
        created_at = data.get("CreateTime")
        if isinstance(created_at, dict):
            created_at = created_at.get("string", "")
        created_at = int(created_at) if str(created_at).isdigit() else 0
        imunion = _text(data.get("FromUsernameToImunion") or data.get("fromusername_to_imunion"))
        # The captured native template uses the @openim member identifier here.
        if not imunion and sender_id.endswith("@openim"):
            imunion = sender_id
        return cls(
            message_id=message_id, chat_id=chat_id, sender_id=sender_id, content=content,
            msg_type=msg_type, sender_name=sender_name, created_at=created_at,
            msg_source=source, str_id=str_id, imunion_sender_id=imunion,
        )

    @property
    def native_ready(self) -> bool:
        if not (
            self.msg_type == 1 and self.content.strip() and self.message_id.isascii()
            and self.message_id.isdigit() and self.created_at > 0 and self.str_id
            and _IDENTIFIER.fullmatch(self.chat_id) and _IDENTIFIER.fullmatch(self.sender_id)
        ):
            return False
        try:
            source = ET.fromstring(self.msg_source)
        except ET.ParseError:
            return False
        return (
            source.tag == "msgsource" and source.findtext("strid") == self.str_id
            and (not self.sender_id.endswith("@openim") or bool(self.imunion_sender_id))
        )

    def appmsg_xml(self, reply_text: str) -> str:
        """Keep all nodes from the successful native template; bind this message only."""
        root = ET.Element("appmsg", {"appid": "", "sdkver": "0"})
        fields = {
            "title": reply_text, "des": "", "action": "", "type": "57", "showtype": "0",
            "soundtype": "0", "mediatagname": "", "messageext": "", "messageaction": "",
            "content": "", "contentattr": "0", "url": "", "lowurl": "", "dataurl": "",
            "lowdataurl": "", "songalbumurl": "", "songlyric": "", "template_id": "",
        }
        for name, value in fields.items():
            ET.SubElement(root, name).text = value
        attachment = ET.SubElement(root, "appattach")
        for name in ("totallen", "attachid", "emoticonmd5", "fileext", "aeskey"):
            ET.SubElement(attachment, name).text = "0" if name == "totallen" else ""
        for name in (
            "extinfo", "sourceusername", "sourcedisplayname", "thumburl", "md5", "statextstr"
        ):
            ET.SubElement(root, name)
        refer = ET.SubElement(root, "refermsg")
        for name, value in {
            "content": self.content, "createtime": str(self.created_at),
            "displayname": self.sender_name, "fromusr": self.chat_id, "svrid": self.message_id,
            "msgsource": self.msg_source, "strid": self.str_id, "type": str(self.msg_type),
            "chatusr": self.sender_id, "fromusername_to_imunion": self.imunion_sender_id,
        }.items():
            ET.SubElement(refer, name).text = value
        return ET.tostring(root, encoding="unicode", short_empty_elements=True)

    def fallback_text(self, reply_text: str) -> str:
        summary = " ".join(self.content.split())
        if summary.startswith("<"):
            summary = "附件消息"
        summary = summary[:80] + ("…" if len(summary) > 80 else "")
        if not summary or summary in reply_text:
            return reply_text
        return f"关于您提到的“{summary}”：\n{reply_text}"
