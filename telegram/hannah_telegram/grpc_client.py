"""Async gRPC client for Hannah."""
from __future__ import annotations

import logging
from typing import Optional

import grpc
import grpc.aio

from hannah_telegram.proto import hannah_pb2, hannah_pb2_grpc

log = logging.getLogger(__name__)


class HannahClient:
    """Thin async wrapper around the Hannah gRPC stub."""

    def __init__(self, host: str, port: int) -> None:
        self._address = f"{host}:{port}"
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[hannah_pb2_grpc.HannahServiceStub] = None

    async def connect(self) -> None:
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub = hannah_pb2_grpc.HannahServiceStub(self._channel)
        log.info("gRPC channel to Hannah at %s created", self._address)

    async def close(self) -> None:
        if self._channel:
            await self._channel.close()

    # ------------------------------------------------------------------
    # Text command
    # ------------------------------------------------------------------

    async def submit_text_full(self, text: str, chat_id: str) -> "hannah_pb2.SubmitTextResponse":
        """Send a text command to Hannah; return the full SubmitTextResponse (answer + intent_name)."""
        assert self._stub, "call connect() first"
        try:
            return await self._stub.SubmitText(
                hannah_pb2.SubmitTextRequest(
                    text=text,
                    source_service="telegram",
                    source_user_id=str(chat_id),
                )
            )
        except grpc.aio.AioRpcError as exc:
            log.error("SubmitText gRPC error: %s", exc)
            return hannah_pb2.SubmitTextResponse(
                answer="Hannah antwortet gerade nicht. Bitte versuche es später nochmal.",
                intent_name="",
            )

    async def submit_text(self, text: str, chat_id: str) -> str:
        """Convenience wrapper — returns only the answer string."""
        resp = await self.submit_text_full(text, chat_id)
        return resp.answer

    async def submit_voice(self, audio_ogg: bytes, chat_id: str) -> "hannah_pb2.SubmitVoiceResponse":
        """Send OGG/Opus audio to Hannah; returns transcript, answer, intent_name and TTS audio."""
        assert self._stub, "call connect() first"
        try:
            return await self._stub.SubmitVoice(
                hannah_pb2.SubmitVoiceRequest(
                    audio=audio_ogg,
                    source_service="telegram",
                    source_user_id=str(chat_id),
                )
            )
        except grpc.aio.AioRpcError as exc:
            log.error("SubmitVoice gRPC error: %s", exc)
            return hannah_pb2.SubmitVoiceResponse(
                transcript="",
                answer="Hannah antwortet gerade nicht. Bitte versuche es später nochmal.",
                intent_name="",
                audio_ogg=b"",
            )

    # ------------------------------------------------------------------
    # User registry
    # ------------------------------------------------------------------

    async def get_user_by_telegram(self, chat_id: str):
        """Look up a Hannah user by their linked Telegram chat_id.

        Returns (found: bool, user_or_None).
        """
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.GetUser(
                hannah_pb2.GetUserRequest(
                    linked_account=hannah_pb2.LinkedAccountLookup(
                        service="telegram",
                        account_id=str(chat_id),
                    )
                )
            )
            return resp.found, resp.user if resp.found else None
        except grpc.aio.AioRpcError as exc:
            log.error("GetUser gRPC error: %s", exc)
            return False, None

    async def get_all_telegram_chat_ids(self) -> list[str]:
        """Return all chat_ids of users with a linked Telegram account."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.GetUsers(
                hannah_pb2.GetUsersRequest(include_inactive=False)
            )
            ids = []
            for user in resp.users:
                if "telegram" in user.linked_accounts:
                    ids.append(user.linked_accounts["telegram"])
            return ids
        except grpc.aio.AioRpcError as exc:
            log.error("GetUsers gRPC error: %s", exc)
            return []

    async def get_system_message_telegram_ids(self) -> list[str]:
        """Return chat_ids of users with system_messages=True and a linked Telegram account."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.GetUsers(
                hannah_pb2.GetUsersRequest(include_inactive=False)
            )
            ids = []
            for user in resp.users:
                log.info(
                    "GetUsers: %s system_messages=%s telegram=%s",
                    user.roomie_id, user.system_messages,
                    user.linked_accounts.get("telegram", "-"),
                )
                if user.system_messages and "telegram" in user.linked_accounts:
                    ids.append(user.linked_accounts["telegram"])
            return ids
        except grpc.aio.AioRpcError as exc:
            log.error("GetUsers gRPC error: %s", exc)
            return []

    async def set_system_messages(self, roomie_id: str, enabled: bool) -> tuple[bool, str]:
        """Enable/disable system message notifications for a roomie."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.SetSystemMessages(
                hannah_pb2.SetSystemMessagesRequest(roomie_id=roomie_id, enabled=enabled)
            )
            return resp.ok, resp.message
        except grpc.aio.AioRpcError as exc:
            log.error("SetSystemMessages gRPC error: %s", exc)
            return False, str(exc)

    async def set_trust_level(self, roomie_id: str, level: int) -> tuple[bool, str]:
        """Set the trust level of a roomie. Returns (ok, message)."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.SetTrustLevel(
                hannah_pb2.SetTrustLevelRequest(roomie_id=roomie_id, level=level)
            )
            return resp.ok, resp.message
        except grpc.aio.AioRpcError as exc:
            log.error("SetTrustLevel gRPC error: %s", exc)
            return False, str(exc)

    async def link_account(self, roomie_id: str, chat_id: str) -> tuple[bool, str]:
        """Link a Telegram chat_id to a Hannah roomie. Returns (ok, message)."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.LinkAccount(
                hannah_pb2.LinkAccountRequest(
                    roomie_id=roomie_id,
                    service="telegram",
                    account_id=str(chat_id),
                )
            )
            return resp.ok, resp.message
        except grpc.aio.AioRpcError as exc:
            log.error("LinkAccount gRPC error: %s", exc)
            return False, str(exc)

    async def get_user_by_roomie(self, roomie_id: str):
        """Check if a roomie_id exists. Returns (found, user_or_None)."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.GetUser(
                hannah_pb2.GetUserRequest(roomie_id=roomie_id)
            )
            return resp.found, resp.user if resp.found else None
        except grpc.aio.AioRpcError as exc:
            log.error("GetUser(roomie) gRPC error: %s", exc)
            return False, None

    # ------------------------------------------------------------------
    # Device Control Menu
    # ------------------------------------------------------------------

    async def get_devices(self) -> "hannah_pb2.GetDevicesResponse":
        """Returns all rooms and devices with current state for building control menus."""
        assert self._stub, "call connect() first"
        try:
            return await self._stub.GetDevices(hannah_pb2.Empty())
        except grpc.aio.AioRpcError as exc:
            log.error("GetDevices gRPC error: %s", exc)
            return hannah_pb2.GetDevicesResponse()

    async def control_device(self, device_id: str, state: str, value: str) -> tuple[bool, str]:
        """Directly set a device state. Returns (ok, message)."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.ControlDevice(
                hannah_pb2.ControlDeviceRequest(
                    device_id=device_id,
                    state=state,
                    value=value,
                )
            )
            return resp.ok, resp.message
        except grpc.aio.AioRpcError as exc:
            log.error("ControlDevice gRPC error: %s", exc)
            return False, str(exc)

    # ------------------------------------------------------------------
    # Car state
    # ------------------------------------------------------------------

    async def get_car_state(self) -> tuple[bool, "hannah_pb2.CarStateProto | None"]:
        """Returns (available, CarStateProto_or_None)."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.GetCarState(hannah_pb2.Empty())
            return resp.available, resp.state if resp.available else None
        except grpc.aio.AioRpcError as exc:
            log.error("GetCarState gRPC error: %s", exc)
            return False, None

    async def get_all_car_states(self) -> list["hannah_pb2.CarStateProto"]:
        """Returns list of all available CarStateProtos."""
        assert self._stub, "call connect() first"
        try:
            resp = await self._stub.GetAllCarStates(hannah_pb2.Empty())
            return list(resp.states)
        except grpc.aio.AioRpcError as exc:
            log.error("GetAllCarStates gRPC error: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Event stream
    # ------------------------------------------------------------------

    async def subscribe_events(
        self,
        event_types: list[str],
        on_event,   # Callable[[hannah_pb2.HannahEvent], Awaitable[None]]
    ) -> None:
        """
        Streams events from Hannah. Reconnects automatically on error.
        Runs until the task is cancelled.

        on_event: async callback called for each received event.
        event_types: list of event type strings to filter, e.g. ["car.parked"].
                     Empty list = all events.
        """
        import asyncio
        assert self._stub, "call connect() first"
        while True:
            try:
                log.info("Subscribing to Hannah events (filter=%s)", event_types or "all")
                stream = self._stub.SubscribeEvents(
                    hannah_pb2.EventFilter(event_types=event_types)
                )
                async for event in stream:
                    try:
                        await on_event(event)
                    except Exception as exc:
                        log.error("on_event callback error: %s", exc)
            except grpc.aio.AioRpcError as exc:
                log.warning("Event stream disconnected: %s – reconnecting in 5s", exc)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                log.info("Event stream subscription cancelled.")
                return
