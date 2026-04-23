# Installation auf ioBroker-Host

## Deployment (Symlink-Methode)

Der Adapter liegt unter `/home/iobroker/iobroker.hannah-notification` und wird
per Symlink in ioBrokers `node_modules` eingehängt.

```bash
# 1. Symlink anlegen
ln -s /home/iobroker/iobroker.hannah-notification \
      /opt/iobroker/node_modules/iobroker.hannah-notification

# 2. Dependencies installieren
cd /home/iobroker/iobroker.hannah-notification
npm install

# 3. adapter-core durch Symlink auf ioBrokers Version ersetzen
#    (Node.js löst Symlinks auf — ohne diesen Schritt stimmt __dirname nicht)
rm -rf node_modules/@iobroker/adapter-core
ln -s /opt/iobroker/node_modules/@iobroker/adapter-core \
      node_modules/@iobroker/adapter-core

# 4. Adapter in ioBroker registrieren (einmalig)
cd /opt/iobroker
node node_modules/iobroker.js-controller/iobroker.js add hannah-notification
```

## Updates einspielen

Reicht ein `git pull` im Adapter-Verzeichnis — der Symlink zeigt direkt dorthin:

```bash
cd /home/iobroker/iobroker.hannah-notification && git pull
```

Der Adapter wird von ioBroker automatisch neu gestartet, falls Dateiänderungen
erkannt werden. Alternativ manuell:

```bash
cd /opt/iobroker && node node_modules/iobroker.js-controller/iobroker.js restart hannah-notification.0
```
