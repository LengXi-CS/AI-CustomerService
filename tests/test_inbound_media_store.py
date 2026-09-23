from fastgpt_assistant.orchestration.inbound_media_store import InboundMediaStore


def test_inbound_media_store_rehydrates_by_original_message_id(tmp_path) -> None:
    store = InboundMediaStore(
        tmp_path / "inbound-media",
        tmp_path / "inbound-media.db",
    )
    stored = store.stage(
        media_id="inm_original-image",
        message_id="original-image-message",
        chat_id="room@chatroom",
        sender_id="room@chatroom",
        kind="image",
        mime_type="image/jpeg",
        content=b"image-bytes",
    )

    restarted = InboundMediaStore(
        tmp_path / "inbound-media",
        tmp_path / "inbound-media.db",
    )
    found = restarted.find_by_message(
        chat_id="room@chatroom",
        sender_id="room@chatroom",
        message_id="original-image-message",
    )

    assert found is not None
    assert found.media_id == stored.media_id
    assert restarted.read(found) == b"image-bytes"


def test_inbound_media_store_retains_only_message_window_entries(tmp_path) -> None:
    store = InboundMediaStore(tmp_path / "inbound-media", tmp_path / "inbound-media.db")
    for message_id in ("old-image", "current-image"):
        store.stage(
            media_id=f"inm_{message_id}",
            message_id=message_id,
            chat_id="room@chatroom",
            sender_id="room@chatroom",
            kind="image",
            mime_type="image/jpeg",
            content=b"image-bytes",
        )

    assert store.retain_message_ids(
        chat_id="room@chatroom", message_ids={"current-image"}
    ) == 1
    assert store.find_by_message(
        chat_id="room@chatroom", sender_id="room@chatroom", message_id="old-image"
    ) is None
    assert store.find_by_message(
        chat_id="room@chatroom", sender_id="room@chatroom", message_id="current-image"
    ) is not None
