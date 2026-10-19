"""Message type labels shared by archival and orchestration."""


def contact_card_label(msg_type: object) -> str:
    """Cards are context, never an implicit customer request."""
    return {"42": "[个人名片]", "66": "[企业微信名片]"}.get(str(msg_type), "")
