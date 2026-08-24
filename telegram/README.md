# Hannah Telegram

Telegram-Bot, der [Hannah Core](../core/README.md) über gRPC steuert. Unterstützt Text- und Sprachnachrichten, Auto-Status, Gerätesteuerung per Inline-Menü und Event-Push (Auto geparkt, Resident angekommen/abgegangen).

## Einrichtung

1. Bot-Token bei [@BotFather](https://t.me/BotFather) holen: `/newbot` schicken, Namen vergeben, Token notieren.
2. Bot installieren (siehe unten) und mit Token + Core-Adresse konfigurieren.
3. Eigenen Telegram-Account einmalig mit dem Hannah-Nutzerprofil verknüpfen — läuft über die [WebUI](https://github.com/NurPech/hannah-webui). `/start` beim Bot zeigt den Link an, solange der Account noch nicht verknüpft ist.

Danach gehen Text- und Sprachnachrichten direkt in dieselbe STT/NLU-Pipeline wie ein Satellit.

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/telegram/deploy/install.sh | sudo bash
```

Lädt den Bot vom Update-Server (Channel `telegram-stable`), System-User `hannah-telegram`. Startet erst, wenn `/etc/hannah-telegram/config.yaml` vorhanden ist.

## Konfiguration (`/etc/hannah-telegram/config.yaml`)

```yaml
telegram_token: "123456789:AAF..."     # von @BotFather
webui_url: "https://hannah.example.com" # für den Konto-Verknüpfungs-Link in Bot-Antworten

grpc:
  host: "127.0.0.1"    # gRPC-Adresse von Hannah Core
  port: 50051
```
