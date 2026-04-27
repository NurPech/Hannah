# -*- coding: utf-8 -*-
"""Telegram bot handlers.

Message flow
────────────
Text message   →  auth check  →  SubmitText(gRPC)   →  text reply
Voice message  →  auth check  →  SubmitVoice(gRPC)  →  voice reply (text fallback)
                 Hannah Core handles STT + NLU + TTS centrally.

CarQuery intent: Hannah returns intent_name="CarQuery" → bot fetches
full CarState via GetCarState and builds the rich Telegram message/caption.

Commands
────────
/start                      – welcome + linking instructions
/verknuepfen <roomie_id>    – link this Telegram account to a Hannah user
/auto                       – query current car status on demand
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import telegram
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    error,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

if TYPE_CHECKING:
    from hannah_telegram.grpc_client import HannahClient
    from hannah_telegram.proto import hannah_pb2

log = logging.getLogger(__name__)

# Mindest-TrustLevel für das Geräte-Steuerungsmenü
_MENU_TRUST_MIN = 7

# Callback-Präfix für alle Menü-Aktionen
_CB = "haus"


def _cb_rooms() -> str:
    return f"{_CB}:rooms"


def _cb_room(room_idx: int) -> str:
    """Callback-Daten für Raumauswahl. Max. ~10 Bytes."""
    return f"{_CB}:r:{room_idx}"


def _cb_device(room_idx: int, dev_idx: int) -> str:
    """Callback-Daten für Geräteauswahl. Max. ~12 Bytes."""
    return f"{_CB}:d:{room_idx}:{dev_idx}"


def _cb_ctrl(room_idx: int, dev_idx: int, state: str, value: str) -> str:
    """Callback-Daten für Steuerbefehl. Max. ~30 Bytes (weit unter Telegramm-Limit von 64)."""
    return f"{_CB}:c:{room_idx}:{dev_idx}:{state}:{value}"


_COMMANDS_DEFAULT = [
    telegram.BotCommand("start",       "Willkommensnachricht"),
    telegram.BotCommand("verknuepfen", "Konto mit Hannah verknüpfen"),
]
_COMMANDS_USER = [
    telegram.BotCommand("start",          "Willkommensnachricht"),
    telegram.BotCommand("auto",           "Fahrzeugstatus abfragen"),
    telegram.BotCommand("systemmessages", "System-Benachrichtigungen an/aus"),
]
_COMMANDS_USER_FULL = [
    telegram.BotCommand("start",          "Willkommensnachricht"),
    telegram.BotCommand("auto",           "Fahrzeugstatus abfragen"),
    telegram.BotCommand("haus",           "Haussteuerung öffnen"),
    telegram.BotCommand("systemmessages", "System-Benachrichtigungen an/aus"),
]
_COMMANDS_ADMIN = [
    telegram.BotCommand("start",          "Willkommensnachricht"),
    telegram.BotCommand("auto",           "Fahrzeugstatus abfragen"),
    telegram.BotCommand("haus",           "Haussteuerung öffnen"),
    telegram.BotCommand("systemmessages", "System-Benachrichtigungen an/aus"),
    telegram.BotCommand("trustlevel",     "Trust-Level setzen"),
]

_CATEGORY_ICONS = {
    "Licht":        "💡",
    "Stecker":      "🔌",
    "Temperaturen": "🌡️",
    "Fenster":      "🪟",
    "Helligkeit":   "☀️",
}

_WELCOME = (
    "Hallo! Ich bin Hannah, dein persönlicher Sprachassistent.\n\n"
    "Damit du mit mir chatten kannst, musst du dein Telegram-Konto "
    "einmal mit deinem Hannah-Profil verknüpfen:\n\n"
    "  /verknuepfen <deine-Roomie-ID>\n\n"
    "Deine Roomie-ID findest du in der Hannah-Konfiguration."
)

_UNKNOWN_USER = (
    "Ich kenne dich noch nicht. Bitte verknüpfe dein Konto zuerst:\n"
    "  /verknuepfen <deine-Roomie-ID>"
)

# Die {0} und {1} sind Platzhalter für Latitude und Longitude
_MAPS_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={0},{1}"


class HannahBot:
    def __init__(
        self,
        token: str,
        hannah: "HannahClient",
    ) -> None:
        self._token = token
        self._hannah = hannah
        self._app: Application | None = None

    # ------------------------------------------------------------------
    # Lifecycle

    def build_app(self) -> Application:
        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )
        app = self._app
        app.add_handler(CommandHandler("start",          self._cmd_start))
        app.add_handler(CommandHandler("verknuepfen",    self._cmd_link))
        app.add_handler(CommandHandler("auto",           self._cmd_auto))
        app.add_handler(CommandHandler("haus",           self._cmd_haus))
        app.add_handler(CommandHandler("trustlevel",     self._cmd_trustlevel))
        app.add_handler(CommandHandler("systemmessages", self._cmd_systemmessages))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        app.add_handler(MessageHandler(filters.VOICE, self._on_voice))
        app.add_handler(CallbackQueryHandler(self._on_callback))

        return app

    async def init_commands(self) -> None:
        """
        Setzt Default-Commands (für unbekannte User) und initialisiert die
        Chat-spezifischen Commands für alle bereits verknüpften User.
        Sollte einmalig nach app.start() aufgerufen werden.
        """
        if self._app is None:
            return
        # Default für alle: nur start + verknuepfen
        await self._app.bot.set_my_commands(
            _COMMANDS_DEFAULT,
            scope=telegram.BotCommandScopeDefault(),
        )
        # Chat-spezifisch für bekannte User
        chat_ids = await self._hannah.get_all_telegram_chat_ids()
        for cid in chat_ids:
            _, user = await self._hannah.get_user_by_telegram(cid)
            if user:
                await self._set_commands_for_chat(cid, user.trust_level)

    async def _set_commands_for_chat(self, chat_id: str, trust_level: int) -> None:
        """Setzt die passenden Commands für einen einzelnen Chat."""
        if self._app is None:
            return
        if trust_level >= 10:
            cmds = _COMMANDS_ADMIN
        elif trust_level >= _MENU_TRUST_MIN:
            cmds = _COMMANDS_USER_FULL
        else:
            cmds = _COMMANDS_USER
        try:
            await self._app.bot.set_my_commands(
                cmds,
                scope=telegram.BotCommandScopeChat(chat_id=int(chat_id)),
            )
        except Exception as exc:
            log.warning("set_my_commands für chat_id=%s fehlgeschlagen: %s", chat_id, exc)

    # ------------------------------------------------------------------
    # Proactive push

    async def send_car_parked_to_all(self, car_state: "hannah_pb2.CarStateProto") -> None:
        """Send a car-parked notification to the car owner's linked Telegram accounts.

        If owner_roomie is set in the car state, only that roomie's accounts are notified.
        Falls back to all linked users if no owner is configured.
        """
        if self._app is None:
            return

        owner_roomie = car_state.owner_roomie
        if owner_roomie:
            found, user = await self._hannah.get_user_by_roomie(owner_roomie)
            if not found or user is None:
                log.warning("Car parked: owner roomie %r not found – nobody to notify", owner_roomie)
                return
            telegram_id = user.linked_accounts.get("telegram")
            chat_ids = [telegram_id] if telegram_id and self._is_private_chat(telegram_id) else []
            if not chat_ids:
                log.warning("Car parked: owner %r has no linked Telegram account", owner_roomie)
                return
        else:
            chat_ids = [c for c in await self._hannah.get_all_telegram_chat_ids() if self._is_private_chat(c)]
            if not chat_ids:
                log.warning("Car parked but no Telegram users linked – nobody to notify")
                return

        text = _car_proto_to_message(car_state)
        for cid in chat_ids:
            try:
                await self._app.bot.send_message(chat_id=cid, text=text)
            except Exception as exc:
                log.error("Failed to notify chat_id=%s: %s", cid, exc)

    async def send_system_notification(self, text: str) -> None:
        """Send a system notification to all users with system_messages=True."""
        if self._app is None:
            return
        chat_ids = [c for c in await self._hannah.get_system_message_telegram_ids() if self._is_private_chat(c)]
        log.info("system.notification: %d Empfänger mit system_messages=True", len(chat_ids))
        if not chat_ids:
            return
        for cid in chat_ids:
            try:
                await self._app.bot.send_message(chat_id=cid, text=f"ℹ️ {text}")
                log.info("system.notification: gesendet an chat_id=%s", cid)
            except Exception as exc:
                log.error("system.notification: Senden an chat_id=%s fehlgeschlagen: %s", cid, exc)

    async def send_status_update(self, text: str) -> None:
        """Send a connection status message to all users with system_messages=True."""
        if self._app is None:
            return
        chat_ids = [c for c in await self._hannah.get_system_message_telegram_ids() if self._is_private_chat(c)]
        if not chat_ids:
            return
        for cid in chat_ids:
            try:
                await self._app.bot.send_message(chat_id=cid, text=text)
            except Exception as exc:
                log.error("status update: Senden an chat_id=%s fehlgeschlagen: %s", cid, exc)

    # ------------------------------------------------------------------
    # Helpers

    @staticmethod
    def _is_private_chat(chat_id: str) -> bool:
        """Persönliche Chats haben positive IDs, Gruppen negative."""
        try:
            return int(chat_id) > 0
        except (ValueError, TypeError):
            return False

    # ------------------------------------------------------------------
    # Auth helpers

    async def _is_known_user(self, chat_id: str) -> bool:
        found, _ = await self._hannah.get_user_by_telegram(chat_id)
        return found

    async def _get_user(self, chat_id: str):
        """Gibt das User-Objekt zurück oder None wenn unbekannt."""
        found, user = await self._hannah.get_user_by_telegram(chat_id)
        return user if found else None

    async def _has_trust(self, chat_id: str, min_level: int) -> tuple[bool, object]:
        """Gibt (ok, user_or_None) zurück. ok=True wenn trust_level >= min_level."""
        user = await self._get_user(chat_id)
        if user is None:
            return False, None
        return user.trust_level >= min_level, user

    # ------------------------------------------------------------------
    # Command handlers

    async def _cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        if await self._is_known_user(chat_id):
            await update.message.reply_text("Hallo! Ich bin Hannah. Was kann ich für dich tun?")
        else:
            await update.message.reply_text(_WELCOME)

    async def _cmd_link(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        if not self._is_private_chat(chat_id):
            await update.message.reply_text(
                "Verknüpfungen bitte im privaten Chat mit mir vornehmen, nicht in Gruppen."
            )
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Bitte gib deine Roomie-ID an:\n  /verknuepfen <roomie-id>")
            return

        roomie_id = args[0].strip()
        found, user = await self._hannah.get_user_by_roomie(roomie_id)
        if not found:
            await update.message.reply_text(f"Roomie-ID '{roomie_id}' nicht gefunden.")
            return

        ok, msg = await self._hannah.link_account(roomie_id, chat_id)
        if ok:
            name = user.display_name if user else roomie_id
            await update.message.reply_text(f"Verknüpfung erfolgreich! Hallo, {name}.")
            # Commands für diesen Chat anpassen (verknuepfen ausblenden)
            await self._set_commands_for_chat(chat_id, user.trust_level if user else 5)
        else:
            await update.message.reply_text(f"Verknüpfung fehlgeschlagen: {msg}")

    async def _cmd_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        if not await self._is_known_user(chat_id):
            await update.message.reply_text(_UNKNOWN_USER)
            return

        debug = bool(ctx.args and ctx.args[0].lower() == "debug")
        states = await self._hannah.get_all_car_states()
        if not states:
            await update.message.reply_text("Ich habe noch keine Auto-Daten empfangen.")
            return

        for state in states:
            if debug:
                home_prefix = state.home_address[:20] if state.home_address else "(nicht gesetzt)"
                addr_match = bool(state.home_address and state.address and state.address.startswith(state.home_address[:20]))
                ts = ""
                if state.position_date:
                    from datetime import datetime
                    ts_val = state.position_date / 1000 if state.position_date > 1e10 else state.position_date
                    ts = datetime.fromtimestamp(ts_val).strftime("%d.%m.%y %H:%M:%S")
                debug_text = (
                    f"🔍 Auto-Debug\n"
                    f"is_moving: {state.is_moving}\n"
                    f"lat/lon: {state.latitude} / {state.longitude}\n"
                    f"address: {state.address or '(leer)'}\n"
                    f"home_address: {state.home_address or '(nicht gesetzt)'}\n"
                    f"home_prefix ([:20]): {home_prefix}\n"
                    f"→ startswith match: {addr_match}\n"
                    f"is_car_locked: {state.is_car_locked}\n"
                    f"door_lock_status: {state.door_lock_status or '(leer)'}\n"
                    f"position_date: {ts or '(leer)'}"
                )
                await update.message.reply_text(debug_text)
                continue

            answer = _car_proto_to_message(state)
            buttons = [[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="refresh:car"),
                InlineKeyboardButton("📍 Karte", url=_MAPS_URL_TEMPLATE.format(state.latitude, state.longitude))
            ]]
            if not state.is_car_locked or state.door_lock_status == "unlocked":
                buttons.append([InlineKeyboardButton("🔐 Auto jetzt verriegeln", callback_data="lock_car")])
            await update.message.reply_text(answer, reply_markup=InlineKeyboardMarkup(buttons))

    async def _cmd_trustlevel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        ok, user = await self._has_trust(chat_id, 10)
        if not ok:
            if user is None:
                await update.message.reply_text(_UNKNOWN_USER)
            else:
                await update.message.reply_text("Nur Admins (Trust-Level 10) können Trust-Level setzen.")
            return

        args = ctx.args or []
        if len(args) != 2:
            await update.message.reply_text("Verwendung: /trustlevel <roomie-id> <0-10>")
            return

        roomie_id = args[0].strip()
        try:
            level = int(args[1])
            if not 0 <= level <= 10:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Trust-Level muss eine Zahl zwischen 0 und 10 sein.")
            return

        set_ok, msg = await self._hannah.set_trust_level(roomie_id, level)
        if not set_ok:
            await update.message.reply_text(f"Fehler: {msg}")
            return

        await update.message.reply_text(f"Trust-Level von '{roomie_id}' auf {level} gesetzt.")

        # Commands für diesen User sofort aktualisieren falls er Telegram verknüpft hat
        _, target_user = await self._hannah.get_user_by_roomie(roomie_id)
        if target_user:
            tg_id = target_user.linked_accounts.get("telegram")
            if tg_id:
                await self._set_commands_for_chat(tg_id, level)
                log.info("Commands für %s (chat_id=%s) auf Trust=%d aktualisiert", roomie_id, tg_id, level)

    async def _cmd_systemmessages(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        ok, user = await self._has_trust(chat_id, _MENU_TRUST_MIN)
        if not ok:
            if user is None:
                await update.message.reply_text(_UNKNOWN_USER)
            else:
                await update.message.reply_text("System-Benachrichtigungen erfordern mindestens Trust-Level 7.")
            return

        args = ctx.args or []
        if len(args) != 1 or args[0].lower() not in ("an", "aus"):
            current = "an" if user.system_messages else "aus"
            await update.message.reply_text(
                f"System-Benachrichtigungen sind aktuell: {current}\n"
                "Verwendung: /systemmessages an  oder  /systemmessages aus"
            )
            return

        enabled = args[0].lower() == "an"
        set_ok, msg = await self._hannah.set_system_messages(user.roomie_id, enabled)
        if not set_ok:
            await update.message.reply_text(f"Fehler: {msg}")
            return

        status = "aktiviert ✅" if enabled else "deaktiviert ❌"
        await update.message.reply_text(f"System-Benachrichtigungen {status}.")

    async def _cmd_haus(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        ok, user = await self._has_trust(chat_id, _MENU_TRUST_MIN)
        if not ok:
            if user is None:
                await update.message.reply_text(_UNKNOWN_USER)
            else:
                await update.message.reply_text(
                    f"Du benötigst Trust-Level {_MENU_TRUST_MIN} für die Haussteuerung."
                    f" Dein aktuelles Level: {user.trust_level}."
                )
            return
        await update.message.reply_text(
            "Haussteuerung — wähle einen Raum:",
            reply_markup=await self._room_keyboard(),
        )

    # ------------------------------------------------------------------
    # Menü-Keyboards

    async def _room_keyboard(self) -> InlineKeyboardMarkup:
        resp = await self._hannah.get_devices()
        buttons = []
        for idx, room in enumerate(resp.rooms):
            buttons.append([InlineKeyboardButton(f"🏠 {room.name}", callback_data=_cb_room(idx))])
        buttons.append([InlineKeyboardButton("✖ Schließen", callback_data=f"{_CB}:close")])
        return InlineKeyboardMarkup(buttons)

    def _device_keyboard(self, room: object, room_idx: int) -> InlineKeyboardMarkup:
        buttons = []
        for dev_idx, dev in enumerate(room.devices):
            icon = _CATEGORY_ICONS.get(dev.category, "⚙️")
            on_val = dev.current.get("on", "")
            state_dot = "🟢" if on_val == "True" else ("🔴" if on_val == "False" else "⚫")
            buttons.append([InlineKeyboardButton(
                f"{state_dot} {icon} {dev.name}",
                callback_data=_cb_device(room_idx, dev_idx),
            )])
        buttons.append([InlineKeyboardButton("← Räume", callback_data=_cb_rooms())])
        return InlineKeyboardMarkup(buttons)

    def _control_keyboard(self, dev: object, room_idx: int, dev_idx: int) -> InlineKeyboardMarkup:
        """Erstellt die Steuerungs-Buttons für ein einzelnes Gerät."""
        buttons = []
        states = list(dev.states)

        if "on" in states:
            on_val = dev.current.get("on", "")
            is_on = (on_val == "True")
            row = []
            if not is_on:
                row.append(InlineKeyboardButton("✅ Einschalten", callback_data=_cb_ctrl(room_idx, dev_idx, "on", "true")))
            else:
                row.append(InlineKeyboardButton("⏹ Ausschalten", callback_data=_cb_ctrl(room_idx, dev_idx, "on", "false")))
            buttons.append(row)

        if "level" in states:
            level_val = dev.current.get("level", "")
            try:
                cur = int(float(level_val))
            except (ValueError, TypeError):
                cur = -1
            level_buttons = []
            for pct in (25, 50, 75, 100):
                mark = "·" if cur != pct else "▶"
                level_buttons.append(InlineKeyboardButton(
                    f"{mark}{pct}%", callback_data=_cb_ctrl(room_idx, dev_idx, "level", str(pct))
                ))
            buttons.append(level_buttons)

        if "color" in states:
            color_buttons = [
                InlineKeyboardButton("🔴", callback_data=_cb_ctrl(room_idx, dev_idx, "color", "#FF0000")),
                InlineKeyboardButton("🟢", callback_data=_cb_ctrl(room_idx, dev_idx, "color", "#00FF00")),
                InlineKeyboardButton("🔵", callback_data=_cb_ctrl(room_idx, dev_idx, "color", "#0000FF")),
                InlineKeyboardButton("🟡", callback_data=_cb_ctrl(room_idx, dev_idx, "color", "#FFFF00")),
                InlineKeyboardButton("⚪", callback_data=_cb_ctrl(room_idx, dev_idx, "color", "#FFFFFF")),
            ]
            buttons.append(color_buttons)

        buttons.append([
            InlineKeyboardButton("🔄 Aktualisieren", callback_data=_cb_device(room_idx, dev_idx)),
            InlineKeyboardButton("← Raum", callback_data=_cb_room(room_idx)),
        ])
        return InlineKeyboardMarkup(buttons)

    def _device_status_text(self, dev: object) -> str:
        parts = [f"*{dev.name}* ({dev.category})"]
        cur = dev.current
        if "on" in cur:
            parts.append("Status: " + ("🟢 an" if cur["on"] == "True" else "🔴 aus"))
        if "level" in cur:
            try:
                parts.append(f"Helligkeit: {int(float(cur['level']))}%")
            except (ValueError, TypeError):
                pass
        if "color" in cur:
            parts.append(f"Farbe: {cur['color']}")
        if "current" in cur:
            parts.append(f"Temperatur: {cur['current']}°")
        if "illuminance" in cur:
            parts.append(f"Helligkeit: {cur['illuminance']} lx")
        if "open" in cur:
            parts.append("Fenster: " + ("offen" if cur["open"] == "True" else "geschlossen"))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Message handlers

    async def _on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        if not await self._is_known_user(chat_id):
            await update.message.reply_text(_UNKNOWN_USER)
            return

        text = update.message.text.strip()
        await update.message.chat.send_action(ChatAction.TYPING)

        resp = await self._hannah.submit_text_full(text, chat_id)
        answer = resp.answer
        reply_markup = None

        if resp.intent_name == "CarQuery":
            states = await self._hannah.get_all_car_states()
            if states:
                state = states[0]
                answer = _car_proto_to_message(state)
                buttons = [[
                    InlineKeyboardButton("🔄 Aktualisieren", callback_data="refresh:car"),
                    InlineKeyboardButton("📍 Karte", url=_MAPS_URL_TEMPLATE.format(state.latitude, state.longitude))
                ]]
                if not state.is_car_locked or state.door_lock_status == "unlocked":
                    buttons.append([InlineKeyboardButton("🔐 Auto jetzt verriegeln", callback_data="lock_car")])
                reply_markup = InlineKeyboardMarkup(buttons)

        elif resp.intent_name == "WeatherQuery":
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🌡️ Wetter aktualisieren", callback_data="refresh:weather")]])

        elif resp.intent_name == "Query":
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Werte aktualisieren", callback_data=f"retry:{text[:50]}")]])

        await update.message.reply_text(answer, reply_markup=reply_markup)

    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        if not await self._is_known_user(chat_id):
            await update.message.reply_text(_UNKNOWN_USER)
            return

        await update.message.chat.send_action(ChatAction.RECORD_VOICE)
        file = await ctx.bot.get_file(update.message.voice.file_id)
        ogg_bytes = bytes(await file.download_as_bytearray())
        resp = await self._hannah.submit_voice(ogg_bytes, chat_id)

        if not resp.transcript:
            await update.message.reply_text("Ich konnte dich leider nicht verstehen.")
            return

        await update.message.chat.send_action(ChatAction.TYPING)
        caption = resp.answer
        if resp.intent_name == "CarQuery":
            states = await self._hannah.get_all_car_states()
            if states:
                caption = _car_proto_to_message(states[0])

        if resp.audio_ogg:
            await update.message.reply_voice(voice=io.BytesIO(resp.audio_ogg), caption=caption)
        else:
            await update.message.reply_text(caption)

    async def _on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        data = query.data
        chat_id = str(update.effective_chat.id)

        # ------------------------------------------------------------------
        # Haussteuerungsmenü — verwaltet query.answer() selbst
        if data.startswith(f"{_CB}:"):
            await self._on_haus_callback(query, chat_id, data)
            return

        # Alle anderen Callbacks: Spinner sofort schließen
        await query.answer()

        if data in ["refresh:car", "update_car"]:
            states = await self._hannah.get_all_car_states()
            if states:
                await self._safe_edit_text(query, _car_proto_to_message(states[0]))
        elif data == "refresh:weather":
            resp = await self._hannah.submit_text_full("Wie ist das Wetter?", chat_id)
            await self._safe_edit_text(query, resp.answer)
        elif data.startswith("retry:"):
            orig = data.split(":", 1)[1]
            resp = await self._hannah.submit_text_full(orig, chat_id)
            await self._safe_edit_text(query, resp.answer)

    async def _on_haus_callback(self, query, chat_id: str, data: str) -> None:
        """Verarbeitet alle haus:* Callback-Aktionen.

        Indices statt Klartexte in callback_data, damit das Telegram-Limit von
        64 Bytes auch bei langen ioBroker-Device-IDs nie überschritten wird.
        Format:
          haus:close
          haus:rooms
          haus:r:{room_idx}
          haus:d:{room_idx}:{dev_idx}
          haus:c:{room_idx}:{dev_idx}:{state}:{value}
        """
        ok, _user = await self._has_trust(chat_id, _MENU_TRUST_MIN)
        if not ok:
            await query.answer("Keine Berechtigung.", show_alert=True)
            return

        parts = data.split(":")

        # haus:close
        if parts[1] == "close":
            await query.answer()
            try:
                await query.delete_message()
            except Exception:
                await query.edit_message_text("Menü geschlossen.")
            return

        # haus:rooms — Raumliste
        if parts[1] == "rooms":
            await query.answer()
            await self._safe_edit_text_markup(
                query,
                "Haussteuerung — wähle einen Raum:",
                await self._room_keyboard(),
            )
            return

        # haus:r:{room_idx} — Geräteliste eines Raums
        if parts[1] == "r" and len(parts) >= 3:
            try:
                room_idx = int(parts[2])
            except ValueError:
                await query.answer("Ungültige Raumauswahl.", show_alert=True)
                return
            await query.answer()
            resp = await self._hannah.get_devices()
            if room_idx < 0 or room_idx >= len(resp.rooms):
                return
            room = resp.rooms[room_idx]
            await self._safe_edit_text_markup(
                query,
                f"🏠 {room.name} — Geräte:",
                self._device_keyboard(room, room_idx),
            )
            return

        # haus:d:{room_idx}:{dev_idx} — Steuerungsansicht eines Geräts
        if parts[1] == "d" and len(parts) >= 4:
            try:
                room_idx = int(parts[2])
                dev_idx = int(parts[3])
            except ValueError:
                await query.answer("Ungültige Geräteauswahl.", show_alert=True)
                return
            await query.answer()
            resp = await self._hannah.get_devices()
            if room_idx < 0 or room_idx >= len(resp.rooms):
                return
            room = resp.rooms[room_idx]
            if dev_idx < 0 or dev_idx >= len(room.devices):
                return
            dev = room.devices[dev_idx]
            await self._safe_edit_text_markup(
                query,
                self._device_status_text(dev),
                self._control_keyboard(dev, room_idx, dev_idx),
            )
            return

        # haus:c:{room_idx}:{dev_idx}:{state}:{value} — Steuerbefehl ausführen
        if parts[1] == "c" and len(parts) >= 6:
            try:
                room_idx = int(parts[2])
                dev_idx = int(parts[3])
            except ValueError:
                await query.answer("Ungültige Auswahl.", show_alert=True)
                return
            state = parts[4]
            value = ":".join(parts[5:])  # Farbwerte wie #FF0000 enthalten kein ':'

            # Device-ID für gRPC-Aufruf via Index nachschlagen
            resp = await self._hannah.get_devices()
            if room_idx < 0 or room_idx >= len(resp.rooms):
                await query.answer("Raum nicht mehr vorhanden.", show_alert=True)
                return
            room = resp.rooms[room_idx]
            if dev_idx < 0 or dev_idx >= len(room.devices):
                await query.answer("Gerät nicht mehr vorhanden.", show_alert=True)
                return
            dev = room.devices[dev_idx]

            ok_ctrl, msg = await self._hannah.control_device(dev.id, state, value)
            if not ok_ctrl:
                await query.answer(f"Fehler: {msg}", show_alert=True)
                return
            await query.answer("✅ Befehl gesendet.")

            # Gerät-Ansicht mit aktualisiertem Status neu laden
            resp2 = await self._hannah.get_devices()
            if room_idx < len(resp2.rooms):
                room2 = resp2.rooms[room_idx]
                if dev_idx < len(room2.devices):
                    dev2 = room2.devices[dev_idx]
                    await self._safe_edit_text_markup(
                        query,
                        self._device_status_text(dev2),
                        self._control_keyboard(dev2, room_idx, dev_idx),
                    )
            return

    async def _safe_edit_text_markup(self, query, text: str, markup: InlineKeyboardMarkup):
        try:
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="Markdown")
        except error.BadRequest as e:
            if "Message is not modified" not in str(e):
                log.error("Edit failed: %s", e)

    async def _safe_edit_text(self, query, new_text):
        try:
            await query.edit_message_text(text=new_text, reply_markup=query.message.reply_markup)
        except error.BadRequest as e:
            if "Message is not modified" not in str(e):
                log.error("Edit failed: %s", e)

# ------------------------------------------------------------------
# Car state proto → Telegram message

_DOOR_LABELS = {"bonnet": "Motorraum", "frontLeft": "Fahrerseite", "frontRight": "Beifahrerseite", "rearLeft": "Tür hinten links", "rearRight": "Tür hinten rechts", "trunk": "Kofferraum"}
_WINDOW_LABELS = {"frontLeft": "Fenster Fahrerseite", "frontRight": "Fenster Beifahrerseite", "rearLeft": "Fenster hinten links", "rearRight": "Fenster hinten rechts"}

def _car_proto_to_message(state: "hannah_pb2.CarStateProto") -> str:
    label = state.display_name or state.plate or "Auto"
    home_address = state.home_address or ""
    parts = []
    if state.is_moving:
        parts.append(f"🚗 {label} fährt, letzte bekannte Adresse:\n{state.address}")
    elif home_address and state.address and state.address.startswith(home_address[:20]):
        parts.append(f"🏠 {label} steht zu Hause.\n{state.address}")
    elif state.address:
        parts.append(f"📍 {label} steht an:\n{state.address}")

    if state.latitude and state.longitude:
        parts.append(f"🗺 {_MAPS_URL_TEMPLATE.format(state.latitude, state.longitude)}")

    problems = []
    if state.door_lock_status == "unlocked" or not state.is_car_locked:
        problems.append("Auto ist nicht abgeschlossen")
    for name, closed in state.doors.items():
        if not closed: problems.append(_DOOR_LABELS.get(name, name) + " geöffnet")
    for name, closed in state.windows.items():
        if not closed: problems.append(_WINDOW_LABELS.get(name, name) + " geöffnet")

    if problems:
        parts.append("⚠️ Sicherheitsproblem:\n" + "\n".join(f"- {p}" for p in problems))
    else:
        parts.append("✅ Alle Fenster geschlossen, Auto abgeschlossen.")

    if state.odometer: parts.append(f"🔢 Kilometerstand: {state.odometer} km")
    if state.total_range: parts.append(f"⛽ Restreichweite: {state.total_range} km")
    if state.position_date:
        ts = state.position_date / 1000 if state.position_date > 1e10 else state.position_date
        parts.append(f"🕐 Letztes Update: {datetime.fromtimestamp(ts).strftime('%d.%m.%y %H:%M:%S')}")
    
    parts.append(f"⏱ Nachricht generiert um: {datetime.now().strftime('%H:%M:%S')}")
    return "\n\n".join(parts) if parts else "Keine Auto-Daten verfügbar."