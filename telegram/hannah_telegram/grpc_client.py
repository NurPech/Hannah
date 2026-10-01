"""Async gRPC client for Hannah."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

import grpc
import grpc.aio

from hannah_grpc import client as hannah_client
from hannah_grpc import translate as hannah_translate
from hannah_proto.v1 import hannah_pb2 as v1_pb2
from hannah_proto.v2 import hannah_pb2

log = logging.getLogger(__name__)


class HannahClient:
    """Thin async wrapper around the Hannah gRPC stub.

    Works with hannah.v2 types only. Against a Core too old for hannah.v2, the calls are
    translated to hannah.v1 (hannah_grpc.client.VersionedStub.resolve_translated())."""

    def __init__(self, host: str, port: int) -> None:
        self._address = f"{host}:{port}"
        self._channel: Optional[grpc.aio.Channel] = None
        self._stubs: Optional[hannah_client.VersionedStub] = None
        # Open ChannelConnect stream (#334) and its redeem requests awaiting an answer
        self._channel_call = None
        self._channel_pb = hannah_pb2
        self._channel_write_lock = asyncio.Lock()
        self._pending_redeems: dict[str, asyncio.Future] = {}

    async def connect(self) -> None:
        # x-proto-version and x-compat-version (hannah-proto#10/hannah#217) on every
        # call, x-compat-version per service path (v2 or N−1).
        self._channel = grpc.aio.insecure_channel(
            self._address,
            interceptors=hannah_client.aio_interceptors(),
        )
        self._stubs = hannah_client.VersionedStub(self._channel)
        log.info("gRPC channel to Hannah at %s created", self._address)

    async def _get_stub(self):
        assert self._stubs, "call connect() first"
        return await self._stubs.resolve_translated()

    def _stream_metadata(self, method: str) -> tuple:
        # grpc.aio's stream interceptors don't reliably apply metadata mutations (unlike
        # unary-unary), so streams pass both headers explicitly. x-compat-version is
        # computed against the service whose path is in use.
        return hannah_client.stream_metadata(self._stubs.service, method)

    async def close(self) -> None:
        if self._channel:
            await self._channel.close()

    # ------------------------------------------------------------------
    # Text command
    # ------------------------------------------------------------------

    async def submit_text_full(self, text: str, chat_id: str) -> "hannah_pb2.SubmitTextResponse":
        """Send a text command to Hannah; return the full SubmitTextResponse (answer + intent_name)."""
        stub = await self._get_stub()
        try:
            return await stub.SubmitText(
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
        stub = await self._get_stub()
        try:
            return await stub.SubmitVoice(
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
        stub = await self._get_stub()
        try:
            resp = await stub.GetUser(
                hannah_pb2.GetUserRequest(
                    linked_account=hannah_pb2.LinkedAccountLookup(
                        provider="telegram",
                        external_id=str(chat_id),
                    )
                )
            )
            return resp.found, resp.user if resp.found else None
        except grpc.aio.AioRpcError as exc:
            log.error("GetUser gRPC error: %s", exc)
            return False, None

    async def get_all_telegram_chat_ids(self) -> list[str]:
        """Return all chat_ids of users with a linked Telegram account."""
        stub = await self._get_stub()
        try:
            resp = await stub.GetUsers(
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
        stub = await self._get_stub()
        try:
            resp = await stub.GetUsers(
                hannah_pb2.GetUsersRequest(include_inactive=False)
            )
            ids = []
            for user in resp.users:
                log.info(
                    "GetUsers: %s system_messages=%s telegram=%s",
                    user.user_name, user.system_messages,
                    user.linked_accounts.get("telegram", "-"),
                )
                if user.system_messages and "telegram" in user.linked_accounts:
                    ids.append(user.linked_accounts["telegram"])
            return ids
        except grpc.aio.AioRpcError as exc:
            log.error("GetUsers gRPC error: %s", exc)
            return []

    async def set_system_messages(self, user_id: int, enabled: bool) -> tuple[bool, str]:
        """Enable/disable system message notifications for the user identified by uuid (always
        unambiguous, unlike roomie_id — callers already resolved uuid via a linked-account lookup)."""
        stub = await self._get_stub()
        try:
            resp = await stub.SetSystemMessages(
                hannah_pb2.SetSystemMessagesRequest(user_id=user_id, enabled=enabled)
            )
            return resp.ok, resp.message
        except grpc.aio.AioRpcError as exc:
            log.error("SetSystemMessages gRPC error: %s", exc)
            return False, exc.details() or str(exc)

    async def set_trust_level(self, user_id: int, level: int) -> tuple[bool, str]:
        """Set the trust level of a roomie. Returns (ok, message)."""
        stub = await self._get_stub()
        try:
            req = hannah_pb2.SetTrustLevelRequest(user_id=user_id, level=level)
            resp = await stub.SetTrustLevel(req)
            return resp.ok, resp.message
        except grpc.aio.AioRpcError as exc:
            log.error("SetTrustLevel gRPC error: %s", exc)
            return False, exc.details() or str(exc)

    async def link_account(self, user_id: int, chat_id: str) -> tuple[bool, str]:
        """Link a Telegram chat_id to a Hannah roomie. resident_type (ROOMIE/GUEST/PET) disambiguates
        if roomie_id collides across types. Returns (ok, message)."""
        stub = await self._get_stub()
        try:
            req = hannah_pb2.LinkAccountRequest(user_id=user_id, service="telegram", account_id=str(chat_id))
            resp = await stub.LinkAccount(req)
            return resp.ok, resp.message
        except grpc.aio.AioRpcError as exc:
            log.error("LinkAccount gRPC error: %s", exc)
            return False, exc.details() or str(exc)

    async def get_user_by_username(self, user_name: str):
        """Check if a userbane exists. Returns (found, user_or_None, error_or_None)."""
        stub = await self._get_stub()
        try:
            req = hannah_pb2.GetUserRequest(user_name=user_name)
            resp = await stub.GetUser(req)
            return resp.found, (resp.user if resp.found else None), None
        except grpc.aio.AioRpcError as exc:
            log.error("GetUser(username) gRPC error: %s", exc)
            return False, None, exc.details() or str(exc)

    # ------------------------------------------------------------------
    # Device Control Menu
    # ------------------------------------------------------------------

    async def get_devices(self) -> "hannah_pb2.GetDevicesResponse":
        """Returns all rooms and devices with current state for building control menus."""
        stub = await self._get_stub()
        try:
            return await stub.GetDevices(hannah_pb2.Empty())
        except grpc.aio.AioRpcError as exc:
            log.error("GetDevices gRPC error: %s", exc)
            return hannah_pb2.GetDevicesResponse()

    async def control_device(
        self, device_id: str, slot_id: str, value: "hannah_pb2.SlotValue", chat_id: str,
    ) -> tuple[bool, str]:
        """Directly set a slot of a device. Returns (ok, message).

        chat_id identifies the requesting user, same as submit_text: Hannah looks the user
        up via linked accounts to check the slot's minimum trust level (#368).
        """
        stub = await self._get_stub()
        try:
            resp = await stub.ControlDevice(
                hannah_pb2.ControlDeviceRequest(
                    device_id=device_id,
                    slot_id=slot_id,
                    value=value,
                    source_service="telegram",
                    source_user_id=str(chat_id),
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
        stub = await self._get_stub()
        try:
            resp = await stub.GetCarState(hannah_pb2.Empty())
            return resp.available, resp.state if resp.available else None
        except grpc.aio.AioRpcError as exc:
            log.error("GetCarState gRPC error: %s", exc)
            return False, None

    async def get_all_car_states(self) -> list["hannah_pb2.CarStateProto"]:
        """Returns list of all available CarStateProtos."""
        stub = await self._get_stub()
        try:
            resp = await stub.GetAllCarStates(hannah_pb2.Empty())
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
        on_event,                  # Callable[[HannahEvent], Awaitable[None]]
        on_connected=None,         # Callable[[], Awaitable[None]] | None
        on_disconnected=None,      # Callable[[], Awaitable[None]] | None
    ) -> None:
        """
        Streams events from Hannah. Reconnects automatically on error.
        Runs until the task is cancelled.

        on_event:        async callback called for each received event.
        on_connected:    async callback when stream is (re-)established.
        on_disconnected: async callback when stream is lost (before reconnect).
        event_types:     list of event type strings to filter; empty = all.
        """
        first_connect = True
        while True:
            try:
                log.info("Subscribing to Hannah events (filter=%s)", event_types or "all")
                stub = await self._get_stub()
                stream = stub.SubscribeEvents(
                    hannah_pb2.EventFilter(event_types=event_types),
                    metadata=self._stream_metadata("SubscribeEvents"),
                )
                if on_connected:
                    try:
                        await on_connected(first_connect)
                    except Exception as exc:
                        log.error("on_connected callback error: %s", exc)
                first_connect = False
                async for event in stream:
                    try:
                        await on_event(event)
                    except Exception as exc:
                        log.error("on_event callback error: %s", exc)
            except grpc.aio.AioRpcError as exc:
                log.warning("Event stream disconnected: %s – reconnecting in 5s", exc)
                self._stubs.reset()  # Core may come back as a different version
                if on_disconnected:
                    try:
                        await on_disconnected()
                    except Exception as exc:
                        log.error("on_disconnected callback error: %s", exc)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                log.info("Event stream subscription cancelled.")
                return

    # ------------------------------------------------------------------
    # Channel registration + link tokens (#334)
    # ------------------------------------------------------------------

    async def channel_connect(self, register: "hannah_pb2.ChannelRegister") -> None:
        """
        Registers this adapter with Hannah and keeps the ChannelConnect stream open,
        so Hannah knows Telegram is running and can hand out deep links.
        Reconnects automatically. Runs until the task is cancelled.
        """
        while True:
            try:
                # A bidirectional stream isn't translated by the lib: the native stub of the
                # active generation, with that generation's messages.
                assert self._stubs, "call connect() first"
                stub = await self._stubs.resolve()
                previous = self._stubs.previous
                pb = v1_pb2 if previous else hannah_pb2
                call = stub.ChannelConnect(metadata=self._stream_metadata("ChannelConnect"))
                await call.write(pb.ChannelMessage(
                    register=hannah_translate.translate(register, "v1") if previous else register,
                ))
                self._channel_call = call
                self._channel_pb = pb
                async for cmd in call:
                    which = cmd.WhichOneof("command")
                    if which == "registered":
                        log.info("Registered with Hannah as channel %r", register.service)
                    elif which == "redeem_result":
                        fut = self._pending_redeems.pop(cmd.redeem_result.request_id, None)
                        if fut and not fut.done():
                            fut.set_result(cmd.redeem_result)
                log.warning("Channel stream closed by Hannah – reconnecting in 5s")
            except grpc.aio.AioRpcError as exc:
                log.warning("Channel stream disconnected: %s – reconnecting in 5s", exc)
            except asyncio.CancelledError:
                log.info("Channel stream cancelled.")
                return
            finally:
                self._channel_call = None
                for fut in self._pending_redeems.values():
                    if not fut.done():
                        fut.set_result(None)
                self._pending_redeems.clear()
            self._stubs.reset()  # Core may come back as a different version
            await asyncio.sleep(5)

    async def redeem_link_token(
        self, token: str, account_id: str, timeout: float = 10.0
    ) -> "Optional[hannah_pb2.RedeemLinkTokenResult]":
        """Redeems a link token over the ChannelConnect stream.
        Returns None if Hannah is not reachable or does not answer in time."""
        call = self._channel_call
        if call is None:
            return None
        request_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending_redeems[request_id] = fut
        try:
            async with self._channel_write_lock:
                pb = self._channel_pb
                await call.write(pb.ChannelMessage(redeem=pb.RedeemLinkToken(
                    request_id=request_id, token=token, account_id=account_id,
                )))
            return await asyncio.wait_for(fut, timeout)
        except (grpc.aio.AioRpcError, asyncio.TimeoutError) as exc:
            log.error("RedeemLinkToken failed: %s", exc)
            return None
        finally:
            self._pending_redeems.pop(request_id, None)
