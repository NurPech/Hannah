'use strict';

const utils = require('@iobroker/adapter-core');
const mqtt  = require('mqtt');

class HannahNotification extends utils.Adapter {
    constructor(options) {
        super({ ...options, name: 'hannah-notification' });
        this.mqttClient = null;
        this.on('ready',   this.onReady.bind(this));
        this.on('message', this.onMessage.bind(this));
        this.on('unload',  this.onUnload.bind(this));
    }

    async onReady() {
        const { mqtt_broker, mqtt_port, mqtt_user, mqtt_pass } = this.config;

        this.mqttClient = mqtt.connect(`mqtt://${mqtt_broker}:${mqtt_port}`, {
            username: mqtt_user,
            password: mqtt_pass,
            clientId: `iobroker-hannah-notification-${this.instance}`,
            reconnectPeriod: 5000,
        });

        this.mqttClient.on('connect', () => {
            this.log.info(`MQTT verbunden: ${mqtt_broker}:${mqtt_port}`);
            this.setState('info.connection', true, true);
        });
        this.mqttClient.on('error', err => {
            this.log.error(`MQTT Fehler: ${err.message}`);
            this.setState('info.connection', false, true);
        });
        this.mqttClient.on('close', () => {
            this.setState('info.connection', false, true);
        });
    }

    onMessage(obj) {
        if (!obj || obj.command !== 'sendNotification') return;

        this.log.debug(`sendNotification raw: ${JSON.stringify(obj.message)}`);
        const text = this._extractText(obj.message);
        if (!text) {
            this.log.warn('Notification ohne Text empfangen — ignoriert.');
            obj.callback && this.sendTo(obj.from, obj.command, { sent: false, error: 'Kein Text' }, obj.callback);
            return;
        }

        const severity = obj.message?.category?.severity ?? 'notify';
        const payload = JSON.stringify({ text, severity });

        if (this.mqttClient?.connected) {
            this.mqttClient.publish(this.config.hannah_topic, payload, { qos: 1 }, err => {
                if (err) {
                    this.log.error(`Publish fehlgeschlagen: ${err.message}`);
                    obj.callback && this.sendTo(obj.from, obj.command, { sent: false, error: err.message }, obj.callback);
                } else {
                    this.log.info(`Notification → Hannah: [${severity}] "${text}"`);
                    obj.callback && this.sendTo(obj.from, obj.command, { sent: true }, obj.callback);
                }
            });
        } else {
            this.log.warn('MQTT nicht verbunden — Notification verworfen.');
            obj.callback && this.sendTo(obj.from, obj.command, { sent: false, error: 'MQTT nicht verbunden' }, obj.callback);
        }
    }

    /**
     * Extrahiert den Benachrichtigungstext aus dem notification-manager Objekt.
     *
     * Format: { category, scope, instances: { 'adapter.0': { messages: [{ message, ts }] } }, host }
     * Fallback: category.description (de → en → string)
     */
    _extractText(notification) {
        try {
            // Primär: Nachrichten aus category.instances (tatsächliches ioBroker-Format)
            const instances = notification?.category?.instances ?? notification?.instances ?? {};
            const parts = [];
            for (const data of Object.values(instances)) {
                for (const msg of (data.messages ?? [])) {
                    if (msg.message) parts.push(msg.message);
                }
            }
            if (parts.length) return parts.join('. ');

            // Fallback: category description
            const desc = notification?.category?.description;
            if (typeof desc === 'string')   return desc;
            if (desc?.de)                   return desc.de;
            if (desc?.en)                   return desc.en;

            // Letzter Fallback: category name
            const name = notification?.category?.name;
            if (typeof name === 'string')   return name;
            if (name?.de)                   return name.de;
            if (name?.en)                   return name.en;
        } catch (e) {
            this.log.warn(`Text-Extraktion fehlgeschlagen: ${e.message}`);
        }
        return null;
    }

    onUnload(callback) {
        try {
            this.mqttClient?.end();
        } catch (_) { /* ignore */ }
        callback();
    }
}

if (require.main !== module) {
    module.exports = options => new HannahNotification(options);
} else {
    new HannahNotification();
}
