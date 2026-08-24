# Hannah Proxy

Optionaler Go-Dienst zwischen Satelliten und [Hannah Core](../core/README.md). Bindet UDP-Satelliten per gRPC an Core an, reicht Audio durch und spielt Core's TTS-Antworten zurück.

```
Satellit → UDP → Hannah Proxy → gRPC → Hannah Core
                 (gleicher Host oder anderer Rechner)
```

Solange der Proxy verbunden ist, deaktiviert Core automatisch ihren eigenen UDP-Server und delegiert Audio/TTS vollständig an den Proxy. Trennt sich der Proxy, reaktiviert Core den eigenen UDP-Server. Satelliten verbinden sich automatisch neu, sobald sich das MQTT-Discovery-Topic ändert.

**Wann braucht man ihn?** Ohne Proxy läuft Core mit eigenem UDP-Server — funktional gleichwertig für ein bis wenige Satelliten. Der Proxy ist für größere Installationen gedacht, bei denen Satelliten auf einem separaten Host/Netzsegment näher an sich selbst laufen sollen, statt direkt mit Core zu sprechen.

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/proxy/deploy/install.sh | sudo bash
```

Erkennt die Architektur automatisch (`amd64` / `arm64`), lädt das passende Binary vom Update-Server (Channel `proxy-stable-amd64` / `proxy-stable-arm64`) und installiert den systemd-Service.

**Deinstallieren:**

```bash
sudo systemctl stop hannah-proxy && sudo systemctl disable hannah-proxy
sudo rm /usr/local/bin/hannah-proxy /etc/systemd/system/hannah-proxy.service
```

## Konfiguration (`/etc/hannah-proxy/config.yaml`)

```yaml
proxy_id: hannah-proxy          # eindeutiger Name, erscheint in Hannah-Logs

hannah:
  address: "127.0.0.1:50051"    # gRPC-Adresse von Hannah Core

udp:
  listen_addr: ":7775"          # UDP-Port für Satelliten
  advertise_host: "192.168.8.15"  # IP die Satelliten via MQTT-Discovery erhalten
```

Vollständiges Beispiel: `config.example.yaml` in diesem Verzeichnis.
