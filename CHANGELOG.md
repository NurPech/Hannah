# Changelog
<!--
    Placeholder for the next version (at the beginning of the line):
    ## **WORK IN PROGRESS**
-->

## 0.72.2 (2026-08-11)
### Satellite Firmware

* Added: `/debug/wav/raw` endpoint + WebUI link — captures the TDM beamformed signal *before* `hannah_resample_ctx()`/gain (48kHz), snapshotted from the same trigger as the existing `/debug/wav` capture so both WAVs cover the identical time window. For comparing the pre- and post-resample signal directly while investigating a perceived quality regression ("sounds muffled") introduced with v0.72.0's beamforming/resample pipeline (Refs #222)

## 0.72.1 (2026-08-11)
### Satellite Firmware

* Added: `tdm_debug_raw_slot` debug setting (WebUI, only under `CONFIG_HANNAH_WAKEWORD_DEBUG`) — bypasses beamforming's delay-and-sum and outputs a single TDM raw slot unmodified, for empirically verifying the slot-to-microphone mapping against a known speaking direction (Refs #222)

## 0.72.0 (2026-08-10)
### Satellite Firmware

* Changed: `tdm_downmix_gain` default raised from 32 to 128 — tuned against real-world usage distance via the WebUI-adjustable value introduced in v0.71.9, no clipping observed at close/loud speech, clean signal on the wakeword debug waveform (Refs #222)
* Added: Delay-and-sum beamforming for the Rev5 TDM mic array, replacing the fixed single-channel downmix — raw capture rate raised to 48kHz (ESP32 I2S-TDM clock only, no ADAU7118 register change needed) for finer delay resolution, all 4 channels time-aligned and summed, then downsampled back to 16kHz via AudioLib's new streaming `hannah_resample_ctx()` before the existing gain stage. Preferred listening direction is configurable via NVS/WebUI (`tdm_beam_direction_deg`, 0–315° in 45° steps, default 180° = opposite the power side) without a reflash (Refs #222)
* Fixed: geometry comment in `hannah_audio.c` had the TDM array's North/South channels swapped (verified against the real Rev5 board, 2026-08-11) — corrected before wiring up the beamforming direction table (Refs #222)

## 0.71.9 (2026-08-10)
### Satellite Firmware
* Changed: `tdm_downmix_gain` moved from a compile-time `#define` into `hannah_config_t` — NVS-backed with a Kconfig-seeded default (`HANNAH_TDM_DOWNMIX_GAIN`, default 32), same runtime-override pattern as `wakeword_threshold`/`vad_silence_ms`. Adjustable via the satellite WebUI (Rev5/TDM builds only) without a reflash — useful while the value is still being tuned against real-world usage distance (#222) (Refs #222)

## 0.71.8 (2026-08-10)
### Satellite Firmware
* Changed: Rev5 TDM mic downmix now applies a fixed digital gain (`TDM_DOWNMIX_GAIN=32`, with clipping protection) — the ADAU7118 has no analog/digital gain register of its own, and after the DMA/HPF fixes (#222) the raw signal, while now correct, is very quiet (RMS ~33 even close-range loud speech vs. 32767 full scale). Same approach the existing PDM mic path already uses (×64). Starting value, not yet tuned against real-world usage distance (Refs #222)

## 0.71.7 (2026-08-10)
### Satellite Firmware
* Fixed: ADAU7118's high-pass filter was left at its reset default (disabled) — confirmed real audio now reaches the ESP32 after the DMA buffer fix (#222, v0.71.6), but a DC offset of roughly -256 sits on top of the signal, dominating the RMS reading and burying the much smaller voice-correlated variation. Datasheet text confirms `HPF_EN` (Bit 0, `HPF_CONTROL`) defaults to off; `adau7118_init()` now enables it, keeping the default cutoff frequency (Refs #222)

## 0.71.6 (2026-08-10)
### Satellite Firmware
* Fixed: TDM I2S channel requested `dma_frame_num = 640` (STEP_SAMPLES×4), which at 8 bytes/frame (4 channels × 16-bit) exceeds the ~4092-byte DMA descriptor limit and was silently clamped by the driver to 511 (`dma frame num is out of dma buffer size, limited to 511`, present in every boot log since the first Rev5 test). Combined with known upstream ESP-IDF I2S-TDM DMA buffer sizing bugs (espressif/esp-idf#15126, #10630), this silent clamping is a plausible cause of the corrupted/frozen-looking values seen in every Rev5 capture so far, independent of ADAU7118 register config. `dma_frame_num` is now set well under the descriptor limit for TDM specifically, avoiding the clamp entirely (Refs #222)

## 0.71.5 (2026-08-10)
### Satellite Firmware
* Changed: reverted the `I2S_ROLE_SLAVE` experiment for TDM from v0.71.4 — on real hardware it made things measurably worse, not better: mic warmup took ~108s instead of the usual ~9s (repeated blocking/failing I2S reads), and the wakeword debug log showed a genuine `rms=0.0000 peak=0.0000` (true zero signal, not even the previous noise-floor-like garbage). Back to `I2S_ROLE_MASTER` for all mic types until the actual clock direction is confirmed. Root cause of the silent/near-silent Rev5 TDM captures still open (Refs #222)

## 0.71.4 (2026-08-10)
### Satellite Firmware
* Fixed: `mic_init()` created the I2S channel with `I2S_ROLE_MASTER` for every mic type, including TDM (Rev5/ADAU7118) — but on that path the ADAU7118 itself is the clock source for BCLK/FSYNC, not the ESP32, so the channel needs `I2S_ROLE_SLAVE` there. With the wrong role, the ESP32 was generating its own uncoordinated clock alongside the ADAU7118's, instead of receiving its clock — a plausible root cause for the persistent near-silent Rev5 captures that survived every ADAU7118 register fix so far (#222). PDM/I2S (INMP441) mic types are unaffected, still `I2S_ROLE_MASTER` (Refs #222)

## 0.71.3 (2026-08-10)
### Satellite Firmware
* Fixed: ADAU7118 `DEC_RATIO_CLK_MAP` register was left at its reset default, which maps `PDM_DAT1` to `PDM_CLK0` — but on Rev5, `PDM_DAT1` (MK2+MK4) is actually driven by `PDM_CLK1`. Per the datasheet, this mapping "must be the actual PDM clock that is driving the PDM microphone", so channels 2/3 were being demodulated against the wrong clock reference, plausibly explaining the near-silent captures despite an otherwise correctly configured chip (#222). `adau7118_init()` now explicitly maps DAT1 to CLK1 (Refs #222)

## 0.71.2 (2026-08-10)
### Satellite Firmware
* Changed: `sdkconfig.defaults.rev5` now enables `CONFIG_HANNAH_WAKEWORD_DEBUG=y` — needed to pull real WAV snapshots (`/debug/wav(/capture)`, 4s raw PCM ring buffer) while investigating why Rev5 TDM captures are still silent despite the ADAU7118 register fix (#222, v0.71.0/v0.71.1). Rev5-only, Rev4 default is unchanged. Removed the one-off raw-frame log dump added in v0.71.1 — the debug ring buffer supersedes it; the ADAU7118 register readback log stays (Refs #222)

## 0.71.1 (2026-08-10)
### Satellite Firmware
* Added: temporary diagnostic logging in `hannah_audio.c` (Rev.5/TDM) — reads back the ADAU7118 registers right after writing them and dumps the first raw TDM frame (all 4 channels) once. Reason: despite successful register configuration (#222, v0.71.0), captures are still pure silence — root cause still open (Refs #222)

## 0.71.0 (2026-08-10)
### Satellite Firmware
* Fixed: ADAU7118 (PDM→TDM-Wandler, Rev.5) wurde bisher gar nicht per I2C konfiguriert, sondern lief nur auf seinem Power-on-Default — dabei stand die serielle Slot-Breite auf 32-bit, während der ESP32-seitige I2S-TDM-Kanal auf 16-bit-Slots konfiguriert ist. `adau7118_init()` setzt jetzt per Register-Schreibzugriff TDM-Modus mit 16-bit-Slots, aktiviert nur die tatsächlich bestückten Kanäle 0-3 (MK1-MK4) und nimmt die unbestückten Kanäle 4-7 vom Bus (Refs #222)

## 0.70.1 (2026-08-10)
### Hannah Core
* Fixed: Activity-Log crashte für Satelliten-/VoiceID-Sprecherkennung mit einem nicht-numerischen Label (z.B. Klarname statt `users.id`) — `speaker_user_id` wird jetzt vor dem Schreiben über `UserManager` aufgelöst (erst per ID, dann per Username als Fallback), statt ungeprüft in die `INTEGER`-Spalte geschrieben zu werden (Refs #221)

## 0.70.0 (2026-08-09)
### Hannah Core
* Added: Activity-Log — protokolliert pro verarbeitetem Kommando Kanal (Satellit/Telegram/ioBroker/gRPC), Zeitpunkt, erkannten NLU-Intent samt Metadaten, Antworttext und (bei Voice-Kanälen) eine WAV-Aufnahme. Läuft in einer separaten MySQL-Datenbank (bewusst getrennt von `hannah.db`/SD-Karte), deaktiviert sich selbst ohne konfigurierten Host. Behebt nebenbei, dass `source_service`/`source_user_id` bei `SubmitText`/`SubmitVoice` bisher nach der User-Auflösung verworfen wurden, statt den Kanal erkennbar zu machen. Needs `dialectorm-m1kad0>=0.1.2` (dialekt-agnostische Schema-Erzeugung) (Refs #220)

## 0.69.4 (2026-08-09)
### Hannah Core
* Added: per-message `compat_version` check (`CompatVersionInterceptor`) alongside the existing global `enforce_protocol_version` check — a breaking change scoped to one message's schema no longer has to reject every client, only calls that actually use the affected message. Additive, not a replacement: both interceptors run, each with their own `enforce` toggle (`enforce_compat_version`, default `false`, same safe-rollout pattern as `enforce_protocol_version`). Needs `hannah-proto>=3.1.0` (`hannah_proto.interceptor`) (Refs #217)

### Hannah Proxy
* Changed: `hannah-proto-go` bumped to `v3.1.0` (module path now `.../v3`) — client now also attaches `x-compat-version` alongside the existing `x-proto-version`, same additive reasoning as the Core-side change above (Refs #217)

### Telegram
* Changed: `hannah-proto` bumped to `>=3.2.0` — client now also attaches `x-compat-version` via `CompatVersionClientInterceptor`, alongside the existing `x-proto-version`, same additive reasoning as the Core-side change above (Refs #217)

## 0.69.3 (2026-08-06)
### Hannah Core
* Fixed: DND-Änderungen wurden nie an den Adapter zurückgemeldet — `_on_dnd`, `_on_agent_satellite_control`s `dnd`-Zweig und `_apply_global_dnd` pushen jetzt `agent_satellite_update(..., dnd=...)`, analog zum bestehenden `mute`-Muster. Betroffener ioBroker-State blieb dadurch dauerhaft `ack:false` — keine Bestätigung, ob eine DND-Aktion tatsächlich griff. Zusätzlich: `_on_agent_satellite_control`s Log-Zeile zeigte immer `room=`, auch wenn tatsächlich per `device_id` aufgelöst wurde. Benötigt `hannah-proto>=2.1.0` (`AgentSatelliteUpdate.dnd`) (Refs #213)

## 0.69.2 (2026-08-06)
### Hannah Core
* **Breaking**: Gruppen referenzieren jetzt Satelliten (`device_id`) direkt statt Räume — `group_rooms` (DB) ersetzt durch `group_satellites`, bestehende Gruppen werden beim ersten Start automatisch migriert (jeder Satellit, der laut DB aktuell im migrierten Raum sitzt). `config.yaml`'s `groups:`-Block entfällt ersatzlos (vollständig durch das DB-/Admin-UI-Gruppenmodell abgelöst). Benötigt `hannah-proto>=2.0.1` (`Group.satellites`/`SetGroupSatellites`, `PROTO_VERSION` 4→5) (Refs #56)

### Hannah Proxy
* Changed: `hannah-proto-go` auf `v2.0.1` gebumpt (Modul-Pfad jetzt `.../v2`) — Pflicht-Update, da Hannah Core `enforce_protocol_version` exakt matcht und den Proxy sonst ab dem nächsten Core-Release ablehnt (Refs #56)

### Telegram
* Changed: `hannah-proto` auf `>=2.0.1` gebumpt — Pflicht-Update aus demselben Grund wie beim Proxy (Refs #56)

## 0.69.1 (2026-08-06)
### Hannah Core
* Fixed: satellite connect sound (#7, v0.69.0) never actually played on quick reconnects/reflashes — `_on_satellite_change()`'s new-vs-known diff silently skipped the whole connect fanfare whenever a device was still in `_known_satellites` from before (no clean `NotifySatelliteGone` in between). Redesigned: the satellite now plays its own connect sound locally the moment it registers, instead of waiting for a `play_asset` command from Core — `"connect"` removed from `RELEVANT_ASSET_IDS`/no longer sent by Core at all (Refs #7)

### Satellite Firmware
* Changed: `"connect"` added to the firmware's fixed asset-relevance exception list (alongside `"wakeword"`) and a new `hannah_net_set_registered_callback()` plays it locally right after UDP registration — no command from Core needed, same eventually-consistent-across-reboots behavior as wakeword model updates (Refs #7)

## 0.69.0 (2026-08-06)
### Hannah Core
* Changed: timer jingle and alarm ring-cycle now wait for the satellite's real `playback_done` ack instead of a blind `time.sleep()`/fixed `cycle_seconds` timer derived from the asset manifest's `duration_s` — alarm ring cadence now follows actual playback length, with `cycle_seconds` repurposed as a safety timeout for satellites without the ack (old firmware). Core no longer reads the satellite-namespace asset manifest at all; `_load_asset_manifest()`/`_asset_manifest` removed (Refs #169)
* Changed: satellite connect sound migrated to the asset-server `play_asset` pattern — Core no longer loads a local `core/sounds/satellite_connected.wav` and streams it as raw PCM on connect; it now publishes `play_asset(device, "connect")` like timer jingle/alarm ring, and adds `"connect"` to the per-satellite asset-relevance list so it gets cached like any other asset (Refs #7)
* Added: `TimeQuery`/`DateQuery` NLU intents — "wie spät ist es"/"welches Datum haben wir" now get answered directly from `datetime`, no LLM required (Refs #211)

### Satellite Firmware
* Removed: `CONFIG_HANNAH_VAD_ENERGY_THRESHOLD` (dead code) — the computed `min_thr`/`adaptive_thr` value was never passed anywhere; the actual streaming silence decision runs exclusively through `hannah_webrtc_vad_feed()` (frequency-based, no energy-threshold parameter). Found while debugging #204 (Refs #205)

## 0.68.0 (2026-08-05)
### Hannah Core
* Changed: `WeatherCache` no longer parses MQTT topics directly (`openweathermap/0/forecast/...`, hardcoded to one vendor) — it now consumes the new `AgentWeatherUpdate` message pushed by the `iobroker.hannah` adapter over the existing `AgentConnect` stream, making the weather source provider-independent. `hannah-proto` bumped to 1.1.0. No MQTT fallback; `build_answer()` and its answer-generation logic are unchanged (Refs #209)

## 0.67.28 (2026-08-04)
### Hannah Core
* Fixed: Core crash-looped on every boot (`AttributeError: 'str' object has no attribute 'get'`) whenever a `residents`-linked account's `provider_payload` had been double-JSON-encoded by the `LinkAccount` gRPC handler (which passed the webui's already-serialized JSON string straight to the `__json_fields__`-backed model column, which serializes it again). `LinkAccount` now decodes the incoming JSON string before persisting; `UserManager._resident_link()` and the car-tracker's equivalent lookup also no longer crash on a malformed/legacy `provider_payload` shape (Refs #207)

## 0.67.27 (2026-08-04)
### Hannah Core
* Fixed: a resident's `display_name` could get wiped back to empty shortly after every adapter restart, for any resident whose presence changes live (most noticeably real, actively-tracked people) — a presence-only `AgentResident` update from the adapter always sent an empty name, which `Resident.update()` applied unconditionally, overwriting the name known from the last snapshot. `hannah-proto` bumped to 1.0.2 (`AgentResident.name`/`presence_state` are now proto3 `optional`); `Resident.update()`, `_on_agent_resident()` and the `resident_update` gRPC handler now only overwrite a field when the incoming message actually set it (Refs #206)

### Hannah Proxy
* Changed: `hannah-proto-go` bumped to v1.0.2, matching Core's protocol version bump (Refs #206)

### Telegram
* Changed: `hannah-proto` bumped to 1.0.2, matching Core's protocol version bump (Refs #206)

## 0.67.26 (2026-08-04)
### Satellite Firmware
* Changed: built-in wakeword default model (`hannah_wakeword/model/model.h`, `models/hannah.tflite`) updated to a new retrain (Refs #168)
* Changed: `CONFIG_HANNAH_VAD_WEBRTC_AGGRESSIVENESS` default raised from 2 to 3 (max) — in rooms with steady background noise (TV/radio/computer) the silence counter never reached the required threshold, so recordings regularly ran into the 10s hard timeout instead of ending on real silence (Refs #204)
* Changed: `CONFIG_HANNAH_WAKEWORD_DEBUG` default flipped from `y` to `n` now that #198 has proven itself over a longer stretch of live use — drops the periodic debug log line and the ~125 KB PSRAM ring-buffer allocation; `/debug/wav(/capture)` stay reachable, just report "no capture available" (Refs #199)

## 0.67.25 (2026-08-03)
### Hannah Core
* Fixed: the satellite room fallback introduced in #201 resolved a room but still found no devices whenever the room's ioBroker enum ID wasn't already an all-lowercase slug (e.g. `OG Zimmer Süd` instead of `wohnzimmer`). Both `pipeline()` and `_handle_text()` set `intent.room_id = room.lower()`, but `satellite_manager.get_satellite_room()` already returns the canonical, case-preserved room ID matching `IoBrokerClient.devices`'s dict keys exactly — no normalization needed or wanted. Dropped the `.lower()` call at both sites (Refs #203)

## 0.67.24 (2026-08-03)
### Hannah Core
* Fixed: voice commands without an explicit room (e.g. "Licht aus") found no devices/room when spoken through a Proxy-connected satellite (`SubmitSatelliteAudio` gRPC path), while the same command with an explicit room worked fine. The satellite's assigned-room fallback only existed in `pipeline()` (the older direct-UDP path) — `_handle_satellite_audio()` → `_handle_text()` never received it. `_handle_text()` now takes an optional `device` argument and applies the same fallback (`satellite_manager.get_satellite_room(device)`) when no room was recognized in the text (Refs #201)

### Satellite Firmware
* Fixed: the persisted crash log (`/assets/_hannah_last_log.txt`, written before an ordered restart so it's retrievable afterward via `GET /log/last`) was deleted again by the very next asset-sync right after boot. It shares its LittleFS mount with the downloaded asset cache, and `garbage_collect()` in `hannah_asset.c` removed anything there not on the current manifest — the leading-underscore filename was chosen specifically to be ignored by asset-sync, but that exception was never actually implemented. `garbage_collect()` now skips any filename starting with `_`, since manifest asset IDs never do (Refs #202)

## 0.67.23 (2026-08-03)
### Satellite Firmware
* Added: `CONFIG_HANNAH_WAKEWORD_DEBUG` Kconfig option (default `y` for now, see #199) gating the wakeword debug infrastructure built up across #173/#180/#194/#197 — the periodic `Wakeword-Debug` log line (rms/peak/confidence/mel-/input-preview), the 4s raw-PCM ring buffer, and both the Vol+/Vol- button-combo and remote (`GET /debug/wav/capture`) snapshot triggers. `GET /debug/wav`/`/debug/wav/capture` stay registered either way and just report "no capture available" when the flag is off, so `hannah_webserver.c` didn't need to change. Kept default `y` deliberately — this tooling was decisive in finding #198, and we don't want to lose it until that fix has proven itself over a longer stretch of live use. Flip to `n` once confident, to skip the log spam and ~125 KB PSRAM ring-buffer allocation (Refs #199)

## 0.67.22 (2026-08-03)
### Satellite Firmware
* Fixed: OTA hung forever (no crash, no restart, but webserver/wakeword already torn down and download never starting) whenever the satellite was idle — not actively playing TTS/an announcement — at the moment an automatic OTA check triggered, which is the normal case. `speaker_task()`'s `s_hw_paused` check (used to synchronize I2S teardown before the download, #193) sat behind the `if (!item) continue;` branch of its ring-buffer receive loop, so it was never reached while `xRingbufferReceive()` kept timing out with nothing queued — `s_speaker_parked_sem` was never given, and `hannah_audio_deinit_for_ota()` blocked forever on it. Moved the check to the top of the loop, unconditional of the receive result, matching the already-correct `mic_task` pattern (Refs #200)

## 0.67.21 (2026-08-03)
### Satellite Firmware
* Fixed: wakeword confidence stayed low live despite near-perfect offline detection on identical audio. `hannah_wakeword_process()` converted raw `FrontendOutput` values to the model's float feature range using a scale of `128.0f`, but the actual training-reference implementation (`pymicro_features`, used by both the training pipeline and `test_inference.py`) uses `1/0.0390625 = 25.6`. Confirmed via exact frame-aligned comparison (using the `frame_no` counter from #197) between live-logged `mel_preview` values and an offline-recomputed reference for the same debug-WAV capture: mel-spectrum shape correlated 0.89–1.00, but amplitude was off by a constant ~5× — exactly `128/25.6`. Live features were therefore systematically ~5× quieter than what the model was trained on (Refs #198)

## 0.67.20 (2026-08-02)
### Satellite Firmware
* Fixed: satellite could hard-crash (`abort()` inside TFLite Micro's `GetQuantizedConvolutionMultipler`) right as an OTA update started. `hannah_audio_pause_wakeword()` only set a flag and returned immediately, so `hannah_wakeword_deinit()` (called right after, freeing the PSRAM TFLite arena) could run while `mic_task` was still mid-`Invoke()` on a previous audio frame — a use-after-free on the arena. Now blocks on a semaphore, mirroring the existing `hannah_audio_deinit_for_ota()`/`s_mic_parked_sem` pattern, until `mic_task` has confirmed it observed the pause and won't start another `Invoke()` (Refs #196)
* Added: `frame_no` counter in the periodic `Wakeword-Debug` log line and both debug-WAV-snapshot log lines (button and remote trigger) — a running count of mic_task iterations (10ms each), synchronized with the debug ring buffer write loop. Lets a downloaded debug WAV be mapped back to an exact sample offset for any given debug log line (`(debug_frame_no - (snapshot_frame_no - 399)) × 160`), instead of correlating via wall-clock timestamps (Refs #197)

## 0.67.19 (2026-08-02)
### Satellite Firmware
* Fixed: wakeword model override (#166) quantized/dequantized every loaded model using hardcoded scale/zero_point/dtype constants taken from `hey_hannah_int8.tflite`, regardless of what the actually-loaded override model's own tensors specified. Now read dynamically from the model's `TfLiteTensor` metadata after `AllocateTensors()`, with a clean rejection (detection stays disabled) if a model uses a tensor type other than int8/uint8 instead of silently computing garbage (Refs #195)
* Added: remote-triggerable debug WAV capture (`GET /debug/wav/capture`) — same 4s microphone ring-buffer snapshot as the existing Vol+/Vol- button combo (#180/#182), but triggerable from the satellite's web UI (works from phone/PC on the same LAN, no JavaScript) instead of requiring physical proximity to hold both buttons. A single blocking request arms the capture (LED switches to the existing purple CAPTURE state), waits out a 3.5s speaking window, then returns the WAV directly — useful for testing wakeword models from realistic conversational distance instead of arm's-length button range (Refs #194)

## 0.67.18 (2026-08-02)
### Satellite Firmware
* Fixed: OTA still failed with `esp-aes: Failed to allocate memory` after #188/#191, observed live on a second satellite only ~73s after boot (not long-uptime fragmentation). Root cause: the hardware AES-DMA engine (`esp_aes_dma_core.c`) allocates its own DMA descriptors/alignment buffers directly with `MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL`, bypassing `mbedtls_platform_set_calloc_free()` entirely — neither #188 nor #191 could reach this allocation, since DMA-capable memory for the crypto hardware must architecturally stay internal, no Kconfig policy can redirect it to PSRAM. `CONFIG_MBEDTLS_HARDWARE_AES=n` forces software AES instead, which goes through the normal (PSRAM-capable since #188) allocator — eliminates this failure mode structurally rather than just giving it more headroom. No measurable throughput impact expected for the occasional HTTPS use here (OTA, asset manifests) — ESP-IDF's own docs note hardware AES offers no speed advantage at 240MHz CPU clock anyway (Refs #192)
* Changed: the webserver and the full audio pipeline (I2S mic/speaker channels, not just wakeword inference) are now shut down before an OTA download starts, and restarted on OTA failure. The existing pre-OTA wakeword pause only freed PSRAM (128KB TFLite arena) — the actual internal-DRAM, DMA-bound consumers (I2S RX/TX channels, the 32KB speaker ring buffer's underlying DMA descriptors) stayed fully allocated and active throughout every previous OTA attempt. `mic_task`/`speaker_task` now check a new hardware-pause flag *before* touching their I2S channel handles (not just after, like the existing wakeword pause) and hand back control via a semaphore before the channels get torn down, avoiding a delete-while-in-use race on a blocking I2S call. Satellite is already unresponsive to wake words during OTA today, so being fully offline for the few seconds of a download is not an additional UX regression. New territory for this codebase (no prior I2S channel teardown/recreate at runtime) — worth watching closely on the first few live OTA cycles (Refs #193)

## 0.67.17 (2026-08-02)
### Satellite Firmware
* Changed: `CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL` lowered from the ESP-IDF default (16KB) to 512 bytes, so plain `malloc()` calls now default to PSRAM instead of the small, permanently-contended internal DRAM pool. Real-time/DMA/ISR allocations (I2S DMA buffers, FreeRTOS task stacks) are unaffected — they use explicit capability requests or are hard-forced internal by IDF regardless of this setting. `CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL=32768` (matching the prior implicit default) is now set explicitly to guarantee a floor for those. Generalizes the fix from #188 (which addressed only mbedTLS) to every subsystem doing a plain, unpinned `malloc()` — e.g. `hannah_webserver.c`'s request/response buffers, suspected contributor to the #150 pattern (heartbeat alive, OTA/webserver dead) (Refs #191)
* Added: heap watchdog in `heartbeat_task` — restarts the satellite via the same ordered `esp_restart()` path as the network watchdog/remote-restart (so the log trail survives) once free internal DRAM (`MALLOC_CAP_INTERNAL`) drops below a configurable threshold (`CONFIG_HANNAH_HEAP_WATCHDOG_THRESHOLD_BYTES`, default 20KB, 0 disables it). Closes the gap the network watchdog can't: in three prior incidents (#150/#161/#184) network liveness (WiFi/MQTT/UDP heartbeat) stayed intact while resource-heavy paths (webserver, TLS/OTA, and in the latest incident even MQTT command handling) had already died on an exhausted internal heap, and the most recent case even needed a hard EN-pin reset that lost the log trail entirely. Diagnostic/rejuvenation measure only, not a fix for the underlying fragmentation cause. The periodic heap log line now also reports internal-only free heap, needed to calibrate the threshold — the existing combined figure is dominated by PSRAM since #191 and barely moves even when internal DRAM is nearly exhausted (Refs #184)

### Hannah Core
* Fixed: a room clarification answer (e.g. "OK, Zimmer Süd.") could resolve to the wrong candidate whenever the STT transcript carried punctuation. `resolve_clarification_answer()` matched words directly against the raw `_normalize()`d transcript instead of going through the usual punctuation-stripping tokenizer, so a trailing period glued to the last word (`"sued."`) never matched the punctuation-free candidate name (`"sued"`) — both candidates ended up on an equal, non-zero score, and a strict `>` comparison silently kept whichever one was first in the list rather than the one actually named. Now strips punctuation before matching, and a genuine tie between candidates returns "no match" instead of guessing, letting the caller fall back to re-parsing the text as a fresh command. `resolve_yes_no()` had the identical bug (e.g. `"Ja."` not matching `"ja"`) and got the same fix (Refs #190)

## 0.67.16 (2026-08-02)
### Satellite Firmware
* Fixed: OTA updates reliably failed (`esp-aes: Failed to allocate memory`) shortly after the download started, even with no wake-word/streaming activity happening at the same time. Root cause: `sdkconfig.defaults` set `CONFIG_MBEDTLS_PLATFORM_MEMORY=y` intending to route mbedTLS's buffers into PSRAM — but that Kconfig symbol doesn't exist in this ESP-IDF version and was a silent no-op, so every TLS buffer (including OTA's) had always been allocated from the small internal DRAM pool, which is permanently under pressure from the always-on audio pipeline (mic/speaker tasks + I2S DMA, running since boot). Replaced with `CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=y`, the option that actually does this (Refs #188)

## 0.67.15 (2026-08-02)
### Satellite Firmware
* Added: `asset_namespace` — optional per-device NVS override for the asset-manifest namespace (settable via `/settings` or `POST /nvs`), defaults to the hardcoded `"satellite"` when empty. Lets a single satellite be pointed at an isolated namespace (e.g. `satellite-test`) to trial risky assets like a new wakeword model without exposing the rest of the fleet (Refs #187)
* Changed: asset downloads (`GET /assets/{key}`) now also send `?namespace=` (same value as the manifest fetch), matching the effective `asset_namespace` (Refs #187)

## 0.67.14 (2026-08-01)
### Hannah Core
* Fixed: a satellite's display name reverted to its MAC in ioBroker's Device Manager within seconds of being set correctly, because the recurring proxy heartbeat push and the volume/mute-change pushes to the adapter never carried `display_name` — only the one-time "newly seen" transition and `GetSatellites()` resolved it. All `agent_satellite_update()` call sites now resolve and pass the display name consistently (Refs #186)

## 0.67.13 (2026-08-01)
### Satellite Firmware
* Fixed: loading a wakeword model using an op not registered in the TFLite Micro resolver (e.g. `okay_nabu_v2.tflite`'s `SplitV`, a different microWakeWord architecture than `hey_hannah`) boot-looped a live satellite — `AllocateTensors()` correctly failed and was caught, but the existing cleanup path (`delete` on a MicroInterpreter with a partially-built op graph) crashed hard (`Guru Meditation Error`), with no remote recovery path reachable (OTA/asset-resync both run later in boot than wakeword init). Now validates every op code the model uses against the resolver *before* ever constructing the interpreter or calling `AllocateTensors()` — an incompatible model logs which op is missing and disables detection cleanly instead. Also stopped deleting the interpreter on any `AllocateTensors()` failure at all (small internal-heap leak instead, PSRAM arena still freed normally) as a second line of defense, in case some other failure mode leaves the graph partially built. `SplitV` itself is now registered too, so `okay_nabu_v2.tflite` actually loads (Refs #183)
* Changed: `models/hannah.tflite` re-synced with the model actually compiled into `model.h` (was stale since 2026-04-18 while `model.h` moved on) — reference copy only, not read by the build

## 0.67.12 (2026-08-01)
### Hannah Core
* Fixed: satellite `last_seen` shown in ioBroker froze right after a satellite (re-)registered instead of tracking real liveness — `GetSatellites()` only pushes `last_seen` to the adapter once, at `AgentConnect` time, while Core's own DB kept advancing on every proxy heartbeat (`RegisterProxy`, ~10s). The heartbeat loop now also pushes `agent_satellite_update` for every currently known proxy satellite, so the adapter's `last_seen` stays live instead of freezing at the last connect/reconnect (Refs #185)

### Satellite Firmware
* Fixed: `/debug/wav`'s Vol+/Vol- snapshot trigger fired the instant the 700ms hold threshold was reached, not on release — anyone pressing the combo at the same time as speaking the test phrase (the natural way to use it) got a recording cut off mid-word (observed: "Okay Nabu" → only "Okay" audible). Hold now only arms the trigger (debounce against accidental presses); the snapshot itself fires on release, so it captures whatever was said for the whole duration the combo was held, regardless of hold length (Refs #182)

## 0.67.11 (2026-08-01)
### Satellite Firmware
* Added: `GET /debug/wav` on the satellite's web server — dumps the last ~4s of raw mic PCM as a WAV file, frozen by holding Vol+ and Vol- simultaneously for ~700ms. Runs passively alongside normal wakeword listening (no sampling/capture-mode switch, no behavior change) so the exact audio the wakeword pipeline sees can be pulled off-device and replayed through `test_inference.py` — needed after #179's arena fix didn't resolve on-device wakeword confidence staying flat at 0.0000 even with a model independently verified at >98% TPR/3% FPR offline, narrowing the search to hardware/on-device pipeline vs. model quality (Refs #180)
* Fixed: `hannah_wakeword_process()` always wrote exactly one 40-value AudioFrontend frame into the model's input tensor, regardless of the tensor's actual size — correct for `hey_hannah_int8.tflite` (shape `(1,1,40)`) but any model expecting multiple aggregated frames as one input (e.g. `okay_nabu`, used as a differential-test reference model) would only ever get its first frame's worth of real data, the rest left as stale/garbage bytes from the last allocation. Frame count is now derived from the input tensor's actual byte size at load time; multi-frame inputs get a sliding window (oldest frame shifted out, newest appended) before each `Invoke()`. No behavior change for single-frame models (Refs #181)

## 0.67.10 (2026-07-31)
### Satellite Firmware
* Fixed: wakeword's streaming resource-variable arena (`RV_ARENA_SIZE`) was a fixed 4KB in internal DRAM, independent of the loaded model — a differential test against `okay_nabu.tflite` (Home Assistant) showed its 11 streaming states need ~8KB total (largest single state alone: 7104 bytes), already exceeding the whole arena. `AllocateTensors()`/`Invoke()` reported no error since the separate main tensor arena was large enough, but the streaming context itself likely never accumulated correctly, leaving `output_raw` stuck (matches the symptom exactly: model "runs", output never moves meaningfully). Grown to 32KB and moved to PSRAM, matching the main arena's lifecycle including freeing on every `tflite_init()` failure path and `tflite_deinit()` (Refs #179)

## 0.67.9 (2026-07-31)
### Satellite Firmware
* Fixed: the syslog client added in v0.67.8 (#176) called `socket()`/`fcntl()`/`sendto()` directly from `log_capture()`, the global `esp_log_set_vprintf()` hook that runs on the stack of whichever task happens to be logging — including small system/driver tasks never sized for extra socket-syscall stack usage. Caused a hard crash/boot loop (`restart_reason: Panic`) on any device with `syslog_host` configured, invisible in `/log`/`/log/last` since a real panic skips the graceful-shutdown log persistence entirely. `log_capture()` now only does a cheap queue copy; a dedicated task with its own 4KB stack does the actual socket work (#177)

## 0.67.8 (2026-07-31)
### Satellite Firmware
* Added: fire-and-forget UDP syslog client (RFC 5424), additive to the local `/log` ring buffer, not a replacement — `/settings` gained a "Syslog" section (`syslog_host` as IPv4 literal, `syslog_port`, default 514, empty host = disabled). Every captured log line is sent non-blocking to the configured receiver alongside the existing ring buffer / UART output, so the buffer's fixed size (#175) no longer risks losing early boot messages before anyone gets to look at `/log` (Refs #176)

## 0.67.7 (2026-07-31)
### Satellite Firmware
* Fixed: `/log`'s ring buffer was only 8KB and lived in internal DRAM — the periodic wakeword debug log (#173) alone produces ~1.2 KB/s, overwriting the buffer within ~7s and making one-time boot/asset-sync messages (e.g. wakeword model override confirmation) practically impossible to catch. Moved to PSRAM and grown to 64KB (Refs #175)

## 0.67.6 (2026-07-31)
### Satellite Firmware
* Fixed: root cause of on-device wakeword confidence staying flat at 0.0000 despite the model validating correctly offline — `hannah_wakeword_init()` explicitly set `noise_reduction.min_signal_remaining = 1.0f`, effectively neutering noise reduction in the AudioFrontend. The training pipeline (`pymicro-features`, `MicroFrontend()` with no parameters) uses the library default of `0.05`, verified directly against both `pymicro-features`' and TFLite Micro's own source. Live features were systematically shifted away from what the model was trained on. Traced to an IDF 6.0 compatibility change that replaced the removed `enable_noise_reduction = 0` field with this value to preserve "noise reduction disabled" — training never ran with it disabled, so the substitution introduced the mismatch. Fix: stop overriding the field, let `FrontendFillConfigWithDefaults()`'s 0.05 default apply, matching training exactly (Refs #173, #174)

## 0.67.5 (2026-07-31)
### Satellite Firmware
* Added: `hannah_wakeword_last_debug()` — exposes the AudioFrontend's `feat.size`/`num_read` (does it even produce complete feature frames?), a preview of both the raw pre-quantization mel values and the quantized int8 values actually fed to the model, the raw unscaled output tensor byte, and a cumulative `Invoke()` failure counter. Wired into the existing periodic idle debug log (#171), which also gained mic peak amplitude (clipping check) alongside the existing RMS. All in one release rather than iterating — on-device wakeword confidence stays flat at 0.0000 despite the model validating correctly offline (72.5% true-positive rate on real recordings via `test_inference.py`), and this is the full diagnostic chain needed to narrow it down without physical device access (Refs #173)

## 0.67.4 (2026-07-31)
### Satellite Firmware
* Added: periodic wakeword debug log in `hannah_audio` (every ~500ms while idle) — mic RMS level and peak wakeword confidence, independent of whether detection actually fires. Lets a live device confirm whether audio is reaching the model at all and how close confidence gets to the threshold, without needing a detection event (Refs #171)
* Changed: `CONFIG_HANNAH_TFLITE_ARENA_KB` default lowered 4096 → 128 KB — a working retrain (correct op set, no hybrid quantization) actually loaded and reported `arena_used_bytes()` of 29332 B, the empirical number this whole saga (#171) was after from the start. 128 KB keeps a ~4.5× margin over that without wasting PSRAM; `range` stays at `64 4096` for future retrains that may need more (Refs #171)
* Fixed: OTA download could fail with `esp-aes: Failed to allocate memory` — mbedTLS is globally pinned to PSRAM (`hannah_net.c`), and the wakeword TFLite arena stayed allocated in that same PSRAM pool for the whole OTA download (`hannah_audio_pause_wakeword()` only paused inference, never freed the arena). `hannah_wakeword` gained `hannah_wakeword_deinit()`/`hannah_wakeword_reinit()` (arena + interpreter now heap-allocated instead of a non-freeable function-local `static`); `hannah_ota` frees the arena right before `esp_https_ota()` for maximum PSRAM headroom (#172)
* Fixed: a failed OTA left wakeword detection dead until a manual reboot — no code path ever un-paused it. Added `hannah_audio_resume_wakeword()` and `hannah_asset_remount()` (SPIFFS is unmounted for OTA, needed again for the asset-cache model override), wired into `hannah_ota`'s failure branch alongside `hannah_wakeword_reinit()` — recovers automatically, no manual intervention needed (#172)

## 0.67.3 (2026-07-31)
### Satellite Firmware
* Fixed: root cause of the wakeword `AllocateTensors()` failures (#171) was never arena size — the retrained model's `arena_used_bytes()` on failure was only ~1.9 KB out of 4096 KB available, way too little to be a capacity issue. Static analysis of the model's flatbuffer `operator_codes` showed it uses `TRANSPOSE`/`SUB`/`SQRT`/`DIV` ops (a LayerNorm-style architecture, no `CONV_2D`/`DEPTHWISE_CONV_2D` at all — a different network topology from the built-in inception/streaming model), none of which were registered in `hannah_wakeword`'s `MicroMutableOpResolver<20>`. Added the 4 missing ops — fits exactly at the existing capacity of 20 (Refs #171)

## 0.67.2 (2026-07-31)
### Satellite Firmware
* Changed: `CONFIG_HANNAH_TFLITE_ARENA_KB` default raised 1024 → 4096 (Kconfig `range` widened to `64 4096`) — 1024 (set in v0.67.1) still wasn't enough for the retrained wakeword model, `AllocateTensors()` still failed. Jumping straight to a generous value instead of doubling again, PSRAM has ample headroom (8 MB on N16R8) (Refs #171)
* Added: on `AllocateTensors()` failure, `hannah_wakeword` now also logs `arena_used_bytes()` — gives a concrete lower-bound data point instead of just "Arena zu klein?" if this ever needs to be raised again (Refs #171)

## 0.67.1 (2026-07-31)
### Satellite Firmware
* Changed: `CONFIG_HANNAH_TFLITE_ARENA_KB` default raised 512 → 1024 — 512 (set in the v0.67.0 release) still wasn't enough for a newly retrained wakeword model, `AllocateTensors()` still failed (Refs #171)

## 0.67.0 (2026-07-31)
### Hannah Core
* Added: `mqtt_handler.publish_asset_relevant()` — publishes a retained per-satellite MQTT topic (`hannah/satellite/{device}/assets/relevant`) listing the asset IDs Core actually needs on that satellite (currently `alarm_ring`, `timer_jingle`), sent whenever a satellite (re-)connects. Replaces `hannah_asset`'s previous blind "load everything in the manifest" behavior with a list Core derives from its own business logic, without reading the `satellite`-namespace manifest itself (Refs #170)

### Satellite Firmware
* Changed: `hannah_asset` now caches only what's actually relevant instead of blindly downloading everything listed in the `satellite`-namespace manifest. Reacts to Core's new retained `hannah/satellite/{device}/assets/relevant` MQTT topic (a JSON array of asset IDs) instead of running a single blind pass ~50s after boot — a fixed, firmware-internal exception list (currently just `wakeword`, #166's model override) stays relevant independent of Core. Assets no longer covered by either list are now garbage-collected from the SPIFFS cache (incl. their sha256 NVS entry) instead of accumulating forever (Refs #170)
* Fixed: `hannah_wakeword_init()` logged "Wakeword bereit" unconditionally even when `AllocateTensors()` failed, silently leaving wake detection disabled (`hannah_wakeword_process()` always returning 0.0) with no indication in the log. Now only logs ready on actual success, otherwise a clear error (Refs #171)
* Changed: `CONFIG_HANNAH_TFLITE_ARENA_KB` default raised 256 → 512 (Kconfig `range` widened to `64 1024`) — a newly retrained wakeword model needed more scratch memory than the previous default provided (Refs #171)

## 0.66.0 (2026-07-30)
### Satellite Firmware
* Added: wakeword model override via the existing Asset-Server cache (`hannah_asset`, namespace `satellite`, asset ID `wakeword`) — lets a newly trained `.tflite` model be tested by upload alone, without a firmware release. `hannah_asset` generalized to cache assets by raw ID (dropped the hardcoded `.wav` suffix) and gained `hannah_asset_read_to_psram()` for non-audio consumers. `hannah_wakeword` loads the cached override into PSRAM at init if present and valid, otherwise falls back to the built-in default model. Asset-cache init moved earlier in `main.c` (before `hannah_audio_init()`, which synchronously triggers wakeword init) so SPIFFS is mounted in time. Takes effect on next boot, not a live hot-swap (Refs #166)

## 0.65.0 (2026-07-30)
### Hannah Core
* Added: periodic preventive "rejuvenation" restart per satellite (`SatelliteManager.check_and_restart_due_satellites()`), configurable via `satellite_manager.restart_interval_days` (default 7). Reuses the existing `#161` remote-restart MQTT command, checked hourly alongside the existing seed-cleanup loop; per-device hash offset spreads restarts across the day instead of firing all at once, and a restart is only triggered while the satellite is actually idle (no active conversation/audio stream/TTS, and not in DND) — otherwise it's retried on the next hourly pass. A manual restart via `TriggerSatelliteRestart` now also resets the interval clock (Refs #162)
* Added: real relevance check for the Smalltalk follow-up mic window (#158) — `llm.classify()` gained a third `NOT_ADDRESSED` category (plus conversation history as input) to catch utterances that aren't actually directed at Hannah (e.g. a conversation between people present, coincidentally picked up during the open mic window) instead of misrouting them into NLU as a device command. Replaces the `ConversationContext.is_addressed_to_hannah()` stub — folded into the existing classify() call instead of adding a second LLM round-trip per utterance (Refs #159)
* Added: satellite restart history (`satellite_restarts` table, `SatelliteManager.record_restart_report()`/`get_restart_reports()`) — the `firmware`-topic MQTT payload now carries `restart_reason`/`restart_count` reported by the satellite (#165), persisted per report instead of only the single most recent value. Deduplicated against `Satellite.last_reported_restart_count` since the `firmware` topic is retained and would otherwise re-record a phantom entry on every Core reconnect (Refs #165)

### Satellite Firmware
* Fixed: `sdkconfig.defaults.rev4`'s header comment pointed at `satellite-esp/hardware/Phase2/GPIO_Map.md`, a path that no longer exists since `hardware/` moved to the repo root. Now points at the file's Git history instead, since `GPIO_Map.md` itself was updated to document Rev.5's pin-out rather than Rev.4's
* Added: persistent restart counter + refined restart reason (`hannah_net_get_restart_count()`/`hannah_net_get_restart_reason()`), survives reboots via NVS. Distinguishes the reactive network watchdog, a remote-triggered restart (#161/#162) and a post-OTA restart — `esp_reset_reason()` alone reports the same "Software" reason for all three, so a small NVS marker (`hannah_net_mark_restart_source()`) is set right before each conscious `esp_restart()` call and consumed on the next boot. Reported to Core via the extended `firmware`-topic payload (Refs #165)

## 0.64.1 (2026-07-26)
### AutoDeploy
* Fixed: `post_install` hooks (`autodeploy.yaml.example`, `autodeploy`/`core`/`voiceid` components) now run `pip install --upgrade -q -r requirements.txt` instead of plain `pip install -q -r requirements.txt` — the latter leaves an already-installed package untouched even when a newer version is actually required (e.g. a transitive floor bump like #163's `protobuf>=7.35.1`), so `hannah-core` deployed and restarted straight into a crash loop after v0.64.0 with no way to notice short of watching logs. `--upgrade` always resolves to the latest version satisfying declared constraints instead of "whatever already happens to satisfy the old one" (Refs #164)

### Hannah Core
* Changed: `numpy` floor capped at `<3` (`numpy>=1.24.0,<3`) — needed now that AutoDeploy deploys with `--upgrade`, to stop an unbounded floor from silently permitting a future breaking major version jump (numpy 2.0 had real breaking changes: ABI, removed APIs) (Refs #164)

### VoiceID
* Changed: same `numpy<3` cap as Hannah Core, same reason (Refs #164)

## 0.64.0 (2026-07-26)
### Hannah Core
* Added: `mqtt_handler.publish_restart()` + `TriggerSatelliteRestart` gRPC handler, wired up analogous to the existing `TriggerFirmwareUpdate` — publishes to the satellite's new `.../restart` MQTT topic. `requirements.txt`/`requirements-test.txt` bumped to `hannah-proto>=0.5.6`, which carries the RPC (Refs #161)
* Fixed: `grpcio` floor raised to `>=1.83.0` — `hannah-proto>=0.5.6`'s generated gencode requires it; the previously-documented `>=1.82.1` hard-failed at import (Refs #163)

### Satellite Firmware
* Added: `hannah/satellite/{device}/restart` MQTT topic — triggers an ordered `esp_restart()` (same path as the existing network watchdog: TWDT deregister first, so the shutdown handler chain incl. `persist_log_to_flash()` runs instead of a hard panic reset). Diagnostic/rejuvenation tool for the resource-exhaustion suspicion from #150, usable even when OTA/webserver are already unresponsive, since MQTT stays alive in that failure mode (Refs #161)

## 0.63.0 (2026-07-24)
### Satellite Firmware
* Added: PCB Rev.5 firmware build (`sdkconfig.defaults.rev5`, `build:esp32:rev5`/`upload:esp32:rev5`/`upload:esp32:rev5:init`/`publish:esp32:rev5` CI jobs, own update-server channel `satellite-esp-stable-rev5`) — 4× PDM mics via ADAU7118 PDM→TDM converter (new `HANNAH_MIC_TYPE_TDM` Kconfig option), USB-C removed for 5V/GND solder pads, repositioned buttons/status LED/SD card. ADAU7118 register init is a placeholder pending its datasheet — TDM audio path compiles and runs but isn't tuned yet. Rev4 build unchanged (Refs #160)
* Fixed: `ota_channel` was written to NVS unconditionally on every settings save (web UI form always submits the field), so a new firmware build's `CONFIG_HANNAH_OTA_CHANNEL` compile default silently never took effect on already-provisioned devices. Added a one-time NVS migration in `hannah_config_init()` (Refs #160)
* Changed: `sdkconfig.defaults.rev4` now explicitly sets `CONFIG_HANNAH_OTA_CHANNEL="satellite-esp-stable-rev4"`, moving Rev4 devices onto their own channel ahead of Rev5 taking over `satellite-esp-stable` (Refs #160)

## 0.62.0 (2026-07-19)
### Hannah Core
* Added: per-satellite setting to keep the microphone open after a Smalltalk answer (`smalltalk_followup_listen` on the `satellites` table, `SatelliteManager.set_satellite_smalltalk_followup`/`get_satellite_smalltalk_followup`) — reuses the existing `hannah/satellite/{device}/listen` MQTT trigger (#18) and the satellite firmware's existing 8s listen-after-TTS window, so no new timeout logic was needed in Core (Refs #158)
* Added: `ConversationContext.is_addressed_to_hannah()` stub (always True for now) — seam for a future LLM-based relevance check on utterances heard during an open Smalltalk follow-up window (Refs #158)
* Added: `SetSatelliteSmalltalkFollowup` gRPC RPC and `smalltalk_followup_listen` on `GetSatellites` — exposes the new per-satellite setting so the WebUI can eventually surface a toggle (`hannah-proto` bumped to 0.5.4) (Refs #158)

## 0.61.0 (2026-07-17)
### Hannah Core
* Added: satellite firmware version/update-available state is now persisted on the `Satellite` model (`satellites` table: `firmware_version`, `update_available`, `new_version`) instead of only living in a volatile in-memory dict — `GetSatellites` now reports the current firmware state directly, and the external `SubscribeEvents`/`satellite.firmware` event now also carries `update_available`/`new_version` (previously only visible to the ioBroker adapter over `AgentConnect`) (Refs #157)

## 0.60.12 (2026-07-16)
### Satellite Firmware
* Removed: PCB Rev2 firmware build and its update-server channel (`satellite-esp-rev2`) — Rev2 boards had a dimension error and were only ever used for electrical testing, never deployed; Rev4 is the only hardware in use
* Changed: `noise` capture sample_type auto-flush interval increased from 5s to 50s (`NOISE_AUTOFLUSH_FRAMES`) — longer ambient/noise sample segments for the Voice Collector (Refs #156)

## 0.60.11 (2026-07-16)
### Hannah Core
* Fixed: `TriggerPlink` enabled virtual PTT (recording start) after a guessed fixed sleep following the plink tone, which didn't account for real playback pipeline latency — the plink tone bled into the start of every hey-hannah wakeword sample recorded this way. Now waits for a `playback_done` ack from the satellite instead, falling back to the old guessed sleep if the satellite's firmware doesn't send one yet (Refs #155)
* Added: `mqtt_handler.reset_playback_done()` / `wait_for_playback_done()` — generic per-device wait for a satellite's `playback_done` MQTT ack (Refs #155)

### Satellite Firmware
* Added: `speaker_task` publishes `hannah/satellite/{device}/playback_done` once a playback (TTS/plink/asset) is fully drained through I2S — generic ack Core can wait on instead of guessing a fixed delay (Refs #155)

## 0.60.10 (2026-07-16)
### Hannah Core
* Fixed: `deploy/install.sh` installed to `/opt/hannah-core`, but `hannah.service`'s `WorkingDirectory`/`ExecStart` expect `/opt/hannah/core` — the service would fail to start after a fresh install. Now installs to `/opt/hannah/core`, matching the service unit
* Fixed: `deploy/install.sh` was tracked in git without the executable bit — `curl | bash` worked regardless, but running the extracted script directly (e.g. `--uninstall`) failed with `Permission denied`

### Hannah Proxy
* Fixed: `deploy/install.sh` was tracked in git without the executable bit, same fix as Core

### Telegram
* Fixed: `deploy/install.sh` installed to `/opt/hannah-telegram`, but `hannah-telegram.service` expects `/opt/hannah/telegram` — same fix as Core
* Fixed: `deploy/install.sh` was tracked in git without the executable bit, same fix as Core

### VoiceID
* Fixed: `deploy/install.sh` installed to `/opt/hannah-voiceid`, but `hannah-voiceid.service` expects `/opt/hannah/voiceid` — same fix as Core
* Fixed: `deploy/install.sh` and `deploy/install-macos.sh` were tracked in git without the executable bit, same fix as Core

### AutoDeploy
* Fixed: `deploy/install.sh` and `deploy/install-macos.sh` were tracked in git without the executable bit, same fix as Core

## 0.60.9 (2026-07-16)
### Hannah Core
* Fixed: `deploy/install.sh` no longer hard-fails when `UPDATE_SERVER_TOKEN` is unset — the Update Server allows anonymous access to `public_read` channels, so the token is only required for non-public ones (Refs #152)
* Added: `deploy/install.sh` now checks it's running as root before attempting any privileged operation, instead of failing partway through with a confusing permission error (Refs #153)

### Hannah Proxy
* Fixed: `deploy/install.sh` no longer hard-fails when `UPDATE_SERVER_TOKEN` is unset, same fix as Core (Refs #152)
* Added: `deploy/install.sh` now checks it's running as root, same fix as Core (Refs #153)

### Telegram
* Fixed: `deploy/install.sh` no longer hard-fails when `UPDATE_SERVER_TOKEN` is unset, same fix as Core (Refs #152)
* Added: `deploy/install.sh` now checks it's running as root, same fix as Core (Refs #153)

### VoiceID
* Fixed: `deploy/install.sh` no longer hard-fails when `UPDATE_SERVER_TOKEN` is unset, same fix as Core (Refs #152)
* Added: `deploy/install.sh` now checks it's running as root, same fix as Core (Refs #153)
* Fixed: `deploy/install-macos.sh` still hard-failed without `UPDATE_SERVER_TOKEN` — missed in the original pass since it only touched the Linux scripts (Refs #153)

### AutoDeploy
* Fixed: `deploy/install.sh` no longer hard-fails when `UPDATE_SERVER_TOKEN` is unset, same fix as Core. `autodeploy.py` no longer requires a `token` key in its config and skips the `Authorization` header entirely when none is configured (Refs #152)
* Added: `deploy/install.sh` now checks it's running as root, same fix as Core (Refs #153)
* Fixed: `deploy/install-macos.sh` still hard-failed without `UPDATE_SERVER_TOKEN` — missed in the original pass since it only touched the Linux scripts (Refs #153)

## 0.60.8 (2026-07-15)
### Satellite Firmware
* Added: `hannah_net`'s `heartbeat_task` now logs free/minimum-ever heap size on every tick — diagnostic for suspected resource-exhaustion failures where a satellite keeps processing UDP heartbeats (no new allocation needed) while OTA/webserver (need fresh allocations) stop working (Refs #150)

### Hannah Core
* Fixed: `_on_ota_pending()` sent the available/target firmware version in `AgentFirmwareEvent.version` — a field that's supposed to always report the satellite's *current* version — instead of the last known current version, causing the adapter to display the not-yet-installed target version as if it were already running. Now sends the last known current version (already tracked from the satellite's own periodic firmware report) alongside `update_available=true` (Refs #151)

## 0.60.7 (2026-07-15)
### Satellite Firmware
* Fixed: network watchdog (`hannah_net`'s `heartbeat_task`) relied on `esp_wifi_sta_get_ap_info()` as a liveness signal during idle periods — this only queries the WiFi driver's internal association state, which can keep reporting "connected" during the exact "zombie" WiFi state the watchdog was built to catch (Refs #86), so the timeout never elapsed and the satellite stayed offline for days until a manual power cycle. Now uses the `heartbeat_ack` reply Proxy/Core already send for every heartbeat (previously received but silently ignored) as the liveness signal instead — a real, server-confirmed round trip (Refs #149)
* Changed: `HANNAH_NET_WATCHDOG_TIMEOUT_S` default raised from 120s to 240s, comfortably above the time a normal STA→AP-mode transition (`HANNAH_WIFI_MAX_RETRY` reconnect attempts) takes, so the watchdog doesn't preempt a legitimate reconnect (Refs #149)

## 0.60.6 (2026-07-14)
### Hannah Core
* Fixed: a trigger's `when` list with both a time condition and one or more state conditions as flat sibling entries (the format the WebUI's trigger editor actually saves) is now treated as time AND state, not as independent OR alternatives — previously the state condition(s) fired the trigger on every matching state change regardless of the time/day condition (Refs #147)

## 0.60.5 (2026-07-13)
### Hannah Core
* Fixed: `AlarmManager._compute_next_fire()` now correctly finds the next occurrence of a recurring alarm when today is the only configured weekday and its time has already passed — previously it returned `None` instead of the same weekday one week later (Refs #145)
* Fixed: `TriggerEngine._check_time_triggers()` now evaluates a condition's `also` field, matching `on_state_update()`/`match_phrase()` — a time-based trigger can now require an additional state match (`also`) directly instead of needing an unguarded second OR-condition that fired regardless of time/day (Refs #146)

## 0.60.4 (2026-07-12)
### Hannah Core
* Added: `GetDevices` now includes a `state_writable` map per device, sourced from the adapter's `AgentDevice.writable` (derived from ioBroker's `common.write`) — lets the WebUI exclude read-only states (e.g. sensors) from control actions (Refs #144)

## 0.60.3 (2026-07-12)
### Hannah Core
* Fixed: `TriggerEngine`'s state cache now persists to disk (`TRIGGER_STATE_CACHE_PATH`, default `trigger_state_cache.json`) and survives a Core restart — previously every state looked "unknown" after a restart, so `also`/`unless` conditions and delay-timer reconciliation silently guessed instead of using the real last-known value. A fresh ioBroker device snapshot now also seeds the cache directly, without firing any trigger (a snapshot is a reality-sync, not a state transition) (Refs #141)

## 0.60.2 (2026-07-11)
### Hannah Core
* Fixed: `RegisterProxy` no longer reverts UDP server + MQTT discovery to Hannah's own address the instant the proxy's gRPC stream ends — a 10s grace period now lets a quick proxy reconnect cancel the revert, preventing satellites behind the proxy from being repointed to an address they can't reach during a brief outage (Refs #140)

### Hannah Proxy
* Fixed: satellite-facing UDP server now stops when the `RegisterProxy` connection to Hannah Core is lost or never established, instead of silently accepting satellite traffic with no path to forward it to (Refs #140)
* Changed: reconnect log line now distinguishes "initial connection failed" from "lost an established connection" (Refs #140)

## 0.60.1 (2026-07-11)
### Hannah Proxy
* Fixed: `hannah-proto-go` bumped to 0.5.1 — 0.5.0 had a stale `ProtoVersion` (2 instead of 3) baked into the generated Go module, causing the proxy to fail Hannah Core's protocol-version check after upgrading to 0.5.0

## 0.60.0 (2026-07-11)
### Hannah Core
* **Breaking:** Routines are removed as a standalone concept — `RoutineManager`, the `routines` table, the `Routine` model, and the `GetRoutines`/`CreateRoutine`/`UpdateRoutine`/`DeleteRoutine` RPCs are gone. The WebUI routine editor (separate repo) stops working until it's updated there
* Added: Triggers get a new `when.phrase` condition type (`TriggerEngine.match_phrase()`) — covers the former Routines functionality (voice phrase → actions, checked synchronously before NLU, no cooldown), now as part of the unified Trigger system instead of a separate data model (Refs #139)
* Added: `core/deploy/migrate_routines_to_triggers.py` — one-off, manually run migration script that converts existing `routines` rows into equivalent `when.phrase` triggers (translates the old `{topic,value}` MQTT-publish actions into `{set_state}`, since the `hannah/set/devices/...` topic scheme is a 1:1 rename of the `javascript.0.virtualDevice.*` ioBroker path and is no longer used ioBroker-side)
* Changed: `hannah-proto` bumped to 0.5.0 (no functional difference — Core never called the removed Routine RPCs through the generic client)

### Hannah Proxy
* Changed: `hannah-proto-go` bumped to 0.5.0. No functional difference

### Telegram
* Changed: `hannah-proto` bumped to 0.5.0. No functional difference

## 0.59.0 (2026-07-11)
### Hannah Core
* Added: `IoBrokerClient`/`GetDevices` now carry a `state_type` (`BOOLEAN`/`NUMERIC`/`ENUM`/`COLOR`/`TEXT`) and, for `ENUM`/`COLOR` states, the allowed values per device state — sourced from the adapter's `state_type`/`enum_values` on `AgentDevice` (`hannah-proto` #117). Prep work for the WebUI trigger editor's dropdown-based condition UI (Refs #117)

## 0.58.1 (2026-07-10)
### Hannah Core
* Added: `set_automation` LLM tool (`tool_agent.py`) — the LLM-driven fallback path (smalltalk-lock, unrecognized intents) now also knows about enabled automations and can enable/disable them for the current user, independent of the deterministic NLU wordlist match. `ToolAgent` now threads `user_id` through `run()`/`_dispatch()` for this (Refs #138)
* Changed: `automations` settings wordlist for `telegram_autoresponder` extended with more phrasing variants ("automatische Antwort" singular, "automatisch antworten") (Refs #138)

## 0.58.0 (2026-07-09)
### Hannah Core
* Added: per-user enable/disable for external "automation" services (e.g. the Telegram auto-responder) — new `user_automations` table, `SetAutomation` RPC, `AutomationConnect` gRPC stream for the external service to register and receive live updates, and a new "Autoresponder" voice intent (`SetAutomation` in `nlu.py`, decoupled from the internal automation key via a new `automations` settings wordlist) (Refs #138)

## 0.57.0 (2026-07-09)
### Satellite Firmware
* Changed: `POST /nvs` whitelist (`NVS_ALLOWED_KEYS`) reworked for the adapter's upcoming wireless NVS write feature — `seed` dropped (the wireless path only ever targets already-paired, connected satellites, so re-pairing over this channel was never a real use case), `ota_token`/`asset_token` added (without them, secret rotation without physical access was impossible, since both were only writable via a full NVS partition flash) (Refs #136)

## 0.56.2 (2026-07-09)
### Satellite Firmware
* Fixed: `hannah_webserver`'s httpd server left `config.max_uri_handlers` at the ESP-IDF default of 8 while registering 11 routes — `httpd_register_uri_handler()` silently failed for the 9th+ route (`/log/clear`, `/nvs`, and as of today `/log/last`), so those endpoints returned a framework-level 404 with no indication anything was wrong. Raised to 16 and registration failures are now logged (Refs #135)

## 0.56.1 (2026-07-08)
### Satellite Firmware
* Fixed: v0.56.0's network watchdog (Refs #86) restarted satellites every `CONFIG_HANNAH_NET_WATCHDOG_TIMEOUT_S` (~2 min) even when perfectly healthy — its liveness signal (IP acquired / MQTT connected/data) only fires once per connection or on incoming commands, so idle satellites with no inbound MQTT traffic never refreshed it. `heartbeat_task` now also marks liveness every cycle whenever `esp_wifi_sta_get_ap_info()` still reports an association, closing the gap for the common idle-but-healthy case (this alone doesn't catch the original "truly zombie" case the watchdog targets, but the IP/MQTT signals still do)
* Fixed: `/log/last` never had any content — `heartbeat_task` is subscribed to the Task Watchdog Timer, and its own `esp_restart()` call (from the network watchdog) could take longer than the TWDT timeout to complete its orderly shutdown (WiFi/MQTT teardown, the log-flush shutdown handler), causing a hard TWDT panic-reset that skips the shutdown-handler chain entirely instead of a clean restart. Now unsubscribes from the TWDT (`esp_task_wdt_delete(NULL)`) immediately before the intentional restart

## 0.56.0 (2026-07-08)
### Satellite Firmware
* Added: `hannah_net`'s `heartbeat_task` now runs an active network watchdog — tracks time since the last confirmed network liveness signal (IP acquired, MQTT connected, or MQTT data received) and calls `esp_restart()` once `CONFIG_HANNAH_NET_WATCHDOG_TIMEOUT_S` (default 120s) is exceeded. Addresses a "zombie" WiFi state where `WIFI_EVENT_STA_DISCONNECTED` never fires, so the existing reactive reconnect logic in `on_wifi_event()` never triggers (Refs #86)
* Added: `heartbeat_task` now also subscribes itself to the Task Watchdog Timer (TWDT) and enables `CONFIG_ESP_TASK_WDT_PANIC`, so a hang of the task itself (not just a dead network) now triggers a reset too — previously TWDT only passively watched the idle tasks and only logged, never reset. Since TWDT has a single shared timeout, it's now raised to `CONFIG_HANNAH_HEARTBEAT_INTERVAL_S + 5s` to fit heartbeat's own feed cadence, which also loosens the idle-task hang-detection window from the previous 5s default to the same value (Refs #86)
* Added: `hannah_webserver` now persists the RAM log ring buffer to flash (`/assets/_hannah_last_log.txt` on the existing SPIFFS partition) via an `esp_register_shutdown_handler()` callback, so it survives any orderly `esp_restart()` (e.g. the new network watchdog above) instead of being lost with the RAM it lived in. Retrievable after reboot via new `GET /log/last`, linked from the `/log` page — no serial connection or PC-at-the-right-moment needed (Refs #86)
* Added: boot now logs `esp_reset_reason()` (Power-On/Panic/Watchdog/Brownout/…) once the webserver is up, so it lands in the log ring buffer (and thus `/log`/`/log/last`) instead of only going out over a UART nobody has connected. Lets you confirm after the fact whether a given restart actually came from the new network watchdog (Refs #86)

## 0.55.2 (2026-07-08)
### Hannah Core
* Fixed: `_on_agent_device_snapshot()` no longer calls `sync_rooms()` a second time on a device-derived, incomplete room list — a room with no currently-matching virtualDevice state (e.g. a room with only a satellite in it) was getting deleted (and any assigned satellite orphaned) on every reconnect, undoing the correct sync `_on_agent_room_snapshot()` had just done moments earlier (Refs #134)

## 0.55.1 (2026-07-08)
### Hannah Core
* Fixed: `handle_device_snapshot()` no longer lets a category-less sibling state (e.g. a power-meter state whose role/function doesn't resolve to anything) blank out a device's already-resolved category from another sibling state (e.g. the `on` state of a socket) — first non-empty category wins, regardless of processing order within the snapshot (Refs #133)

## 0.55.0 (2026-07-08)
### Hannah Core
* Changed: `core/hannah/models/` no longer carries its own hand-rolled mini-ORM (`base_module.py`/`query.py`) — replaced by the published `dialectorm-m1kad0` package (import name `pyorm`), the same code extracted and generalized into a standalone, dialect-aware (SQLite/Postgres/MySQL) library. Pure internal swap, no behavior change (Refs #132)
* Fixed: `SetSatelliteRoom` now pushes an `AgentSatelliteUpdate` to all connected adapters right away, instead of adapters only learning about a room reassignment on the affected satellite's next connect/disconnect event (Refs #109)
* Added: "Wie viel Strom/Watt/Leistung braucht mein PC?" — sockets now have a Watt-based category answer (`_CATEGORY_STATES["socket"]`), gated to the new `power` query intent so it doesn't hijack plain on/off status queries. Requires a `power` entry in the instance's `iobroker.state_names` setting to actually receive the value (new installs get it seeded by default now) (Refs #121)

## 0.54.0 (2026-07-05)
### Hannah Core
* Added: `GetTimers`/`DeleteTimer` unary RPCs — query/cancel a user's active timers independent of the `TimerConnect` stream (e.g. for a future "my timers" view), without needing to be the connected Timer Service. Bridges the async `TimerListResponse` push to a synchronous unary call via a one-shot waiter queue (Refs #97)
* Added: `SetTimer` now attaches `metadata["user_id"]` when creating a timer, same attribution chain already used for alarms (#4) but without the system-user fallback: Voice-ID-resolved speaker → satellite's assigned owner → left unset if neither resolves. Lets `GetTimers` actually filter by user instead of always returning an empty list (Refs #97)
* Changed: `hannah-proto` bumped to v0.3.7 for the new `Timer`/`GetTimersRequest`/`GetTimersResponse`/`DeleteTimerRequest` messages (`timer_admin.proto`)

### Telegram
* Fixed: `SubscribeEvents` never actually sent the `x-proto-version` metadata despite `ProtocolVersionClientInterceptor` being wired up — `grpc.aio`'s `UnaryStreamClientInterceptor` doesn't reliably apply metadata mutations for streaming calls, unlike unary-unary (`SubmitText`/`SubmitVoice` were unaffected). Now passed explicitly at the call site instead of relying on the interceptor (Refs #60)

## 0.53.2 (2026-07-05)
### Satellite Firmware
* Changed: `audiolib` is now consumed via the ESP-IDF Component Registry (`nurpech/audiolib`, ≥0.2.3) instead of a git submodule — `hannah_audio` declares it in its own `idf_component.yml`, same pattern already used for `espressif/cjson`/`espressif/mqtt`/etc. `EXTRA_COMPONENT_DIRS` and the `audiolib` submodule removed (Refs #130)

## 0.53.1 (2026-07-05)
### Hannah Core
* Changed: `proto` submodule bumped to `hannah-proto` v0.3.3 (`PROTO_VERSION` 1 → 2) — the only change is the Go package moving to the public `github.com/NurPech/hannah-proto-go`; no message/schema changes. `PROTO_VERSION` copies in `core`, `telegram`, `proxy` updated to match (Refs #60)
* Changed: `proto` submodule bumped to `hannah-proto` v0.3.4 — npm package tooling switched to `ts-proto` codegen; no `.proto`/schema changes, `PROTO_VERSION` unchanged at 2 (Refs #60)
* Changed: `proto` submodule bumped to `hannah-proto` v0.3.5 — fixes the pip package's `hannah_pb2` re-export patch (see `hannah-proto`!30/#125); no `.proto`/schema changes, `PROTO_VERSION` unchanged at 2 (Refs #60)
* Changed: switched from locally generating proto stubs off the `proto` git submodule to depending on the published `hannah-proto` PyPI package. `core/hannah/proto/` (generated stubs) removed; `scripts/gen_proto.sh` removed entirely (Core was its last consumer). Third and last of the three components originally in scope for this migration, see #60

### Telegram
* Changed: switched from locally generating proto stubs off the `proto` git submodule to depending on the published `hannah-proto` PyPI package. `telegram/hannah_telegram/proto/` (generated stubs) removed; `scripts/gen_proto.sh` no longer generates Telegram stubs. Second component migrated off the git-submodule pattern, see #60

### Hannah Proxy
* Changed: switched from locally generating proto stubs off the `proto` git submodule to depending on the published `github.com/NurPech/hannah-proto-go` Go module directly. `proxy/proto/hannah/` (generated stubs) and `proxy/gen_proto.sh` removed — first component migrated off the git-submodule pattern, see #60

## 0.53.0 (2026-07-04)
### Hannah Core
* Added: `SetVolume` NLU intent — "stell die Lautstärke auf 50"/"lauter"/"leiser" now sets satellite volume (absolute or relative ±10), last remaining piece of the Volume/Mute refactor (v0.13.0, 2026-05-27) (Refs #63)
* Removed: dead `TimerManager` class in `core/hannah/timers.py` — unused since `SetTimer` moved to the external Timer Service. File renamed to `alarms.py` since only `AlarmManager` (+ `format_duration`) remains (Refs #128)
* Added: gRPC server interceptor checking the `x-proto-version` metadata on every RPC (unary and streaming) against the local `PROTO_VERSION` (from the `proto` submodule, now bumped to `hannah-proto` v0.3.0). Defaults to log-only (`grpc.enforce_protocol_version: false`); hard-reject mode (`FAILED_PRECONDITION` on mismatch/missing metadata) is opt-in until every external client sends the header (Refs #60)

### Telegram
* Added: gRPC client interceptor attaching the local `x-proto-version` metadata to every outgoing call to Hannah Core (unary and streaming), so Core's protocol-version check (see above) can verify Telegram is on a compatible proto schema (Refs #60)

### Hannah Proxy
* Added: gRPC client interceptors attaching the local `x-proto-version` metadata to every outgoing unary and streaming call to Hannah Core. Proxy ships as a single compiled binary with no filesystem access to the `proto` submodule, so the version is embedded at build time via `go:embed` from a local copy in `proto/hannah/` (Refs #60)

## 0.52.0 (2026-07-04)
### Hannah Core
* Changed: `proto` submodule bumped to `hannah-proto` v0.2.0 — `TimerCreate`/`TimerInfo` now carry a generic `metadata` map instead of fixed `room`/`roomie_id` fields, and `TimerFired` echoes `metadata` back. Hannah Core's own `HannahTimerStore` (SQLite) removed — was a redundant duplicate of what the Timer Service already persists; announcement routing now reads `room`/`roomie_id` straight from the echoed `metadata` instead. `trigger_engine`'s `"trigger:"` label-prefix hack for delay-timers replaced with a proper `metadata["trigger_id"]` key (Refs #127)

### Telegram
* Changed: generated proto/gRPC stubs regenerated following the `hannah-proto` v0.2.0 bump (Timer Service `metadata` map). No functional change for Telegram itself — it shares the same generated `hannah_pb2`/`timer_service_pb2` modules as Core but doesn't use the Timer Service RPCs (Refs #127)

## 0.51.7 (2026-07-04)
### Telegram
* Fixed: `init_commands()`'s default-scope `set_my_commands` call had no error handling — a Telegram flood-control error (`RetryAfter`, e.g. from rapid restarts) crashed the whole service on startup instead of just skipping that one call. Now caught and logged, same pattern already used for the per-chat `set_my_commands` calls just below it

## 0.51.6 (2026-07-03)
### Hannah Core
* Added: regression test (`core/tests/test_proto_reexport.py`) walking every scope-split `*_pb2` module and asserting nothing is missing from `hannah_pb2` — guards against the class of bug fixed in Telegram below (Refs #125)

### Telegram
* Fixed: `hannah_telegram.proto.hannah_pb2` was missing every scope-split message (`EventFilter` and others) — `telegram/hannah_telegram/proto/__init__.py` never got the re-export patch that `core/hannah/proto/__init__.py` received in #44, so it stayed empty. Service crash-looped on every `subscribe_events` call (`AttributeError: module ... has no attribute 'EventFilter'`) (Refs #125)

## 0.51.5 (2026-07-03)
### Hannah Core
* Changed: Proto schema extracted into its own repo, [hannah-proto](https://dev.kernstock.net/gessinger/voice/hannah-proto) (history-preserving subtree split from `core/proto/`), consumed as a Git submodule (`proto/` at repo root) instead of being manually copied into each consumer. `scripts/gen_proto.sh` now reads from the shared submodule path instead of separate `core/proto`/`telegram/proto` copies (Refs #43)

### Hannah Proxy
* Changed: `proxy/gen_proto.sh` reads proto sources from the shared `../proto` submodule instead of its own local copy (Refs #43)

### Telegram
* Changed: `telegram/proto/` (manually copied proto sources) removed — codegen now reads from the shared `proto/` submodule (Refs #43)

## 0.51.4 (2026-07-03)
### Hannah Core
* Changed: `core/proto/hannah.proto` (1241 lines, ~80 messages) split by scope into 12 separate `.proto` files (`shared`, `user_registry`, `control`, `car_state`, `event_stream`, `satellite_proxy`, `device_control_menu`, `satellite_provisioning`, `speaker_enrollment`, `agent`, `wakeword_capture`, `timer_service`), linked via `import`; `hannah.proto` itself now only holds the header/imports and the single `service HannahService` (unchanged, no service split — the codegen footprint reduction that would require doesn't pay off for the current all-backend consumer set, see #44). `scripts/gen_proto.sh`/`core/proto/gen_proto.sh` updated to pass all `.proto` files to `protoc` (it doesn't follow imports transitively for codegen) and to patch relative imports across every generated `*_pb2*.py`, not just `hannah_pb2_grpc.py`. Python (unlike Go/TS) keeps each file's generated messages in that file's own module instead of re-exporting them into `hannah_pb2` — `core/hannah/proto/__init__.py` now patches every scope module's public names onto `hannah_pb2` so existing `pb.AgentDevice`/`pb.ResidentType.ROOMIE`-style call sites across `grpc_server.py`/`iobroker.py`/`residents_manager.py` keep working unchanged (Refs #44)

### Hannah Proxy
* Changed: `proxy/gen_proto.sh` fixed (stray unmatched quotes broke argument parsing, wrong path assumption after a WIP edit) and updated for the multi-file proto split — lists all 13 files explicitly with a per-file Go package mapping. Switched from `--go_opt=paths=source_relative` to the more robust `module=` pattern (output path derived from the Go module root, independent of source layout), unifying with the Hannah Timer Service's equivalent script. `proxy/Makefile`'s now-redundant `proto` target removed — `gen_proto.sh` is the one tool, matching Timer Service (no Makefile there either) (Refs #44, #45)

### Telegram
* Changed: `telegram/proto/` synced to the split scope files (was stale — missing `Alarm`/`SetSatelliteOwner`/`DeleteSatellite`, still had the removed `CreateSetting`/`DeleteSetting`) (Refs #44)

## 0.51.3 (2026-07-03)
### Hannah Core
* Added: `Car` (proto + `Car` model, `core/hannah/models/car.py`) now has its own `name` field for the display name, analogous to `Satellite.display_name` — previously the WebUI showed the technical `topic_prefix` as the card title for lack of a dedicated display-name field. `CreateCarRequest`/`UpdateCarRequest`/`Car` (gRPC) and `CarRegistry.create_car`/`update_car` (`core/hannah/car_registry.py`) extended accordingly; `topic_prefix` remains the technical MQTT key, unchanged. Additive `ALTER TABLE` migration for existing `cars` tables in `core/hannah/utils/db.py` (Refs #123)

## 0.51.2 (2026-07-02)
### Hannah Core
* Added: Helligkeits-/Illuminance-Kategorie (`illuminance_sensor`) wieder in `_CATEGORY_STATES` (`core/hannah/iobroker.py`) ergänzt — kategorienweite Abfragen wie "wie hell ist es im Wohnzimmer" funktionieren wieder (Einheit `lx`, State-Suffix `illuminance` war bereits vorhanden). Passende `category_words`-Einträge `helligkeit`/`lux` in `DEFAULT_NLU_SETTINGS` (`core/hannah/settings_manager.py`) ergänzt (Refs #120)

## 0.51.1 (2026-07-02)
### Hannah Core
* Fixed: `main.py` crashed on startup (`TypeError: HannahServicer.__init__() got an unexpected keyword argument 'create_setting'`) — v0.51.0's `CreateSetting`/`DeleteSetting` removal (#115) only got `delete_setting` cleaned out of the `HannahServicer(...)` call site, `create_setting=settings_manager.create_setting` was left behind (Refs #118)

## 0.51.0 (2026-07-02)
### Hannah Core
* Fixed: `AlarmManager`'s ringing loop (`play_asset` for `alarm_ring`) was completely fire-and-forget — a satellite that couldn't play the sound (asset not cached, e.g. wrong asset-server namespace tagging) never told Core, so the alarm rang silently forever with no audible feedback at all. Satellites now report back per-attempt success/failure over a new `hannah/satellite/{device}/play_asset/result` MQTT topic (`MQTTHandler.set_play_asset_result_handler`); `AlarmManager.on_play_result()` switches the ringing loop from the (broken) asset sound to a repeated TTS announcement on the first nack for that device, so the alarm stays audible instead of going completely silent (Refs #116)
* Added: `BleTag.user`/`Car.owners` properties (lazy, DB-backed) return the related `User` object(s); reverse `User.ble_tags`/`User.cars` properties mirror them — same lazy-loading pattern as the existing `User.linked_accounts`/`User.satellites` (Refs #115)
* Changed: `ble.tags`/`cars` moved out of the generic Settings system into their own DB models — new `BleTag` (`mac_address`/`label`/`user_id`) and `Car` (`topic_prefix`/`home_address`) tables, plus a `user_to_car` n:n pivot table for Car ownership (a Car can have multiple owners, keyed on Hannah's own `users.id` instead of the old free-form `owner_roomies` string list). New `hannah.ble_tags.BleTagManager` and `hannah.car_registry.CarRegistry` provide CRUD, wired to new gRPC RPCs `GetBleTags`/`CreateBleTag`/`UpdateBleTag`/`DeleteBleTag` and `GetCars`/`CreateCar`/`UpdateCar`/`DeleteCar` (no consumer in this repo yet — WebUI is out of scope, see #12). `car_tracker.py`'s live MQTT tracking is unchanged (still matches by Roomie-ID); `main.py` translates the new Owner-User-IDs to Roomie-IDs at startup via the existing Hannah-User ↔ Residents `linked_accounts` link. `BleLocationEngine` no longer needs a `UserManager` reference — tag ownership now arrives pre-resolved as `user_id` instead of a `username` string. New one-time migration `core/deploy/migrate_settings_to_models.py` moves any already-migrated `ble.tags`/`cars` Settings rows into the new tables; `migrate_config_settings.py` (for installs migrating a `config.yaml` for the first time) now targets the new tables directly for these two instead of the generic Settings table (Refs #115)
* Removed: `CreateSetting`/`DeleteSetting` gRPC RPCs — with `ble.tags`/`cars` moved to their own models (above) and `nlu`/`iobroker.state_names`/`llm.system_prompt` always pre-known/auto-seeded (below), there was no remaining legitimate use case for creating an arbitrary new Settings row. `GetSettings`/`UpdateConfig` (read/update existing values) are unaffected (Refs #115)
* Added: `SettingsManager.seed_defaults()` now also seeds `llm.system_prompt` with `""` when the `llm` category is empty — safe no-op (`llm.py`'s `if system_prompt:` guard skips the persona prompt on an empty string, no error) that removes the last case where a fresh DB needed a Settings row nobody had created yet (Refs #115)
* Added: `SettingsManager.seed_defaults()` — populates the `nlu` and `iobroker` (`state_names`) DB-Settings categories with generic, install-independent defaults on every startup, but only if the category is still completely empty (never overwrites migrated or admin-edited values). Fixes a regression from the `config.example.yaml` trim in 0.50.1: `nlu.py`'s `turn_on_words`/`turn_off_words`/`query_words` have no code-level fallback (unlike `category_words`), and `iobroker.py`'s built-in `state_names` default only covers `on`/`level`/`color`/`colorTemp`, not the sensor suffixes — a fresh install with an empty DB and the trimmed example silently lost TurnOn/TurnOff/Query intent detection and sensor live-updates. `core/deploy/migrate_config_settings.py` is unchanged, still used for migrating an existing customized `config.yaml` (Refs #114)
* Fixed: `config.example.yaml`'s `iobroker.state_names` comment named a nonexistent DB category `"iobroker.state_names"` — the actual category is `"iobroker"` with a setting named `"state_names"` inside it (Refs #114)

### Satellite Firmware
* Added: `hannah_asset_play()` now returns `bool` (previously `void`) — the result is reported back to Core over a new `hannah/satellite/{device}/play_asset/result` MQTT message via a new `hannah_asset_set_play_result_callback()`, wired in `main.c` alongside the existing `play_asset` command callback. Previously a missing/corrupt cached asset only logged a local `ESP_LOGW`, invisible to Core (Refs #116)

## 0.50.1 (2026-07-02)
### Hannah Core
* Changed: `config.example.yaml` trimmed to the settings that actually stay YAML-only — `nlu.*` (word lists), `llm.system_prompt`, `ble.tags`, `cars` and `iobroker.state_names` are DB-Settings now (`migrate_config_settings.py`, #27 Phase 5); `config.yaml` keeps infra/bootstrap config (connections, credentials, paths)
* Removed: `core/routines.yaml`/`core/triggers.yaml` — dead since routines/triggers moved to the DB-backed `Routine`/`Trigger` models (`migrate_triggers_routines.py`, #27 Phase 4); nothing in the codebase loaded them anymore

## 0.50.0 (2026-07-02)
### Hannah Core
* Added: recurring alarm clock ("Wecker"), fully rebuilt on top of a new DB-backed `Alarm` model/`AlarmManager` (replaces the old JSON-file-backed, one-shot-only `AlarmManager`). Voice support for setting ("stelle einen Wecker für Montag 8 Uhr", with a Mon-Fri recurring follow-up question for a single weekday), deleting ("lösche meinen Wecker für morgen 8 Uhr" — matches across all satellites, not just the one it's bound to; deleting one occurrence of a recurring series asks whether to delete the whole series), querying ("welche Wecker habe ich"), and stopping a currently-ringing alarm via the existing generic `StopIntent` ("Stopp"). Ringing plays a looping `alarm_ring` asset with alternating volume via MQTT until stopped, then restores the satellite's prior volume. New `Alarm` gRPC message + `GetAlarms`/`CreateAlarm`/`UpdateAlarm`/`DeleteAlarm` RPCs for the future WebUI's alarm management (no consumer in this repo yet). `pending_clarification` (used for the Mon-Fri yes/no follow-ups) gained a `kind`/`payload` discriminator, backward-compatible with the existing room-disambiguation flow (Refs #4)

## 0.49.1 (2026-07-01)
### Hannah Core
* Fixed: `BaseModel.create()`/`update()` only re-encoded `__json_fields__` columns as JSON when the value was a `list`/`dict` (`isinstance` check), not based on `__json_fields__` itself — a scalar value (e.g. `llm.system_prompt`, a plain string) written through `UpdateConfig`/`CreateSetting` landed in the DB unencoded and crashed the next `GetSettings`/`UpdateConfig` call with `JSONDecodeError`, taking down the WebUI's Settings page entirely. Both methods now check `key in __json_fields__` instead (Refs #113)

## 0.49.0 (2026-07-01)
### Hannah Core
* Changed: `LinkAccount` gRPC handler now validates the provider against a known set (`residents`, `telegram`, `microsoft`) and rejects duplicate account links (same `service`+`account_id` already linked to a different user) with `ALREADY_EXISTS`. `UnlinkAccountRequest` gains an optional `requestor_id` field — when set, Core enforces that the requestor is either the target user themselves or holds trust level 10; `requestor_id=0` (default) bypasses the check for internal/system callers (Refs #112)

### Telegram
* Changed: `/verknuepfen` command removed — unlinked users now receive a link to the Hannah WebUI instead. WebUI URL is configurable via `webui_url` in `config.yaml` (Refs #112)

## 0.48.2 (2026-07-01)
### Hannah Core
* Added: `SetSatelliteRoom`/`SetSatelliteDisplayName`/`SetSatelliteOwner`/`DeleteSatellite` now enforce trust-level/ownership checks in `SatelliteManager` via a new `requestor_id` field on each request (proto, additive, no consumer in this repo yet — WebUI needs to start sending it). `DeleteSatellite`/`SetSatelliteOwner` require trust level 10; `SetSatelliteRoom`/`SetSatelliteDisplayName` require trust level 5 and ownership of the satellite (trust level 10 is unrestricted). `requestor_id` omitted (`None`) bypasses the check entirely, for internal/system callers. New `SatellitePermissionError`, raised by `SatelliteManager` and translated to `ok=False, message="forbidden"` in the gRPC layer. `SatelliteManager.get_satellite()` now returns the real `Satellite` model instead of a hand-built dict — fixes a latent bug in `DeleteSatellite` that treated the previous dict as an object (`sat.device_id`/`sat.room_id`, `AttributeError` on the actual dict, untested until now) (Refs #111)

### Firmware (satellite-esp)
* Added: `hannah_ble` (BLE scanner) can now be enabled/disabled per build via new Kconfig setting `HANNAH_BLE_ENABLED` (default `y`) — NimBLE includes and the full implementation in `hannah_ble.c` sit behind the guard, with no-op stubs for `hannah_ble_init()`/`hannah_ble_set_watchlist_json()` when disabled (same pattern as `hannah_sd`). Groundwork for Satellite-Light variants with tight internal RAM (Refs #67)

## 0.48.1 (2026-06-30)
### Hannah Core
* Added: `DeleteSatellite` gRPC RPC — removes a satellite from the database via `SatelliteManager.delete_satellite()`; pushes a `satellite_deleted` event to the adapter so stale ioBroker object trees are cleaned up immediately

## 0.48.0 (2026-06-30)
### Hannah Core
* Changed: `RoomManager` split into `RoomManager` (rooms/groups only) and new `SatelliteManager` (provisioning, pairing, room/owner assignment, seed cleanup) — satellites no longer share a manager class with rooms/groups. `config.yaml`'s `room_manager.seed_ttl_days` moved to `satellite_manager.seed_ttl_days` (Refs #108)
* Added: satellites can now be assigned to a `User` ("Person") in addition to a room — new `satellites.owner_user_id` column, `Satellite.owner`/`Satellite.set_owner()` and `User.satellites` model properties. Groundwork for personalized announce routing — no gRPC/UI exposure yet (Refs #31)
* Added: `Announce` RPC accepts `room_id`/`user_id` in addition to the legacy `device` field — targets all satellites in a room, all satellites owned by a Person, or (if both set) only the satellite that's both, via new `SatelliteManager.get_room_satellite_ids()`/`get_user_satellites()`. New `SetSatelliteOwner` RPC and `Satellite.owner_user_id`/`owner_display_name` fields expose the #31 data model over gRPC — no WebUI exposure yet (WebUI is now a separate repository) (Refs #31)

## 0.47.0 (2026-06-29)
### WebUI
* Removed: `webui/` extracted into its own repository (`gessinger/voice/hannah-webui`) — no longer part of this monorepo. `test:webui`, `upload:webui` and the container-build jobs added in #105 (`build-container:webui:*`, `merge-manifests:webui`) are gone from this pipeline; equivalents now live in the new repo's own `.gitlab-ci.yml`. Fresh start there, no history carried over — see that repo's `CHANGELOG.md` for everything from here on (Refs #106)

### Hannah Proxy
* Fixed: Announcements an mehrere Satelliten liefen über denselben Proxy nacheinander statt gleichzeitig ab — `runProxyOnce`s einzige Receive-Goroutine rief `onPlayAudio` synchron auf, das wiederum `udp.Server.SendTTSChunk`s `time.Sleep`-Pacing blockierte, solange die Announcement des vorherigen Satelliten noch lief; `stream.Recv()` für andere Satelliten lief währenddessen nicht weiter. Neuer `playAudioDispatcher` (`internal/hannah/dispatcher.go`) verteilt `PlayAudioCommand`-Chunks pro `device_id` auf je eine eigene gepufferte Queue + Worker-Goroutine — Reihenfolge bleibt pro Gerät strikt FIFO, verschiedene Geräte spielen jetzt parallel (Refs #49)

## 0.46.1 (2026-06-28)
### WebUI
* Fixed: random logout on almost every click — `create_app()` set `app.secret_key = os.urandom(24)`, generating a new key on every call. Gunicorn runs without `--preload`, so each of its 2 worker processes imports `wsgi.py` and calls `create_app()` independently, ending up with a different secret key per worker; whichever worker didn't sign a given session cookie rejects it, dropping the user back to `/login`. `secret_key` is now read from `config.yaml` (or `HANNAH_WEBUI_SECRET_KEY`), stable across workers and restarts; falls back to a random key with a warning log if unset (Refs #104)
* Added: `hannah_webui/config.py`'s `load()` now falls back to environment variables (`HANNAH_WEBUI_HOST`/`PORT`/`SECRET_KEY`/`GRPC_HOST`/`GRPC_PORT`) when no `config.yaml` file is present, in preparation for a future containerized deployment (Refs #104)
* Changed: `deploy/hannah-webui.service`'s gunicorn now binds `0.0.0.0:5000` instead of `127.0.0.1:5000` — the previous bind made the service unreachable from outside its own host (Refs #104)
* Changed: deploy now runs as the shared `hannah` user instead of the dedicated `hannah-webui` user, matching how it's actually run in production (`core` already used `hannah`). Also adds `Environment=HOME=/opt/hannah/webui` (same pattern already used by `voiceid`) since `ProtectHome=true` makes the shared user's real `/home/hannah` invisible to the service, which made `grpc`'s C-core fail with `Permission denied` during gunicorn's worker fork handling (Refs #104)

### Telegram
* Changed: deploy now runs as the shared `hannah` user instead of the dedicated `hannah-telegram` user, matching how it's actually run in production (Refs #104)

### Hannah Proxy
* Changed: deploy now runs as the shared `hannah` user instead of the dedicated `hannah-proxy` user, matching how it's actually run in production (Refs #104)

### VoiceID
* Changed: deploy now runs as the shared `hannah` user instead of the dedicated `hannah-voiceid` user, matching how it's actually run in production (Refs #104)

## 0.46.0 (2026-06-28)
### Hannah Core
* Removed: old in-process WebUI (`hannah/webui.py`, `hannah/webui_templates/`) — fully superseded by the standalone `webui/` service (#27). `main.py` no longer spawns the Flask dev-server thread, `config.yaml`'s `web_ui` section is gone. `flask` dropped from `requirements.txt`; `werkzeug` (used directly for password hashing in `db.py`/`user_manager.py`/`grpc_server.py`, previously only pulled in transitively via `flask`) is now an explicit dependency (Refs #27)

### WebUI
* Added: initial `webui/` service skeleton — Flask app, synchronous gRPC client (`HannahClient`, analog to `telegram/hannah_telegram/grpc_client.py` but without `grpc.aio`, since Flask handles requests synchronously), and a `Login` flow against Core's existing `Login` RPC (#27 Phase 3). Proof-of-concept for the full request chain (Flask → session → gRPC → Core → template) ahead of the real Personal/Admin pages; no CI stages, deployment scripts or tests yet (Refs #94, #27)
* Added: Räume/Gruppen-Verwaltung (Admin) — `/rooms` (read-only list) and `/groups` (full CRUD: anlegen, umbenennen, Räume zuweisen, löschen), ported from the old in-process `core/hannah/webui.py`/`webui_templates/` onto the new `HannahClient` (`GetRooms`/`GetGroups`/`CreateGroup`/`UpdateGroup`/`DeleteGroup`/`SetGroupRooms` RPCs, already shipped in #88). First real admin page beyond the #94 skeleton; missing dependency `protobuf` (needed by the generated `*_pb2.py` stubs, not pulled in transitively by `grpcio`) added to `webui/requirements.txt` (Refs #27)
* Added: Satelliten-Verwaltung (Admin) — `/satellites`, listet alle bekannten Satelliten (DB + Live-Status bereits serverseitig gemerged via `GetSatellites`, #89) und erlaubt Anzeigename/Raum-Zuweisung. Kein Löschen-Button, da #89 keine `DeleteSatellite`-RPC eingeführt hat — anders als die alte In-Process-Version, die direkt `RoomManager.delete_satellite()` aufrief (Refs #27)
* Added: `webui/tests/` — erste rudimentäre Test-Suite (10 Tests, pytest + Flask-Testclient), deckt Login, Räume/Gruppen-CRUD und Satelliten-Verwaltung ab. `FakeHannahClient` ersetzt den echten gRPC-Client durch eine In-Memory-Stand-in mit echten `hannah_pb2`-Messages (kein Netzwerk, keine echte Hannah Core nötig) — analog zum leichtgewichtigen Test-Pattern von `telegram/tests/` statt zu core's schwergewichtigerem Mock-Setup. Noch nicht in `.gitlab-ci.yml` verdrahtet (eigener Checklist-Punkt "CI-Stages für webui/"), aber lokal lauffähig via `PYTHONPATH=webui pytest webui/tests/ -v` (Refs #27)
* Added: Settings-Verwaltung (Admin) — `/settings`, listet alle Settings-Kategorien (`ble.tags`, `cars`, `nlu.*`, `llm.system_prompt`, `iobroker.state_names`, per #92 schon in `hannah.db` statt `config.yaml`) mit ihren Werten als editierbares JSON-Textarea (anlegen/bearbeiten/löschen über `GetSettings`/`UpdateConfig`/`CreateSetting`/`DeleteSetting`). Bewusst generisch statt mit Feld-spezifischen Formularen pro Kategorie — die Werte sind zu heterogen (Listen, verschachtelte Dicts, einzelner String bei `system_prompt`) und Validierungsdetails sind laut #27 noch offen. Keine Kategorie-Erstellung in der UI, da dafür keine RPC existiert (Kategorien kommen aus der einmaligen Migration, #27 sieht das nicht als Admin-Aufgabe vor). Neuer generischer Flash-Message-Block in `base.html` für Fehlermeldungen (z.B. ungültiges JSON), nutzbar auch von künftigen Seiten (Refs #27)
* Added: Routinen-Editor (Personal, No-Code) — `/routines`, volle CRUD-Verwaltung über `GetRoutines`/`CreateRoutine`/`UpdateRoutine`/`DeleteRoutine` (#91). Trigger-Phrasen als Textarea (eine pro Zeile statt JSON-Array), Aktionen als feste Zeilen mit Typ-Auswahl (Gerät setzen: Topic+Wert / Ansage: Text+Raum) statt rohem `actions_json` — damit bleibt das vom Issue geforderte "kein-Code"-Versprechen eingehalten, ohne JS für dynamisches Hinzufügen/Entfernen von Zeilen zu brauchen (Bearbeiten-Formular zeigt `vorhandene Aktionen + 2` Zeilen, Neu-Formular 3). Deckt nur "Routinen" ab, nicht die separat modellierten proaktiven "Trigger" (`triggers`-Tabelle, ioBroker-State-basiert) — die hat im #27-Checklist keinen eigenen Punkt und bräuchte einen eigenen Editor mit anderem Datenmodell (Refs #27)
* Added: User-Verwaltung (Admin) — `/users`, portiert von der alten In-Process-WebUI auf `CreateUser`/`UpdateUser`/`DeleteUser`/`GetResidents` (#98) plus die bereits bestehenden `GetUsers`/`LinkAccount`/`UnlinkAccount`/`SetTrustLevel`/`SetSystemMessages`-RPCs. `UpdateUser` deckt bewusst nur Stammdaten ab (Anzeigename/E-Mail/Typ/Aktiv/Passwort) — Trust-Level und System-Benachrichtigungen laufen weiterhin über ihre eigenen RPCs, nicht dupliziert (siehe Proto-Kommentar zu #98). Resident-Verknüpfung baut den `provider_payload`-JSON-String (`resident_type`+`roomie_id`) clientseitig genauso wie die alte In-Process-Version, da `LinkedAccount.provider_payload` weiterhin von `_resolve_roomie_id()`/Car-Tracker/Residents-Sync gelesen wird. `webui/proto/hannah.proto` war bei #98 nicht mitgepflegt worden (nur `core/`, `telegram/`, `proxy/`) — jetzt synchronisiert und Stubs neu generiert (Refs #27)
* Added: gunicorn-Deployment + systemd-Service für `webui/` — `webui/wsgi.py` baut die App einmalig beim Import (gunicorns `module:app`-Konvention lässt kein `--config`-Argument zu wie `main.py`s Flask-Dev-Server-Pfad; Config-Pfad kommt über `HANNAH_WEBUI_CONFIG`, Default `config.yaml`). `webui/deploy/hannah-webui.service` + `install.sh` folgen dem etablierten Muster von `core/deploy/`/`telegram/deploy/` (Update-Server-Download, venv, eigener Service-User, systemd-Hardening). Bewusst EIN konsistenter Pfad (`/opt/hannah/webui`) für `install.sh`s `INSTALL_DIR` und der Unit's `WorkingDirectory`/`ExecStart` — bei core/telegram/voiceid klaffen die beiden Pfade auseinander (`/opt/hannah-<name>` vs. `/opt/hannah/<name>`), separat getrackt (#100), hier bewusst nicht übernommen. Bind-Adresse (`127.0.0.1:5000`, 2 Worker) steht direkt im `ExecStart`, nicht in `config.yaml` — gunicorn bindet den Socket, bevor die WSGI-App überhaupt geladen wird (Refs #27)
* Added: Trigger-Editor (No-Code) — `/triggers`, Teil 2 von #101 (Teil 1 = Backend, v0.45.4). Sektionen "Wenn" (mehrere Zustand-/Uhrzeit-Bedingungen, fest ODER-verknüpft), "Und" (mehrere Zustandsbedingungen, UND/ODER wählbar), "Außer wenn" (mehrere Zustandsbedingungen, fest UND, einklappbar) und "Dann" (mehrere Aktionen: Ansage oder State setzen) — Zeilen-Builder-Pattern wie bei den Routinen, kein JS für dynamisches Hinzufügen. Da die Engine `also`/`unless` pro Wenn-Bedingung prüft (trigger_engine.py, OR-Branches), die No-Code-UI aber EINEN globalen "Und"/"Außer wenn"-Block abbildet, dupliziert `_attach_also_unless()` diesen Block auf jede Wenn-Zeile beim Speichern und `_extract_also_unless()` liest ihn beim Bearbeiten wieder zurück (alle Kopien sind identisch). `ask`+`on_response_json` bleiben als rohes JSON in einer "Erweitert"-Sektion, `cancel_when` ist in der UI nicht editierbar (kein Use-Case ohne Delay-UI). `webui/proto/hannah.proto` war noch nicht auf das `actions_json`-Feld aus #101 synchronisiert — jetzt nachgezogen (Refs #101)

## 0.45.4 (2026-06-28)
### Hannah Core
* Added: `trigger_engine.py`'s `when` akzeptiert jetzt ein Dict (Alt-Format, unverändert) oder eine Liste solcher Dicts (neu: ODER-verknüpft); `also` ("und") akzeptiert zusätzlich `{"op": "and"|"or", "conditions": [...]}` für explizite ODER-Verknüpfung (eine Plain-Liste bleibt wie bisher UND); neue `actions`-Liste (`say`/`set_state`, analog zu `Routine.actions`) ersetzt das bisherige Einzel-`say`, wenn gesetzt. Alt-Trigger ohne Migration weiter lauffähig. Backend-Teil von #101s No-Code-Editor für die WebUI (Teil 1 von 2 — WebUI-Seite folgt nach diesem Release) (Refs #101)
* Fixed: `BaseModel.create()`/`update()` quoteten Spaltennamen nicht — brach bei reservierten SQL-Wörtern wie der `triggers`-Tabellenspalte `"when"` (`sqlite3.OperationalError: near "when": syntax error`). Nie aufgefallen, weil `CreateTrigger`/`UpdateTrigger` bisher ausschließlich mit gemocktem `TriggerEngine` getestet wurden — aufgefallen beim Schreiben echter Engine-Tests für #101 (Refs #102)

### Hannah Proxy
* Changed: Proto-Datei für #101s neues `actions_json`-Feld auf `Trigger`/`CreateTriggerRequest`/`UpdateTriggerRequest` aktualisiert (Refs #101)

### Telegram
* Changed: Proto-Datei für #101s neues `actions_json`-Feld auf `Trigger`/`CreateTriggerRequest`/`UpdateTriggerRequest` aktualisiert (Refs #101)

## 0.45.3 (2026-06-27)
### Hannah Core
* Fixed: `UnlinkAccount` RPC reported `ok=True, message="entfernt"` without actually removing the link — the handler only checked whether the user exists but never called `user.unlink_account(request.service)` (`LinkAccount` correctly calls its counterpart when linking). Found while building the `/users` page in `webui/` (#27) (Refs #99)

## 0.45.2 (2026-06-27)
### Hannah Core
* Added: `CreateUser`/`UpdateUser`/`DeleteUser`/`GetResidents` RPCs on `HannahServicer` — Phase 6 of #27's WebUI gRPC surface, übersehen bei der ursprünglichen Phasenplanung (1–5, #88–#92). `User`-Message additiv um `email`/`type` erweitert. Passwort kommt im Klartext über gRPC an und wird serverseitig gehasht — gleicher bereits akzeptierter Constraint wie beim `Login`-RPC (#90). `trust_level`/`system_messages` bleiben bei den bestehenden `SetTrustLevel`/`SetSystemMessages`-RPCs, nicht dupliziert. `HannahServicer` bekommt neuen `get_residents`-Callback, in `main.py` per Lambda verdrahtet (Forward-Reference auf `residents`, das erst nach der `HannahServicer`-Instanziierung entsteht — gleiches Muster wie `get_satellites` mit `grpc_servicer` selbst) (Refs #98, #27)

### Hannah Proxy
* Changed: Proto-Dateien aktualisiert für Phase 6 (User-CRUD, Residents) (Refs #98, #27)

### Telegram
* Changed: Proto-Dateien aktualisiert für Phase 6 (User-CRUD, Residents) (Refs #98, #27)

## 0.45.1 (2026-06-27)
### Hannah Core
* Fixed: `hannah.service` failed to start with `RuntimeError: ... depends on grpcio>=1.81.1` — `grpc_tools.protoc` bakes the locally-installed grpcio-tools version into the generated `_grpc.py` as a minimum runtime requirement, but `requirements.txt`'s old `grpcio>=1.60.0` floor didn't force an upgrade of an already-installed older grpcio on deploy. Raised the floor to `>=1.81.1` to match, and added a warning comment in `gen_proto.sh` so future stub regenerations keep grpcio-tools in step with this pin (Refs #93)

### Telegram
* Fixed: same `grpcio`/`grpcio-tools` version floor raised to `>=1.81.1`, for the same reason as Hannah Core (Refs #93)

## 0.45.0 (2026-06-27)
### Hannah Core
* Added: new unary gRPC RPCs `GetRooms`/`GetGroups`/`CreateGroup`/`UpdateGroup`/`DeleteGroup`/`SetGroupRooms` on `HannahServicer` — first phase of #27's planned WebUI gRPC surface. Pure wiring onto `RoomManager`'s existing methods (#77), no new business logic; no consumer yet, this just adds the server-side API surface ahead of the future standalone `webui/` service (Refs #88, #27)
* Added: `UserManager.login_user(username, password)` — verifies via `check_password_hash` against the stored hash, constant-time even for unknown usernames (checks against a dummy hash instead of short-circuiting). Prep work for #27 Phase 3's planned `Login` RPC, not wired into gRPC yet (Refs #27)
* Changed: `GetSatellites` RPC now returns every satellite known to `RoomManager`'s DB (not just currently-connected ones), with new `room_id`/`room_display_name`/`last_seen`/`connected`/`room_mismatch` fields — the "full status" merge logic (DB + live state + room-mismatch detection) that used to live only in-process in `webui.py`'s `/satellites` route moved into the RPC itself, since the future standalone `webui/` service won't have direct `RoomManager` access. Breaking change for existing consumers, intentionally — `iobroker.hannah` updated in the same step (Refs #89, #27)
* Added: `SetSatelliteRoom`/`SetSatelliteDisplayName` RPCs on `HannahServicer` — second phase of #27's WebUI gRPC surface, pure wiring onto `RoomManager`'s existing methods, same pattern as Phase 1's Rooms/Groups RPCs (Refs #89, #27)
* Added: `Login` RPC on `HannahServicer` — third phase of #27's WebUI gRPC surface, wires the already-prepared `UserManager.login_user()` to the existing `UserResponse` shape (same as `GetUser`); failed logins return `found=false` with gRPC `UNAUTHENTICATED` (Refs #90, #27)
* Added: `GetRoutines`/`CreateRoutine`/`UpdateRoutine`/`DeleteRoutine`/`GetTriggers`/`CreateTrigger`/`UpdateTrigger`/`DeleteTrigger` RPCs on `HannahServicer` — fourth phase of #27's WebUI gRPC surface. `RoutineManager`/`TriggerEngine` gain new CRUD methods (thin wrappers around `BaseModel.create/update/delete`, no new business logic) since they previously only supported read-only matching/runtime checks. `when`/`cancel_when`/`on_response`/`actions` stay JSON-encoded string fields in the proto rather than structured messages — both are deliberately open-ended/union-shaped, modeling them rigidly would force a proto change on every new trigger condition kind (Refs #91, #27)
* Fixed: a state-based trigger created via the new `CreateTrigger`/`UpdateTrigger` RPCs would never fire until the next ioBroker-adapter reconnect, because the adapter only re-subscribes to trigger-referenced states (`WatchMore`) on connect. `TriggerEngine` now takes an `on_change` callback that re-pushes the current `WatchMore` set right after a create/update (Refs #91, #27)
* Added: new `SettingsManager` (`settings_category`/`settings` tables, hierarchical via self-referencing `parent`) plus `GetSettings`/`UpdateConfig`/`CreateSetting`/`DeleteSetting` RPCs on `HannahServicer` — final phase of #27's WebUI gRPC surface. Moves `ble.tags`, `cars`, `nlu.*`, `llm.system_prompt` and `iobroker.state_names` out of static `config.yaml` into editable DB storage; `core/deploy/migrate_config_settings.py` does the one-time cutover. Unlike earlier phases, this one is wired into runtime immediately: `main.py` now builds the same `cfg`-shaped dicts `NLU`/`CarTracker`/`BleLocationEngine`/`IoBrokerClient` already expected, just sourced from `SettingsManager` instead of `cfg.get(...)` for these 5 areas — no changes needed in those 4 modules themselves, with a fallback to the old `cfg`/code defaults wherever a category hasn't been migrated yet (Refs #92, #27)

### Hannah Proxy
* Changed: updated proto files to reflect the newest Core changes (#27 Phases 1–5: Rooms/Groups, Satellites, Login, Routines/Triggers, Settings) (Refs #27)

### Telegram
* Changed: updated proto files to reflect the newest Core changes (#27 Phases 1–5: Rooms/Groups, Satellites, Login, Routines/Triggers, Settings) (Refs #27)

## 0.44.0 (2026-06-27)
### Hannah Core
* Changed: `routines.yaml`/`triggers.yaml` replaced by SQLite (`routines`/`triggers` tables, `hannah.db`) — new `Routine`/`Trigger` models (`hannah.models.routine`/`hannah.models.trigger`), nested condition/action structures (`when`, `cancel_when`, `on_response`, `triggers`, `actions`) stored as JSON columns, same pattern as `LinkedAccount.provider_payload`. `RoutineManager`/`TriggerEngine` now take a `db` callable instead of a file path; eliminates the mtime-based hot-reload entirely (SQL query is always current). Part of #27's planned WebUI scope — Routinen/Trigger get full CRUD via the WebUI once it lands (Refs #27)
* Added: `core/deploy/migrate_triggers_routines.py` — one-time, idempotent migration of existing `routines.yaml`/`triggers.yaml` content into `hannah.db`, analogous to `migrate_rooms_db.py` for #77 (Refs #27)

## 0.43.1 (2026-06-26)
### Hannah Core
* Fixed: the room fallback for voice commands without an explicit room (`main.py`) only checked `udp_server.get_registered_room()` — proxy-connected satellites are tracked separately (`grpc_servicer._proxy_satellites`) and were never consulted, even though RoomManager already had a room assigned for them at registration time. Now resolves directly via `room_manager.get_satellite_room()`, independent of the live connection type (Refs #87)
* Removed: `device_rooms` config (static MQTT-satellite room fallback) — dead since #35 removed room reporting from satellite NVS entirely, making RoomManager the sole authority; no legacy satellites needing this fallback remain in active use (Refs #87)
* Removed: `residents.user_roomie`/`user_roomies` config (static list of "real" roomie IDs used to tell residents apart from guests in unscoped presence queries) — `ResidentsClient.is_home()` now derives this from the User Registry (new `UserManager.get_roomie_ids()`, based on `User.type == "roomie"` via the linked `residents` account) instead of duplicating it in config, consistent with #72 (Refs #87)

## 0.43.0 (2026-06-26)
### Satellite Firmware
* Added: `POST /nvs` HTTP endpoint — lets the ioBroker adapter remotely update whitelisted NVS keys (`wifi_ssid`, `wifi_pass`, `mqtt_broker`, `mqtt_port`, `ota_channel`, `seed`, `ww_threshold`) over WiFi without physical/WebSerial access, then restarts. Secured by a new, dedicated `nvs_token` — kept separate from `ota_token` since that one isn't guaranteed identical across the fleet (overridable per-device via `/settings`) and can't double as a shared secret. Empty `nvs_token` = endpoint fully disabled (fail closed) (Refs #36)

## 0.42.1 (2026-06-25)
### Hannah Core
* Fixed: `UserManager.get_user_by_id()` crashed with `ValueError` on a non-numeric `user_id` instead of returning `None` — Voice-ID returns the literal string `"unknown"` as `speaker_user_id` when recognition confidence is too low, which flows straight into this lookup via `main.py`'s `_speaker_context()`/`_resolve_roomie_id()`. Those two call sites also bypassed `UserManager`'s cache entirely by calling `User.get()` directly; now go through `get_user_by_id()` like everything else (Refs #84)

## 0.42.0 (2026-06-25)
### Hannah Core
* Changed: BLE-Indoor-Lokalisierung (`ble_location.py`) ist jetzt von ioBroker Residents entkoppelt und setzt direkt `User.presence`, statt über `ResidentsClient`/`Resident` zu laufen. Grund: `ResidentsClient._residents` wird nur asynchron über die gRPC-Verbindung zum Adapter befüllt (Einzel-Updates oder das `send_residents`-Snapshot, #73) — BLE-Reports kommen aber unabhängig per MQTT und können direkt nach einem Core-Neustart schon eintreffen, bevor der Adapter verbunden ist, was zu `log.warning(...Tippfehler in config.yaml?)` führte, obwohl kein Tippfehler vorlag. `UserManager` lädt dagegen synchron aus der lokalen SQLite-DB, keine Race möglich
* Changed: `config.yaml`s BLE-Tag-Einträge nutzen jetzt `username` statt `roomie`/`type` — Auflösung zu `user_id` passiert einmalig beim Config-Laden (nicht mehr pro Sichtung), ein unbekannter Username wird sofort beim Start gewarnt statt erst bei der ersten Sichtung
* Added: `UserManager.dump_present_users()`, aufgerufen bei jedem `AgentConnect` — pusht "anwesend" für jeden User, den Hannah aktuell als zuhause kennt, Richtung ioBroker. Schließt eine Lücke aus #82: BLE-Sichtungen können eintreffen, bevor der Adapter überhaupt verbunden ist, das zugehörige arrival-Event verhallt dann ungehört. Sendet bewusst nur "anwesend", nie "weg" — ioBroker kann eine eigene, unabhängige Presence-Quelle haben (z.B. WLAN-Controller-Tracking), die nicht überschrieben werden soll (Refs #83)

## 0.41.2 (2026-06-25)
### Hannah Core
* Fixed: the `/satellites` WebUI page's "Meldet sich als" warning compared a live-resolved room *ID* (e.g. `leonie_schlafzimmer`) against the assigned room's *display name* (e.g. `Leonie Schlafzimmer`) — a false positive for every room whose ID isn't spelled identically to its display name, even though the satellite was correctly assigned. The satellite/proxy never sends a room at all (`SatelliteRegistration.room` was deliberately removed — RoomManager is the sole authority); the mismatch check now compares room ID against room ID, resolving the live ID to a display name only for the message text (Refs #81)

## 0.41.1 (2026-06-25)
### Hannah Core
* Fixed: a satellite's "last seen" timestamp froze forever after its initial registration — `udp_server.py` only refreshed it on the `"register"` control packet, never on the periodic `"heartbeat"` ones; `grpc_server.py`'s `NotifySatelliteRegistered` (proxy-routed satellites) never refreshed it at all, not even once. Since the Go proxy also never forwards individual satellite heartbeats to Core (only one heartbeat per proxy connection, covering every satellite behind it), `RegisterProxy`'s heartbeat drain loop now refreshes `last_seen` for every currently-known proxy satellite on each proxy heartbeat as a pragmatic stand-in — a real per-satellite heartbeat would need a proxy protocol change, deliberately out of scope here (Refs #80)

## 0.41.0 (2026-06-25)
### Hannah Core
* Fixed: `BaseModel.create()`/`update()`/`delete()` never rolled back on a failed write (e.g. `IntegrityError` from a UNIQUE violation) — the implicitly-started transaction stayed open on that connection, which then blocked every other write to the same DB file with `database is locked` until the connection happened to get garbage-collected. Found while writing an end-to-end test for #77; also affects `User`/`LinkedAccount` already in production (e.g. a duplicate username/email via `/users/create`) (Refs #79)
* Added: `Room`/`Group`/`Satellite` models, `rooms`/`groups`/`group_rooms`/`satellites` tables added to `hannah.db`'s schema (Refs #77)
* Changed: `RoomManager` now uses the `hannah.models` layer instead of hand-rolled `sqlite3` — same public API/return shapes, so `main.py`/`webui.py`/`grpc_server.py` needed no changes beyond the constructor call. `group_rooms` (pure n:n pivot) stays model-less, queried via joins; `Satellite`'s pairing rename (device_id is the PK) stays raw SQL since `BaseModel.update()` never touches PK columns (Refs #77)
* Added: `core/deploy/migrate_rooms_db.py` — one-time, idempotent migration of the real production data in the old standalone `rooms.db` into `hannah.db`'s new tables; ships with the next core release since `deploy/` is part of the release tarball (Refs #77)

## 0.40.6 (2026-06-25)
### Hannah Core
* Fixed: `hannah.db` (User-Registry, Issue #72) was deleted on every AutoDeploy update — `DB_PATH` defaulted to a path relative to `__file__` (`.../core/hannah/hannah.db`), landing it *inside* the `hannah/` package directory that `autodeploy.py`'s `_extract_and_copy()` wipes and replaces wholesale on each deploy. Now defaults to the relative path `"hannah.db"`, resolved against the service's working directory like `room_manager.py`'s `rooms.db` and `memory.py`'s `memory.db` already do (Refs #76)

## 0.40.5 (2026-06-25)
### Hannah Core
* Added: "Löschen"-Button auf der `/users`-WebUI-Seite — `username` ist im Edit-Formular absichtlich readonly (Identifier für Telegram `/verknuepfen` u.a.), ein Vertipper beim Anlegen (z.B. Groß-/Kleinschreibung) ließ sich bisher nur direkt in der DB korrigieren. Neue `UserManager.delete_user()` räumt zusätzlich den In-Memory-Cache/Wiring-State auf, `linked_accounts` läuft per `ON DELETE CASCADE` mit (Refs #75)

## 0.40.4 (2026-06-25)
### Hannah Core
* Fixed: the adapter's initial `send_residents` snapshot (sent once per `AgentConnect`, all currently known residents in one message) was never wired up on the Core side — `on_agent_send_residents` was passed as `None` with a `#TODO`, so `HannahServicer` always fell through to `log.warning("[grpc] Unrecognized AgentMessage payload: send_residents")` and Core had to wait for the next individual `resident_update` per resident instead (Refs #73)

## 0.40.3 (2026-06-25)
### Hannah Core
* Changed: `User.id` and every `*Request.user_id`/`GetUserRequest.id` field (LinkAccount, UnlinkAccount, SetTrustLevel, SetSystemMessages, GetUser) are now `int32` instead of `string` on the wire, matching the actual `users.id` SQLite column — found while debugging `/verknuepfen` always failing with `Exception calling application: '3'`. `EnrollVoiceprintRequest.user_id`/`SubmitSatelliteAudioRequest.speaker_user_id` stay `string` on purpose — those cross into the Voice-ID HTTP service, which treats the identifier as an opaque key
* Fixed: `UserManager.get_user_by_id()` looked a (possibly string) `user_id` up against its int-keyed cache after caching under the int — `self._users[user_id]` then missed with `KeyError` for any non-int input; now normalizes to `int(user_id)` up front regardless of what the proto wire type guarantees
* Fixed: `_user_to_pb` read `acc.service` to build the `linked_accounts` map, but the `LinkedAccount` model attribute is `.provider` — crashed with `AttributeError` on `GetUsers`/`GetUser` for any user with at least one linked account
* Fixed: `BaseModel.__init__` called `json.loads(value)` on every JSON-typed column unconditionally, including an empty string — `provider_payload` defaults to `""` when a caller (e.g. Telegram's `/verknuepfen`) never sets it, so the very next read of that row raised `JSONDecodeError`. Empty string now deserializes to `None`
* Added: regression tests in `core/tests/test_grpc_server.py` exercising `LinkAccount` and `_user_to_pb` against a real (non-mocked) `UserManager`/SQLite DB — all four bugs above were invisible to the existing mock-based tests

### Hannah Proxy
* Changed: proto copy synced with the `User`/`*Request.user_id` `string` → `int32` change above — mirror-only, the proxy itself never touches these fields

### Telegram
* Changed: proto copy synced with the `User`/`*Request.user_id` `string` → `int32` change above — `user.id` was already typed `int` in `grpc_client.py`'s signatures, so no source changes needed, just regenerated stubs

## 0.40.2 (2026-06-25)
### VoiceID
* Added: `voiceid/deploy/install-macos.sh` now passes `--config /opt/hannah/etc/voiceid.yaml` to the service — config support already existed in `app.py` but nothing on macOS ever wired it up, so `unknown_threshold`/`uncertain_threshold`/host/port were silently stuck on defaults

## 0.40.1 (2026-06-25)
### AutoDeploy
* Fixed: the self-update restart path (when autodeploy deploys a newer version of itself) still hardcoded `systemctl restart` — `_restart_service()` got the macOS/launchd platform switch earlier, but this is a separate call site that was missed, crashing with `FileNotFoundError` on the Mac Mini as soon as a newer autodeploy release was available

## 0.40.0 (2026-06-25)
### Hannah Core
* Added: new SQLite-backed user/linked-account model (`users`, `linked_accounts`), replacing ioBroker Residents as the source of authority for Hannah's users — accounts, trust levels, and provider links now live natively in Hannah Core (Refs #72)
* Added: `linked_accounts.external_id` column — separates the per-provider lookup key from `provider_payload` (now JSON metadata only), since the payload's shape differs per provider (residents: `roomie_id` nested in JSON; telegram: raw ID; OAuth: tokens) and can't be queried generically. `LinkedAccountLookup` proto message gets an `external_id` field to carry the search value (Refs #72)
* Fixed: `GetUser`'s `linked_account` lookup branch joined `linked_accounts` without an `ON` clause and filtered via a non-existent `linked_accounts__provider` kwarg (`Query.where()` has no Django-style relation traversal); now joins and filters explicitly on `provider` + `external_id` (Refs #72)
* Fixed: `GetUser`'s `user_name` lookup queried a non-existent `users.user_name` column — the actual DB/model column is `username` (Refs #72)
* Fixed: `_user_to_pb` called dict-style `.get()` on `User` model instances, which silently resolved to `BaseModel.get()` (a classmethod) instead of raising — crashed with `AttributeError` on every `GetUsers`/`GetUser` call; now reads model attributes directly (Refs #72)
* Fixed: `GetUser` mapped every lookup failure through `_ambiguous_message` (built for the old `AmbiguousResidentError`) — with the new model "not found" is the only failure mode (`one_or_404()` raises plain `LookupError`), so it now returns `NOT_FOUND` instead of crashing on the missing `.roomie_id`/`.types` attributes (Refs #72)
* Removed: `user_registry.py` — the old ioBroker-Residents-driven SQLite registry (UUID/`roomie_id`/trust level) is fully superseded by the new `hannah.models` layer (Refs #72)
* Changed: `EnrollVoiceprintRequest.roomie_id` → `.user_id`, `SubmitSatelliteAudioRequest.speaker_roomie_id` → `.speaker_user_id` — Voice-ID now identifies speakers by Hannah's own stable `users.id` instead of an ioBroker roomie_id, consistent with decoupling account identity from ioBroker entirely (Refs #72)
* Fixed: `_speaker_context()` queried `User.get(db, user_name=...)` against a column that doesn't exist (the actual column is `username`) and then read the result with dict-style `user["display_name"]`/`user.get("trust_level", 5)` — both crash (or silently return a bound method) on a `User` model instance; now resolves by `id` and reads plain attributes (Refs #72)
* Added: `_resolve_roomie_id()` in `main.py` — bridges a Hannah `user_id` back to its linked ioBroker `roomie_id` (via `linked_accounts[provider="residents"].provider_payload`) for the two places that still need a name-shaped identifier: `car_tracker`'s `owner_roomies` matching and `residents.set_user_home`/`set_user_away` (Refs #72)
* Added: WebUI page `/users` — lists Hannah Users and lets an admin link/unlink them to a known ioBroker Resident (Roomie/Guest/Pet), using the same `link_account`/`unlink_account` calls Telegram's `/verknuepfen` already goes through. Backed by a new `ResidentsClient.all_residents()`. Manual stand-in until residents get auto-linked on arrival (Refs #72)
* Fixed: `UserManager.create_user()` never passed `display_name`/`type` to `User.create()` — both are `NOT NULL` without a default, so every call crashed with `IntegrityError`; now defaults `display_name` to the username and `type` to `"roomie"`, both overridable (Refs #72)
* Added: WebUI `/users/create` and `/users/<id>/edit` — an admin can now create and edit Hannah Users directly in the WebUI instead of via raw SQL, which was only ever meant for the initial bootstrap (Refs #72)
* Added: bidirectional mood sync between a Hannah User and its linked Resident. Pull (ioBroker → Hannah) via a new `ResidentsClient.on_mood_changed()`, mirroring the existing arrival/departure dispatch. Push (Hannah → ioBroker) via a new `AgentSetResidentMood` command — kept separate from `AgentSetResident` rather than adding an optional field to it, to avoid any ambiguity between "mood intentionally 0" and "mood not set" on the wire. `UserManager` now tracks presence- and mood-wiring per user in separate sets, so `set_residents_pusher()` and `set_mood_pusher()` can be bound in either order without one blocking the other's retroactive wiring (Refs #72)

### Hannah Proxy
* Changed: Voice-ID client (`internal/voiceid/client.go`) and `SubmitSatelliteAudio` follow the same `roomie_id` → `user_id` rename — `IdentifyResponse.RoomieID` → `.UserID`, `X-Roomie-ID` HTTP header → `X-User-ID`, `SubmitSatelliteAudioRequest.SpeakerRoomieId` → `.SpeakerUserId` (Refs #72)

### VoiceID
* Changed: `/enroll` and `/identify` speak `user_id` instead of `roomie_id` — `X-Roomie-ID` request header → `X-User-ID`, `{"roomie_id": ...}` response field → `{"user_id": ...}`; the service itself stores/matches by opaque key either way, only the wire naming changes (Refs #72)
* Changed: `voiceid/deploy/install-macos.sh` rewritten to install from the Update Server (matching every other `install.sh` in the repo) instead of a direct git clone. Also fixes two bugs in the old script found while planning a reinstall: `--uninstall` deleted `voice_profiles` despite claiming to keep them (nested inside the install dir it then `rm -rf`'d), and it crashed outright on an unset `$MEM_SYMLINK`. Code/venv now live in `/opt/hannah/voiceid`, voice profiles/cache in a separate `/opt/hannah/voiceid-data/` that no install/update/uninstall step ever touches

### Telegram
* Changed: `/verknuepfen` and `/trustlevel` resolve users by `username` instead of `roomie_id` — `get_user_by_roomie()` and its `resident_type` disambiguation argument (made obsolete now that usernames live in Hannah's own `users` table instead of colliding across ioBroker resident types) are replaced by `get_user_by_username()`; linking now threads the resolved `user.id` through to `LinkAccountRequest` instead of a bare username string (Refs #72)
* Changed: client-side proto usage follows the `User`/`LinkedAccountLookup`/`*Request` field renames that came with Hannah's own user model — `uuid` → `id`, `roomie_id` → `user_name`, `LinkedAccountLookup.service`/`.account_id` → `.provider`/`.external_id`, `SetSystemMessagesRequest.uuid` / `SetTrustLevelRequest.roomie_id` / `LinkAccountRequest.roomie_id` → `.user_id` (Refs #72)
* Fixed: the rename above initially landed only half-applied and would have taken the bot down hard — `LinkedAccountLookup` was still built with the old `service`/`account_id` field names, which fails outright on every `GetUser` linked-account lookup, i.e. every authenticated message (`_is_known_user`/`_get_user`/`_has_trust` all go through it); leftover `user.roomie_id`/`.uuid`/`.username` accesses on the renamed `User` message, plus a stray `get_user_by_roomie()` call in `send_car_parked_to_all()` that was never updated when the method itself got renamed, would additionally have broken system notifications, `/systemmessages`, the trust-level confirmation reply, and car-parked-owner pings respectively (Refs #72)
* Changed: linking/help copy (`/verknuepfen` docstring, `_WELCOME`, `_UNKNOWN_USER`, command usage strings) now asks for a username instead of a Roomie-ID; `/verknuepfen` and `/trustlevel` drop the now-meaningless `[roomie|guest|pet]` disambiguation argument (Refs #72)

### AutoDeploy
* Added: macOS support — `_restart_service()` uses `launchctl kickstart -k system/<label>` instead of `systemctl restart` when running on Darwin; new `deploy/install-macos.sh` bootstraps the agent itself as a LaunchDaemon via the Update Server, mirroring the existing Linux installer. The voiceid Mac install still bypasses the Update Server entirely (separate concern, not changed here)

## 0.39.1 (2026-06-21)
### Hannah Core
* Fixed: `UserRegistry._init_db()`'s `type`-column migration (#64/0.39.0) did `ALTER TABLE users RENAME TO users_old`, which made SQLite automatically rewrite `linked_accounts.user_uuid`'s FOREIGN KEY to point at `users_old` — the migration then dropped that table, leaving `linked_accounts` referencing a table that no longer existed. Every `link_account()` call failed with `FOREIGN KEY constraint failed` (surfaced in Telegram as `/verknuepfen` always failing). `_init_db()` now rebuilds `linked_accounts` too, repointed at the new `users` table *before* `users_old` is dropped (dropping it first fails too — for the same reason) (Refs #69)
* Fixed: `get_by_roomie`/`link_account`/`set_trust_level` resolved a resident by `roomie_id` alone — if a Guest and a Roomie (or a Pet) share a name, `fetchone()`/`UPDATE ... WHERE roomie_id = ?` would silently act on whichever row SQLite happened to return, with no guarantee it's the right one (e.g. linking your Telegram account to a same-named pet instead of yourself). All three now accept an optional `resident_type` and raise a new `AmbiguousResidentError` (naming the colliding types) when it's omitted and more than one active match exists, instead of guessing (Refs #69)
* Changed: `GetUserRequest`/`LinkAccountRequest`/`SetTrustLevelRequest` get an optional `ResidentType type` field to pass the disambiguation through gRPC; the corresponding `HannahServicer` handlers catch `AmbiguousResidentError` and fail the RPC with `FAILED_PRECONDITION`, naming the colliding types in the details (Refs #69)
* Changed: `set_system_messages` now identifies the target by `uuid` instead of `roomie_id` — its only caller (Telegram `/systemmessages`) always acts on the requesting user, who is already uniquely resolved via their linked Telegram account beforehand, so threading `roomie_id` (+ the collision risk that comes with it) through was pointless. `SetSystemMessagesRequest.roomie_id`/`.type` replaced by `.uuid` (never released, safe to change outright) (Refs #69)

### Hannah Proxy
* Changed: proto updated — `GetUserRequest`/`LinkAccountRequest`/`SetTrustLevelRequest` get an optional `type` field, `SetSystemMessagesRequest.roomie_id`/`.type` replaced by `.uuid` (Refs #69)

### Telegram
* Changed: `/verknuepfen <roomie-id> [roomie|guest|pet]` — the type is now an optional second argument, needed only when `roomie-id` is ambiguous; the bot surfaces Hannah Core's `FAILED_PRECONDITION` details instead of swallowing them as a generic "not found" (`get_user_by_roomie`/`link_account` now thread `resident_type` through and return the real error message) (Refs #69)
* Changed: `/trustlevel <roomie-id> <0-10> [roomie|guest|pet]` — same optional type argument as `/verknuepfen`, for the same reason (admin-only command, but still needs to disambiguate a colliding `roomie-id`) (Refs #69)

## 0.39.0 (2026-06-21)
### Hannah Core
* Changed: `is_guest: bool` replaced by a `ResidentType` enum (`ROOMIE`/`GUEST`/`PET`) throughout the residents proto surface (`AgentResident`, `AgentSetResident`); `AgentResidentUpdate` removed and merged into `AgentResident` (now also carries `name`, `optional mood_level`, `presence_state`), used directly as the `resident_update` payload — groundwork for Pet support (Refs #64)
* Added: `core/hannah/residents/` package — `Resident` base class (`Roomie`/`Guest`/`Pet` subclasses) replaces the flat boolean/cache-dict model; a minimal event system (`on()`/`_emit()`) plus `update()` detect arrival/departure (and `mood_changed`) transitions on the object itself instead of an external string-keyed cache, so Pets get the same presence semantics as Roomies/Guests for free (Refs #64)
* Changed: `ResidentsClient` (`core/hannah/residents.py` → `core/hannah/residents_manager.py`) drops the MQTT-topic-string parsing path entirely — residents have been driven exclusively via gRPC for months, the string cache was dead weight; adds `get_or_create(roomie_id, cls)` as a persistent per-resident registry. The four separate `on_arrival`/`on_departure`/`on_guest_arrival`/`on_guest_departure` callbacks collapse into one `on_arrival`/`on_departure` pair that receives the `Resident` object itself; consumers branch on type via `isinstance` where behavior actually differs (Refs #64)
* Fixed: `set_guest_home`/`set_guest_away` passed a bare `1` where the old `is_guest` bool argument used to go — after the `ResidentType` enum landed this silently collided with `ROOMIE = 1`, so outbound guest-presence writes tagged guests as roomies; now passes `pb.ResidentType.GUEST` explicitly
* Fixed: `_on_agent_set_resident` discarded the incoming `resident_type` and always called `residents.set_presence()` without it — adapter-initiated `SetResident` commands for guests were written through as roomie presence updates
* Fixed: `resident_update` handling in `grpc_server.py` never read the proto's `name` field, so Hannah never learned a resident's display name from gRPC presence updates; also `r.has_field(...)` → `r.HasField(...)` (would have raised `AttributeError` on the first update carrying a `mood_level`)
* Added: `User` class in `user_registry.py` — a thin decorator around `Resident` (Roomie/Guest/Pet) adding the registry-only fields (UUID, trust level, system messages) that don't belong in the presence domain. Pets get a `User` entry and a `trust_level` just like Roomies/Guests instead of being excluded — a SmartHome's permission model applies to every resident it lets live there, not just the humans (e.g. an electronic cat flap gated by `trust_level`). `UserRegistry.sync()` now resolves each incoming resident's live `Resident` instance via `ResidentsClient.get_or_create()` (shared object, not a duplicate) and wraps it; query methods (`get_all`/`get_by_roomie`/etc.) still return plain dicts for now (Refs #64, follow-up to replace the whole query API tracked in #68)
* Fixed: `ResidentsClient._residents` and the `users` table were keyed by `roomie_id` alone — a Guest and a Roomie with the same name (separate prefixes in the residents adapter, perfectly legal) would have collided: `get_or_create()` would silently return the wrong type's instance once a key existed, and the `users` table's `UNIQUE(roomie_id)` constraint would reject the second insert outright. Both are now keyed by `(roomie_id, type)` — `ResidentsClient` via a `(resident_cls, roomie_id)` tuple key plus a new `get_or_null(roomie_id, cls)` (returns `None` instead of creating, for callers like the BLE tracker that should only ever reference an already-known resident); `users` via a `UNIQUE(roomie_id, type)` constraint, migrated from the old single-column `UNIQUE(roomie_id)` with a table rebuild (existing rows get `type=NULL` since the real type was never recorded before — `sync()` backfills it from the next live snapshot instead of inserting a duplicate)
* Fixed: `UserRegistry.sync()` deactivation/reactivation matched rows by `roomie_id` instead of `uuid` — would have deactivated/reactivated the wrong row once two residents share a `roomie_id` across types
* Fixed: `UserRegistry.sync()` recomputed `resident_ids` and reassigned `residents = list(residents)` inside the per-resident loop (only correct once a `next()` had already run), and incremented `added` for every already-known resident on every sync call, not just newly inserted/reactivated ones
* Added: BLE tags can now reference Pets, not just Roomies — `ble.tags[]` in `config.yaml` gets an optional `type` field (`roomie`/`guest`/`pet`, default `roomie`) alongside `roomie`, since a `roomie_id` alone isn't unique across types. On a location change, `_on_ble_location_change` resolves the tag's `(roomie, type)` via `ResidentsClient.get_or_null()` — only acting on an already-known resident, never creating a phantom one from a config typo — and sets `presence_state` to home. This is one-directional on purpose: a BLE sighting is a strong "home" signal, but a stale/lost tag is not a reliable "away" signal (weak reception ≠ left the house), so a disappearing tag never resets presence (Refs #64)

### Hannah Proxy
* Changed: proto updated — `ResidentType` enum replaces `is_guest` bool, `AgentResidentUpdate` merged into `AgentResident` (Refs #64)

### Telegram
* Changed: proto updated — `ResidentType` enum replaces `is_guest` bool, `AgentResidentUpdate` merged into `AgentResident` (Refs #64)

## 0.38.3 (2026-06-20)
### Hannah Core
* Added: `IoBrokerClient.handle_state_update()` now logs a `WARNING` (once per suffix, no log spam on repeated updates) when a live state update arrives for a suffix missing from `config.yaml`'s `iobroker.state_names`, instead of silently dropping it — found via a stale production `config.yaml` that never got the `iaq`/`co2_equiv`/`voc_equiv` entries added for #21, causing those values to freeze at the last gRPC snapshot indefinitely without any visible symptom (Refs #21)

## 0.38.2 (2026-06-20)
### Satellite Firmware
* Added: WiFi AP-Setup-Modus verlässt sich nicht mehr endgültig — ein periodischer Timer (alle 10 Minuten, gleiches Muster wie der bestehende SNTP-Retry) versucht im Hintergrund das ursprüngliche Netz wiederzufinden, parallel zum laufenden AP (kein Scan-/Konfigurations-Unterbruch). Bei Erfolg wird der AP nur sofort abgeworfen, wenn kein Client mehr am Captive Portal hängt — sonst wartet der Cutover bis zur letzten Trennung, damit eine laufende Konfiguration (z.B. neuer PSK bei Netz-Umzug) nicht durch einen verschwindenden AP unterbrochen wird. Kein Retry bei unkonfigurierten Geräten ohne hinterlegtes WiFi (Refs #52)

## 0.38.1 (2026-06-20)
### Hannah Core
* Added: `RoomManager.sync_rooms()` now detects rooms that disappeared from the ioBroker enum catalog and removes them; satellites that were assigned to a vanished room have their `room_id` nulled (kept in the DB, not deleted) and are reported back to the caller, which pushes `agent_satellite_deleted()` to the adapter so the now-roomless satellite's object tree is cleaned up there too (Refs #51)
* Fixed: `NotifySatelliteRegistered`/`NotifySatelliteGone` pushed `agent_satellite_update()` to the adapter twice per connect/disconnect — once directly, once via the `_on_satellite_change` online/offline diff in `main.py`; the direct calls are removed, `_on_satellite_change` now resolves `display_name` itself via `RoomManager.resolve_satellite_name()` (Refs #53)
* Fixed: the UDP-direct satellite path (fallback when no proxy is connected) took its room straight from the satellite's own registration payload, completely bypassing `RoomManager` — a satellite has had no way to know its own room since the room/group management rework (#25); it now resolves the room via `RoomManager` like the proxy path already does, and isn't tracked/forwarded to the adapter at all without one (Refs #53)

## 0.38.0 (2026-06-20)
### Hannah Core
* Added: `humidity_sensor` sensor category — `_CATEGORY_STATES["humidity_sensor"]` with `current` (%); reuses the existing generic category-query mechanism, same pattern as `temperature_sensor` (Refs #47)
* Added: `category_words` for humidity (luftfeuchtigkeit, luftfeuchte, feuchtigkeit, feuchte) in `config.yaml`/`config.example.yaml` (Refs #47)
* Changed: satellite deletion moved fully into Hannah Core — `RoomManager.delete_satellite()` + new Web UI "Löschen" button on `/satellites` (only shown for offline satellites) replace the old AdminUI-only path that never touched Core's DB, leaving ghost entries behind; `HannahServicer.agent_satellite_deleted()` pushes the new `AgentSatelliteDeleted` command (`AgentCommand.satellite_deleted`) to tell the adapter to remove the object tree (Refs #42)

### Hannah Proxy
* Changed: proto updated — `AgentSatelliteDeleted` added (Refs #42)

### Telegram
* Changed: proto updated — `AgentSatelliteDeleted` added (Refs #42)

## 0.37.1 (2026-06-20)
### Hannah Core
* Fixed: `IoBrokerClient.handle_state_update` silently dropped live updates for state suffixes missing from `config.yaml`'s `iobroker.state_names` — affected `iaq`/`co2_equiv`/`voc_equiv` (added in the `air_quality_sensor` category) since they were never added there; the initial gRPC snapshot writes the raw suffix directly (no `state_names` translation), so affected values froze at whatever the last snapshot held instead of updating live (Refs #21)
* Fixed: `_describe_category` repeated the device name twice in single-device responses (e.g. "Sofaecke im Wohnzimmer: Sofaecke: okay, ...") — the per-device name prefix is now only added when there's more than one device in the room; affects all single-device sensor categories (temperature, window, door, air quality), not just air quality
* Changed: `air_quality_sensor` category — `co2_equiv`/`voc_equiv` units now read "ppm CO₂"/"ppm VOC" instead of plain "ppm" so the two values are distinguishable by voice

### Telegram
* Added: `_device_status_text` now renders `iaq`/`co2_equiv`/`voc_equiv` for air-quality devices (was missing entirely — the device showed up in `/haus` menus with no values); `_iaq_label` mirrored from Hannah Core for the same plain-text assessment

## 0.37.0 (2026-06-19)
### Hannah Core
* Added: `air_quality_sensor` sensor category — `_CATEGORY_STATES["air_quality_sensor"]` with `iaq` (rendered as plain-text assessment via new `_iaq_label()`: 0–50 good, 51–100 okay, 101–150 slightly polluted, >150 bad), `co2_equiv` and `voc_equiv` (ppm); reuses the existing generic category-query mechanism instead of a Hannah-specific cache, so any ioBroker-known air quality sensor works, not just Hannah's own satellites (Refs #21)
* Added: `category_words` for air quality (luftqualitaet, iaq, co2, voc, luftguete, luft, raumluft) in `config.yaml`/`config.example.yaml` (Refs #21)

## 0.36.2 (2026-06-19)
### Satellite Firmware
* Fixed: `bsec_set_configuration` returned `BSEC_E_CONFIG_VERSIONMISMATCH` (-34) — `libalgobsec.a` (esp32s3) was linked from the BSEC2 "Selectivity" algorithm variant, while `bme680_iaq_33v_3s_4d.bin` is a config for the classic "IAQ" variant; replaced both with the matching `bsec_IAQ` build (BSEC 2.6.1.0 generic release) and stripped a 4-byte length header that the source `.config` file carries in front of the raw 492-byte config blob (closes #24)
* Fixed: unused variable `cfg` in `status_handler` (`hannah_webserver.c`) — leftover from the device-ID/room removal in #26/#32, never read (closes #46)

## 0.36.1 (2026-06-19)
### Hannah Core
* Added: `RoomManager` cleans up provisioned-but-never-paired satellite seeds older than `seed_ttl_days` (default 7) via a background thread, configurable in `config.yaml` (Refs #41)

## 0.36.0 (2026-06-19)
### Hannah Core
* Added: `AgentRoomSnapshot`/`AgentRoom` proto message + `AgentMessage.send_rooms` — adapter now sends the full `enum.rooms.*` catalog (independent of devices) on connect and on enum change; `RoomManager.sync_rooms()` is fed from it via a new `on_agent_room_snapshot` callback, so provisioning a satellite into a brand-new room with no devices yet no longer fails with `FOREIGN KEY constraint failed` (Refs #40)

### Hannah Proxy
* Changed: proto updated — `AgentRoomSnapshot`/`AgentRoom` added (Refs #40)

### Telegram
* Changed: proto updated — `AgentRoomSnapshot`/`AgentRoom` added (Refs #40)

## 0.35.0 (2026-06-19)
### Hannah Core
* Fixed: `NotifySatelliteRegistered` now skips satellites with no room in RoomManager instead of propagating empty `room_id` to the adapter — prevents ghost registrations from unpaired MAC-based device IDs (Refs #37)

### Hannah Proxy
* Refactor: removed `room` from all Go callbacks and gRPC calls — `AudioCallback`, `SatelliteChangeCallback`, `SubmitSatelliteAudio`, `NotifySatelliteRegistered`, `NotifySatelliteGone`; `SatelliteInfo.Room` removed; `RegisteredDevices()` now returns `[]string` (Refs #38)

## 0.34.1 (2026-06-19)
### Hannah Core
* Fixed: `_on_agent_satellite_control` (mute/dnd/volume/announcement/announcement_ssml/announcement_rephrase via the adapter) matched only against the satellite's self-reported room, which is always empty since #35 removed room reporting from firmware — now uses `_resolve_targets()` like all other room-based routing, so `RoomManager` assignments are honored (closes #39)

## 0.34.0 (2026-06-18)
### Hannah Core
* Changed: `GrpcServer.NotifySatelliteRegistered` no longer uses the satellite-reported room as fallback; `RoomManager` is now the sole authority for room assignment (Refs #35)
* Changed: `GrpcServer.SubmitSatelliteAudio` resolves room from `_proxy_satellites` / `RoomManager` instead of `request.room` (Refs #35)
* Changed: proto — `SatelliteRegistration.room` (field 2) reserved; room assignment is now a server-side concern only (Refs #35)

### Hannah Proxy
* Changed: proto updated — `SatelliteRegistration.room` (field 2) reserved (Refs #35)
* Note: proxy Go code still passes `room` in callbacks/RPCs — full cleanup pending proxy refactoring

### Telegram
* Changed: proto updated — `SatelliteRegistration.room` (field 2) reserved (Refs #35)

### Firmware (satellite-esp)
* Changed: removed `room` field from `hannah_config_t`, NVS, and register JSON message (Refs #35)
* Changed: removed `HANNAH_ROOM_NAME` from Kconfig (Refs #35)

### ioBroker Adapter
* Changed: `NvsDialog` — removed `room` field; re-flashing NVS no longer requires room selection; `provisionSatellite` call no longer passes `roomId` (Refs #35)
* Changed: `FlashDialog` — room free-text field replaced with dropdown populated from `enum.rooms.*`; `provisionSatellite` now called before flash with `seed` + `roomId`; `seed` written to NVS partition (Refs #35)
* Changed: `provisionSatellite` sendTo handler — `roomId` is now optional; enables seed-only re-provisioning without changing the satellite's room assignment (Refs #35)

## 0.33.0 (2026-06-18)
### Hannah Core
* Changed: `AgentDevice.room` now carries the enum ID segment (e.g. `wohnzimmer`) instead of the German display name; `room_names` map added with all available languages for NLU matching (Refs #33)
* Changed: `IoBrokerClient` keys rooms by enum ID; `Device.room_display_name` carries the German display name for spoken responses (Refs #33)
* Changed: `NLU._find_room` matches on display name (`room_names["de"]`) instead of the enum key — NLU behaviour unchanged, but now stable when enum IDs differ from German names (Refs #33)
* Changed: `GrpcServer` resolves `room_id` from `RoomManager` on satellite registration; `AgentSatelliteUpdate.room` now carries the enum ID so the adapter can use it as a language-neutral ioBroker path segment (Refs #33)
* Changed: proto — `AgentDevice.room_names: map<string, string>` added (field 8); comments updated on `AgentSatelliteUpdate.room` and `AgentSatelliteControl.room` to clarify room_id semantics (Refs #33)

### Hannah Proxy
* Changed: proto updated — `AgentDevice.room_names` map added (field 8) (Refs #33)

### Telegram
* Changed: proto updated — `AgentDevice.room_names` map added (field 8) (Refs #33)

## 0.32.0 (2026-06-18)
### Hannah Core
* Added: `RoomManager.resolve_satellite_name(device_id, serial)` — returns provisioned `display_name` from DB; looked up by serial if present, else by device_id (Refs #26)
* Added: `display_name` field (8) to `AgentSatelliteUpdate` — Core populates it from DB on every satellite registration event so the adapter can show a human-readable name in ioBroker (Refs #26)
* Added: `display_name` field (5) to `Satellite` message — returned by `GetSatellites` so the adapter has the correct name on initial connect without waiting for a re-registration event (Refs #26)
* Added: `resolve_satellite_name` callback parameter to `HannahServicer`; wired to `RoomManager.resolve_satellite_name` in `main.py` (Refs #26)
* Changed: `GetSatellites` now resolves `serial` and `display_name` per satellite from internal proxy state and DB (Refs #26)
* Added: `AgentSatelliteUpdate.display_name` (field 8) — human-readable satellite name from Core DB (Refs #26)
* Added: `Satellite.display_name` (field 5) — human-readable name included in `GetSatellites` response (Refs #26)
* Changed: `device_id` is now always derived from the eFuse MAC at boot (12-char lowercase hex) — replaces the previously NVS-configurable string; `serial` fields removed from proto, DB, and proxy (Refs #32)
* Changed: proto — removed `serial` from `Satellite` (field 4 reserved), `SatelliteRegistration` (field 4 reserved), `AgentSatelliteUpdate` (field 7 reserved) — field numbers reserved to prevent future accidental reuse (Refs #32)
* Changed: `room_manager.py` — removed `serial` column from `satellites` table; `pair_satellite` and `resolve_satellite_name` now operate on `device_id` only; removed `get_satellite_by_serial()` (Refs #32)
* Changed: `grpc_server.py` — `_proxy_satellites` keyed exclusively by `device_id`; removed dual-key serial/device_id lookup; `agent_satellite_update` no longer carries `serial` (Refs #32)

### Satellite Firmware
* Changed: status page (`/`) shows hardware serial (eFuse MAC) instead of configurable device-ID at "Gerät" row (Refs #26)
* Changed: settings page (`/settings`) no longer exposes "Geräte-ID" and "Raum" input fields — both are now managed by Hannah Core; NVS values remain intact and are still used for routing (Refs #26)
* Changed: `device_id` is now always the eFuse MAC (computed in `hannah_config_init`); `CONFIG_HANNAH_DEVICE_ID` Kconfig option removed; NVS key `device_id` no longer written or read (Refs #32)
* Changed: `send_register()` no longer sends a `"serial"` JSON field — `device` already carries the eFuse MAC (Refs #32)

### Hannah Proxy
* Changed: proto updated — `serial` removed from `SatelliteRegistration`; `NotifySatelliteRegistered` and satellite callbacks no longer carry or store serial (Refs #32)

## 0.31.1 (2026-06-18)
### Hannah Core
* Fixed: `_migrate_db` in `room_manager.py` failed with `sqlite3.OperationalError: Cannot add a UNIQUE column` — SQLite does not support `ADD COLUMN … UNIQUE`; replaced with `ADD COLUMN serial TEXT` followed by `CREATE UNIQUE INDEX … WHERE serial IS NOT NULL` (Refs #26)

## 0.31.0 (2026-06-18)
### Hannah Core
* Added: `satellites` table extended with `serial`, `seed`, `paired_at` columns; auto-migrates existing DBs (Refs #26)
* Added: `provision_satellite(seed, display_name, room_id)` — pre-registers a satellite before WebFlash (Refs #26)
* Added: `pair_satellite(device_id, serial, seed)` — links hardware serial to pre-provisioned seed entry on first connect (Refs #26)
* Added: `get_satellite_by_serial(serial)` — lookup by hardware serial (Refs #26)
* Added: `ProvisionSatellite` RPC + `ProvisionSatelliteRequest` message — adapter pre-provisions before flash (Refs #26)
* Added: `SatelliteRegistration.serial` (field 4) and `SatelliteRegistration.seed` (field 5) — sent by satellite on first connect (Refs #26)
* Added: `Satellite.serial` (field 4) — hardware serial in `GetSatellites` response (Refs #26)
* Changed: `NotifySatelliteRegistered` now returns `message="paired"` when seed pairing succeeds (Refs #26)
* Changed: `_proxy_satellites` keyed by serial for paired satellites; `device_id` stored in dict value for proxy routing (Refs #26)
* Changed: `stream_audio_to_proxy` resolves `proxy_device_id` from sat info — works with serial or device_id as `target` (Refs #26)
* Changed: `get_satellite_room_map()` returns both `device_id` and `serial` as keys so `_resolve_targets()` resolves rooms for paired satellites (Refs #26)
* Added: `AgentSatelliteUpdate.serial` (field 7) — hardware serial sent to adapter on registration; adapter uses as ioBroker object-ID (Refs #26)
* Added: `get_proxy_satellite_info(key)` helper on `HannahServicer` — resolves `(device_id, serial)` from a snapshot key (which may be serial or device_id) (Refs #26)
* Fixed: `_on_satellite_change` in `main.py` now resolves correct `device_id`/`serial` for paired satellites via `get_proxy_satellite_info()` instead of passing serial as device_id (Refs #26)

### Hannah Proxy
* Updated: `proto/hannah.proto` synced with Core — `ProvisionSatellite` RPC, `serial`/`seed` fields in `SatelliteRegistration` (Refs #26)
* Changed: `NotifySatelliteRegistered` now forwards `serial` and `seed` from the satellite register payload to Core; sends `{"type":"paired"}` to satellite if Core confirms pairing (Refs #26)
* Fixed: `SatelliteChangeCallback` signature in unit tests updated to match new `serial, seed` parameters (Refs #26)

### Satellite Firmware
* Added: hardware serial read from eFuse MAC (`esp_efuse_mac_get_default`) and sent in every Register message as `serial` field (Refs #26)
* Added: `seed` NVS key — one-time pairing token written during WebFlash; included in Register if present, cleared from NVS on `{"type":"paired"}` ACK from proxy (Refs #26)

## 0.30.0 (2026-06-17)
### Hannah Core
* Added: `RoomManager` — SQLite persistence for rooms (synced from ioBroker), n:n room groups, and satellite-to-room assignment (`core/hannah/room_manager.py`) (Refs #25)
* Added: Web UI (`core/hannah/webui.py`) — Flask app for room/group/satellite management; starts as daemon thread on configurable port (default 8080); `flask>=3.0.0` added to `requirements.txt` (Refs #25)
* Added: `web_ui` and `room_manager` config sections in `config.example.yaml` (Refs #25)
* Added: `_resolve_targets` uses DB satellite room assignments (overrides self-reported room) and DB groups (fallback: `config.yaml groups:`); NLU room list updated with DB groups on device snapshot (Refs #25)

## 0.29.3 (2026-06-17)
### AutoDeploy
* Added: optional `post_install` shell command in component config — executed after extraction, before state save and service restart; non-zero exit aborts the deployment

### VoiceID
* Added: `requirements.txt` with service dependencies (`torch`, `numpy`, `PyYAML`, `fastapi`, `uvicorn`, `speechbrain`)

## 0.29.2 (2026-06-17)
### Satellite Firmware
* Added: BSEC2 3.3V config binary (`bme680_iaq_33v_3s_4d.bin`) embedded via `EMBED_FILES` and loaded with `bsec_set_configuration()` after `bsec_init()` to improve self-heating compensation for 3.3V supply; falls back to 1.8V defaults with a warning on version mismatch (Refs #17, Refs #24)
* Fixed: `work_buf[BSEC_MAX_WORKBUFFER_SIZE]` (4096 bytes) in `sensor_init()` declared `static` to prevent stack overflow that caused heap corruption and a boot loop

## 0.29.1 (2026-06-17)
### Satellite Firmware
* Refactored: AudioLib integrated as an IDF component via EXTRA_COMPONENT_DIRS instead of a manual list of source files — future AudioLib updates will automatically include the source and header files (closes #23)
* Added: WebRTC VAD replaces RMS-based silence detection during streaming — `hannah_webrtc_vad_init/feed/free` (from AudioLib 0.2.0 / libfvad) distinguishes speech from music and background noise by spectral features instead of energy level; aggressiveness configurable via Kconfig (`HANNAH_VAD_WEBRTC_AGGRESSIVENESS`, default 2); `noise_ema` stays for the wakeword-onset guard (closes #20)

## 0.29.0 (2026-06-16)
### Hannah Core
* Added: MQTT sensor handler forwards IAQ, IAQ accuracy, CO₂ equivalent and VOC equivalent from satellite MQTT payload through to gRPC `AgentSensorUpdate` (Refs #17)

### Proto
* Added: `AgentSensorUpdate` extended with fields `iaq` (float), `iaq_accuracy` (uint32), `co2_equiv` (float), `voc_equiv` (float) — fields 6–9; zero when BSEC2 not calibrated (Refs #17)

### Hannah Proxy
* Updated: `proto/hannah.proto` synced with Core — `AgentSensorUpdate` extended with BSEC2 fields `iaq`, `iaq_accuracy`, `co2_equiv`, `voc_equiv`; field 5 (`gas_resistance`) reserved (Refs #17)

### Telegram
* Updated: `proto/hannah.proto` synced with Core — `AgentSensorUpdate` extended with BSEC2 fields `iaq`, `iaq_accuracy`, `co2_equiv`, `voc_equiv`; field 5 (`gas_resistance`) reserved (Refs #17)

### Satellite Firmware
* Added: BSEC2 library integration for BME680 — replaces raw gas resistance (Ω) with meaningful IAQ (0–500), Static IAQ, CO₂ equivalent (ppm), and breath VOC equivalent (ppm); accuracy level (0–3) published alongside; BSEC2 calibration state persisted to NVS every 30 min and restored on boot to retain accuracy across reboots (Refs #17)
* Added: `bme68x` and `bsec2` as local IDF components (`components/bme68x/`, `components/bsec2/`) with precompiled `libalgobsec.a` for ESP32-S3 (Xtensa LX7); workaround for IDF 6.0 `component_requirements.py` Windows path bug via explicit `target_include_directories` / `target_link_libraries` in `hannah_sensors/CMakeLists.txt`

## 0.28.3 (2026-06-16)
### Satellite Firmware
* Fixed: PDM clock inversion flag set to `true` (`clk_inv = true`) — SPH0641 with SEL=GND outputs data on the falling CLK edge; reading on the rising edge caused white noise instead of signal (Refs #5)
* Fixed: VAD `noise_ema` stuck at initial value 0.02 — `noise_ema` is now calibrated during the 5 s mic warmup (excluding TTS frames via `s_speaking_active` guard) so the correct floor is known before the first stream; idle tracking also gated on `!s_speaking_active` to prevent speaker bleed from contaminating the noise floor estimate (Refs #19)

## 0.28.2 (2026-06-16)
### Hannah Core
* Fixed: `_ask_fn` routes `start_listening` via MQTT instead of UDP — UDP-based send silently failed for proxy-connected satellites (closes #18)

### Satellite Firmware
* Fixed: `hannah_net` subscribes to `hannah/satellite/{device}/listen` MQTT topic and calls `start_listening` callback on receipt — proxy satellites now receive the command
* Fixed: `hannah_audio_start_listen_after_tts` activates virtual PTT immediately if TTS has already ended, avoiding a missed trigger when the MQTT message arrives after the sentinel

## 0.28.1 (2026-06-16)
### Hannah Core
* Fixed: `_ask_fn` now sends `start_listening` UDP command to all satellites in the room after TTS — satellites were not entering listening mode after the question was played, so no answer ever arrived at Hannah

### Satellite Firmware
* Fixed: added `start_listening` UDP command handler in `hannah_net` — triggers `hannah_audio_start_listen_after_tts()` callback
* Fixed: `hannah_audio`: after TTS playback drains (end-sentinel), if `start_listening` was received, sets virtual PTT active with 8s auto-timeout; PTT-mode mic task decrements counter and clears PTT on timeout; wakeword-mode cleans up virtual listen state on stream end

### Scripts
* Fixed: `release.js` now removes the `## **WORK IN PROGRESS**` line when promoting WIP entries to a version — the HTML comment above it is preserved

## 0.28.0 (2026-06-16)
### Hannah Core
* Added: `AgentAskResident` 

### Proto
* Added: `correlation_id` field to `AgentAskResident` — identifies a pending question across the round-trip; `AgentResidentAnswered` message carries the resident's spoken answer back to the adapter; `resident_answered` variant added to `AgentCommand` oneof so Hannah can push the answer over the existing adapter stream

## 0.27.0 (2026-06-15)
### Hannah Core
* Added: `for:` delay in triggers — state-trigger can specify `for: "5h"` (or `"30m"`, `"90s"`) to defer execution until the duration elapses; the Timer Service registers a SQLite-persistent timer so delays survive Hannah restarts; `cancel_when:` cancels the pending timer if a counter-condition is met before the delay fires; on reconnect, `TimerListRequest` reconciles active trigger timers against current state (stale timers cancelled, active ones restored into RAM); `cancel_when` state IDs included in `WatchMore` so the adapter watches them

## 0.26.0 (2026-06-15)
### Hannah Core
* Added: Trigger-Engine supports active questioning — triggers can use `ask` instead of `say` to pose a question via TTS and route the next utterance from that room as the answer; `on_response` rules match the free-form answer via `llm_match("category")` (LLM classification prompt) and execute `say` actions accordingly; unanswered questions time out after 60s; answered utterances bypass NLU routing (`AnswerPending` intent)
* Added: `LLMClient.match(text, category)` — classifies whether a free-form answer belongs to a semantic category using a yes/no LLM prompt; `DummyLLM` always returns `False`
* Added: `set_state` action in `on_response` rules — sets an ioBroker state directly when a response condition matches (`set_state: {id: "...", value: ...}`); can be combined with `say` in the same rule

### Hannah Proxy
* Changed: replaced `gopkg.in/yaml.v3` with `sigs.k8s.io/yaml` for config parsing — struct tags switched from `yaml:"..."` to `json:"..."` accordingly (config.yaml format unchanged)

## 0.25.2 (2026-06-13)
### Hannah Proxy
* Fixed: `SendTTSChunk()` now throttles UDP packet sending to playback rate — each 1400-byte packet is followed by a sleep proportional to its audio duration (`chunk_bytes / (sample_rate × 2)`); without this, the proxy sent all packets in a burst that overflowed the satellite's lwIP socket buffer, dropping most audio and causing garbled/truncated TTS on long responses

## 0.25.1 (2026-06-13)
### Satellite Firmware
* Changed: Speaker audio buffering replaced per-chunk `malloc`/`free` with a FreeRTOS `RINGBUF_TYPE_NOSPLIT` ring buffer (32 KB internal DRAM, ~640ms buffer at 24kHz) — `hannah_audio_play()` uses `xRingbufferSendAcquire`/`xRingbufferSendComplete` to write directly into the ring buffer without heap allocation; `speaker_task` uses `xRingbufferReceive`/`vRingbufferReturnItem`; end-of-stream signalled by a sentinel item with `len=0`; internal DRAM required (PSRAM not suitable for I2S-DMA source)

## 0.25.0 (2026-06-13)
### Hannah Core
* Added: `stream_audio_to_proxy()` — slices full TTS PCM into ~100ms chunks (4800 bytes @ 24kHz) and sends each as a separate `PlayAudioCommand` with `is_last=true` on the final chunk; reduces satellite startup latency from full Azure response time to first chunk arrival

### Hannah Proxy
* Added: `SendTTSChunk()` / `SendTTSEnd()` on the UDP server — proxy forwards each `PlayAudioCommand` chunk immediately without buffering; `tts_end` is sent only when `is_last=true`; removed 300ms sleep before `tts_end`
* Changed: `PlayAudioFunc` callback is now synchronous (no goroutine) to preserve chunk order within the gRPC stream

### Proto
* Changed: `PlayAudioCommand` — added `bool is_last = 4`; signals the proxy to send `tts_end` after the final chunk

## 0.24.13 (2026-06-13)
### Satellite Firmware
* Added: Asset Server URL and Token fields to the satellite settings web interface (`/settings`) — token inputs are write-only (password type); submitting an empty token field leaves the stored value unchanged
* Added: Update Server Token field to the satellite settings web interface — same write-only behaviour
* Added: "Disable TLS certificate validation" checkbox in settings web interface — stored in NVS (`tls_skip`), default off; when enabled, `crt_bundle_attach` is omitted so ESP-IDF skips chain verification (useful for self-signed certificates)

## 0.24.12 (2026-06-12)
### Satellite Firmware
* Fixed: `hannah_asset` now verifies the SHA256 of a downloaded asset against the manifest before caching it — previously an aborted partial download (e.g. 512 bytes from a dropped TLS connection) was accepted as valid (`total > 0`), its manifest hash stored in NVS, and the corrupt file served forever; mismatching files are now discarded and re-fetched on the next cycle (SHA256 via PSA Crypto API, mbedTLS 4.x compatible)
* Changed: BLE (NimBLE) memory footprint reduced — host heap moved to PSRAM (`BT_NIMBLE_MEM_ALLOC_MODE_EXTERNAL`) and roles restricted to observer-only (central/peripheral/broadcaster/SMP disabled), since `hannah_ble` is a pure passive scanner; frees scarce internal RAM that AES/I2S DMA and WiFi mgmt-frames compete for (TLS asset download failed with `esp-aes: Failed to allocate memory` while BLE was active)

## 0.24.11 (2026-06-12)
### Satellite Firmware
* Fixed: `asset_upd` task stack increased from 8 KB to 16 KB — with mbedTLS now able to complete the TLS handshake (v0.24.10), the ECDHE MPI hardware-acceleration operations (`mpi_ll_read_from_mem_block`) ran out of stack during the asset download, causing a stack overflow and reboot

## 0.24.10 (2026-06-12)
### Satellite Firmware
* Fixed: mbedTLS context allocations redirected to PSRAM via `mbedtls_platform_set_calloc_free()` — internal RAM fragmentation (caused by BLE/NimBLE init) prevented `mbedtls_ssl_setup` from allocating the SSL context, causing all TLS connections to fail after boot

## 0.24.9 (2026-06-12)
### Satellite Firmware
* Fixed: `hannah_asset` retries manifest fetch indefinitely (every 30 min) instead of giving up after 3 attempts — previously the update task deleted itself on failure, so assets were never fetched if TLS wasn't ready at boot
* Fixed: `CONFIG_MBEDTLS_KEY_EXCHANGE_RSA=n` added — disables RSA key exchange cipher suites (no forward secrecy); forces ECDHE negotiation with the Netscaler reverse proxy which otherwise prefers `AES256-SHA` (RSA key exchange) causing TLS handshake failure on ESP32-S3 via PSA crypto

## 0.24.8 (2026-06-12)
### Satellite Firmware
* Fixed: `CONFIG_LWIP_SNTP_MAX_SERVERS=2` added — without it, pool.ntp.org (at slot 1) was silently dropped because lwIP only allocated one server slot; now DHCP NTP (slot 0) and pool.ntp.org (slot 1) are both active
* Improved: `hannah_net_wait_sntp()` now uses an EventGroup bit instead of `esp_netif_sntp_sync_wait` — fixes immediate return on second call; bit stays set after sync so all subsequent callers return instantly
* Improved: after WiFi gets IP, a 30s repeating FreeRTOS timer calls `esp_sntp_restart()` until SNTP is synced

## 0.24.7 (2026-06-12)
### Satellite Firmware
* Fixed: SNTP init moved from `IP_EVENT_STA_GOT_IP` handler to `hannah_net_init()` — previously SNTP registered its `renew_servers_after_new_IP` event handler *after* the IP event already fired, so the DHCP-provided NTP server (Option 42) was never picked up; now the handler is registered before WiFi connects
* Fixed: `hannah_ota` now waits up to 10s for SNTP sync before the first `check_for_update()` call — prevents TLS handshake failure (`-0x008D`) caused by invalid system clock at t=60s boot

## 0.24.6 (2026-06-12)
### Satellite Firmware
* Fixed: `CONFIG_LWIP_DHCP_GET_NTP_SRV=y` added to `sdkconfig.defaults` — required for `server_from_dhcp = true` in SNTP config; without it ESP-IDF rejected the SNTP init with `sntp_init_api: Tried to configure SNTP server from DHCP, while disabled`

## 0.24.5 (2026-06-12)
### Satellite Firmware
* Fixed: SNTP time synchronization added — `hannah_net` starts NTP (`pool.ntp.org`) after WiFi connect; `hannah_asset` waits up to 10s for sync before first manifest fetch — fixes TLS handshake failure (`-0x3B00`) caused by invalid system clock
* Improved: SNTP now prefers NTP server from DHCP (Option 42); falls back to `pool.ntp.org` if DHCP provides none; DHCP-provided server is refreshed automatically on IP renewal
* Fixed: BME680 humidity compensation formula corrected to match Bosch reference (`bme68x.c`) — wrong divisors for `par_h3` (200→100), `par_h4` (100→16384), `par_h5` (10⁹→1048576) and wrong structure of correction term (used `v1` instead of `h`); fixes `H=0.0%` readings

## 0.24.4 (2026-06-11)
### Satellite Firmware
* Changed: `wakeword_enabled` removed from NVS and web interface — wake-word on/off is now a compile-time decision via `CONFIG_HANNAH_WAKEWORD_ENABLED`; threshold (`ww_threshold`) remains configurable at runtime
* Fixed: `hannah_asset` manifest fetch retries up to 3 times with 30s delay on failure instead of silently skipping the update
* Fixed: BME680 calibration block 1 address corrected from `0x89` to `0x8A`, length from 25 to 23 bytes — fixes incorrect temperature/humidity readings

## 0.24.3 (2026-06-11)
### Satellite Firmware
* Fixed: `hannah_asset` startup delay increased from 10s to 50s to ensure PSA crypto is ready before first TLS connection (asset check at t=50s, OTA check at t=60s)

## 0.24.2 (2026-06-11)
### Satellite Firmware
* Fixed: `PSA_ERROR_INSUFFICIENT_MEMORY` (-141) during TLS handshake — `hannah_asset` delays 10s at boot before manifest fetch and uses `esp_crt_bundle_attach`; OTA unmounts SPIFFS before download to free heap for PSA signature verification

## 0.24.1 (2026-06-11)
### Satellite Firmware
* Fixed: OTA TLS handshake failed with `MBEDTLS_ERR_X509_CERT_VERIFY_FAILED` — replaced hardcoded intermediate CA PEM with `esp_crt_bundle_attach` in both version check and OTA download

## 0.24.0 (2026-06-11)
### Satellite Firmware
* Changed: asset server URL and token moved from compile-time Kconfig constants to NVS (with sdkconfig fallback) — adapter can now provision them during initial flash
* Changed: `hannah_asset` uses `hannah_config_get()` instead of `CONFIG_HANNAH_ASSET_SERVER_URL` / `CONFIG_HANNAH_ASSET_SERVER_TOKEN`; asset URL is now logged on each manifest fetch

## 0.23.16 (2026-06-10)
### Satellite Firmware
* Fixed: PDM microphone channel selection was wrong — code read right channel (SEL=VDD, index 1) but Rev 4 PCB has SEL=GND (left channel, index 0); switched to `s16[i * 2]`
* Fixed: PDM gain factor x256 caused hard clipping; tuned to x64 which gives usable speech levels without distortion

## 0.23.15 (2026-06-10)
### Satellite Firmware
* Fixed: `mic_task` could starve `IDLE0` on CPU0 and trigger the task watchdog — every loop iteration now yields via `vTaskDelay(1)` instead of relying solely on `i2s_channel_read()` blocking (or `taskYIELD()`)

### Hannah Core
* Fixed: audio received via UDP from a satellite in capture/sampling mode was processed through the normal STT/LLM/TTS pipeline instead of being routed to the capture stream — `process_audio_udp` now checks `is_captured()` like the gRPC path
* Fixed: a satellite could get stuck in capture/sampling mode after a Hannah Core restart because the retained MQTT sampling-mode flag survived independently of Hannah's in-memory capture state — Hannah now republishes `sampling: false` (retained) for any newly (re)connected satellite it doesn't consider captured

## 0.23.14 (2026-06-10)
### Satellite Firmware
* Added: configurable status LED — `HANNAH_STATUS_LED_ENABLED` / `HANNAH_STATUS_LED_GPIO` (Kconfig, default GPIO 18 for Rev 4); turned on as early as possible in `app_main`

## 0.23.13 (2026-06-09)
### Hannah Core
* Fixed: `_on_satellite_change` callback crashed with `TypeError` when a proxy satellite registered — `grpc_server.py` was passing `{device: {"room": ..., "addr": ...}}` but the callback expected `{device: room_string}`; snapshots now consistently use `{device: room_string}` matching the UDP server format

## 0.23.12 (2026-06-09)
### Hannah Core
* Fixed: proxy satellites always had empty `address` state in ioBroker — `SatelliteRegistration` proto now carries the satellite IP; `grpc_server.py` stores it in `_proxy_satellites`; `get_satellites` lambda uses new `proxy_satellites_full()` to include the address

### Hannah Proxy
* Changed: `SatelliteChangeCallback` now includes `address` (satellite IP); passed through `NotifySatelliteRegistered` to Hannah Core
* Added: `udp.Server.RegisteredDevicesFull()` — returns `{device: SatelliteInfo{Room, Address}}` for re-notify on reconnect

## 0.23.11 (2026-06-09)
### CI
* Fixed: upload jobs failed with SSL certificate error — `alpine` container has no internal CA; added `echo insecure >> ~/.curlrc` in `.upload.before_script` so all curl calls skip TLS verification for the self-signed Update-Server

## 0.23.10 (2026-06-09)
### Hannah Core
* Fixed: `GetSatellites` response always returned empty `address` field — `get_satellites` lambda now uses new `udp_server.registered_devices_full()` which includes the actual `ip:port` address
* Added: `UdpServer.registered_devices_full()` — returns `{device: {room, addr}}` with address as `ip:port` string

### Satellite Firmware
* Added: `HANNAH_MIC_TYPE_NONE` Kconfig option — disables microphone input (mic_init, mic_task, sampling/PTT callbacks skipped); LED set to IDLE directly at init
* Added: `HANNAH_SPEAKER_ENABLED` Kconfig bool (default y) — disables I2S speaker output and TTS callbacks when set to n; allows building pure sensor-node firmware

## 0.23.9 (2026-06-07)
### Satellite Firmware
* Fixed: BLE watchlist retained MQTT message was dropped on boot because `hannah_ble_init()` registers the callback after MQTT has already connected and received the retained payload; `hannah_net` now caches the payload and delivers it immediately when the callback is registered

## 0.23.8 (2026-06-07)
### Hannah Core
* Changed: `udp_server` — added 300 ms delay before sending `tts_end` to satellite; prevents hard audio cutoff caused by `tts_end` arriving before the last PCM UDP packets are received and queued on the satellite

### Hannah Proxy
* Changed: `udp.SendTTS` — added 300 ms delay before sending `tts_end`; same reason as above (proxy is the primary TTS path for ESP32 satellites)

### Satellite Firmware
* Fixed: `hannah_audio` warmup loop — `taskYIELD()` (0.23.7) does not yield to `IDLE0` (priority 0) when higher-priority tasks are runnable during boot; replaced with `vTaskDelay(1 ms)` so the loop actually blocks and lets `IDLE0` reset the task watchdog

## 0.23.7 (2026-06-07)
### Satellite Firmware
* Fixed: `hannah_audio` warmup loop — `continue` bypassed the `taskYIELD()` at the end of `mic_task`'s main loop, starving `IDLE0` for the full 5-second warmup period and causing a task watchdog warning at boot; added `taskYIELD()` inside the warmup block before `continue`

## 0.23.6 (2026-06-07)
### Satellite Firmware
* Fixed: `hannah_ota` / `hannah_audio` — TFLite wakeword inference in `mic_task` (CPU 0) prevented `IDLE0` from running during OTA download, triggering repeated task watchdog warnings and potentially stalling HTTPS reads; `ota_update_task` now calls `hannah_audio_pause_wakeword()` before starting the download, causing `mic_task` to sleep 50 ms per iteration instead of running inference

## 0.23.5 (2026-06-07)
### Satellite Firmware
* Fixed: `hannah_audio` — `mic_task` and `speaker_task` both ran unpinned on CPU 0; TFLite inference starved the speaker task causing `i2s_channel_write` silence drain to time out, resulting in TTS audio cutoff at end; `mic_task` now pinned to CPU 0, `speaker_task` to CPU 1; silence drain timeout changed to `portMAX_DELAY`

## 0.23.4 (2026-06-07)
### Satellite Firmware
* Fixed: `mic_task` — added `taskYIELD()` at end of each loop iteration; TFLite wakeword inference was monopolizing CPU 0 and starving IDLE0, causing repeated task watchdog triggers (especially during concurrent OTA download)

## 0.23.3 (2026-06-07)
### Hannah Core
* Fixed: BLE tag locations were not delivered to ioBroker adapter after reconnect — `_on_agent_connect` now pushes all current locations via `ble_engine.get_current_locations()` as a resync on every adapter connect
* Added: `BleLocationEngine.get_current_locations()` — returns last known location for all configured tags

### Satellite Firmware
* Fixed: `hannah_audio` speaker task — TTS playback was cut off at the end; on `audio_end` only 320 bytes of silence were written which was insufficient to drain the I2S DMA pipeline (8 × 640 frames × 2 bytes = 10240 bytes); now writes full DMA-sized silence buffer to ensure all buffered audio is clocked out

## 0.23.2 (2026-06-06)
### Hannah Core
* Fixed: `tool_agent` — LLM had no access to current date/time; now injected into system prompt on every run (weekday, date, time); prevents wrong guesses for questions like "Welcher Tag ist heute?"

## 0.23.1 (2026-06-06)
### Hannah Core
* Fixed: startup crash — `main.py` log statement referenced removed `topic_prefix_write` attribute on `ResidentsClient`; replaced with `topic_prefix_read`

## 0.23.0 (2026-06-06)
### Satellite Firmware
* Added: `hannah_sd` component — SPI Micro-SD card support via `esp_vfs_fat`; mounts at `/sdcard`; enabled per Kconfig (`CONFIG_HANNAH_SD_ENABLED`); no-op stubs when disabled
* Added: `sdkconfig.defaults.rev4` enables SD card (GPIO 4/5/6/7) and BME680

### CI
* Added: `build:esp32:rev4` — builds firmware with `sdkconfig.defaults.rev4`
* Added: `upload:esp32:rev4` — uploads Rev4 firmware to channel `satellite-esp-stable`

## 0.22.2 (2026-06-05)
### Hannah Core
* Fixed: `tool_agent` — `speak()` is now a terminal tool; the loop returns immediately after dispatching `speak` without waiting for a further LLM round-trip; previously the loop could exhaust `_MAX_ITERATIONS` before `speak` was ever called, causing the fallback "Das habe ich leider nicht verstanden." instead of the generated answer
* Changed: `_MAX_ITERATIONS` raised from 3 to 5 — allows more complex tool-use flows (e.g. multi-device commands, intermediate queries) without hitting the limit prematurely
* Added: TTS result logging in `_handle_satellite_audio` — logs byte count and sample rate on success, or a warning when `synthesize()` returns nothing

## 0.22.1 (2026-06-04)
### Hannah Core
* Fixed: `process_notification` (notify/alert severity) was sending raw Azure TTS (24kHz) to satellites without resampling — audio played at 67% speed with noticeably lower pitch; now resampled to 16kHz via `_resample_to_16k` before `_send_audio`
* Fixed: `_on_agent_satellite_control` (ioBroker announcements) had the same missing resample, and called `udp_server.send_tts` directly instead of `_send_audio` — breaking proxy-connected satellites

## 0.22.0 (2026-06-04)
### Hannah Core
* Added: LLM rephrase for announcements — `_rephrase_text()` helper shared by trigger engine and satellite control handler; falls back to original text when LLM is unavailable or fails
* Added: `rephrase: true` field in `triggers.yaml` — TriggerEngine passes `say` text through LLM before TTS when set
* Added: `AgentSatelliteControl.announcement_rephrase` gRPC field — adapter can request LLM reformulation per announcement

### Proto
* Added: `announcement_rephrase` (field 8) to `AgentSatelliteControl.oneof control` — speak announcement with LLM rephrase applied before TTS

## 0.21.3 (2026-06-04)
### Telegram
* Added: Automated test suite for the Telegram bot (`telegram/tests/test_app.py`, 28 tests) — covers private-chat guard, trust-level checks, link/unlink flow, `/start` welcome message, free-text command dispatch, and car-state formatting; integrated as `test:telegram` CI job

### VoiceID
* Refactored: `voiceid/app.py` — moved all module-level side effects (model loading, argparse, `os.makedirs`) out of import scope into a `create_app()` factory and FastAPI lifespan handler; routes extracted to `APIRouter`; `get_embedding()` now accepts classifier as parameter instead of using a global
* Added: Automated test suite for the VoiceID service (`voiceid/tests/test_app.py`, 16 tests) — covers embedding extraction, profile enrollment (new + blending), identification with threshold logic, startup profile sync from disk to RAM, and config-file threshold overrides; integrated as `test:voiceid` CI job (Python 3.11, torch CPU-only, no speechbrain install required)

## 0.21.2 (2026-06-04)
### Hannah Core
* Changed: Asset manifest is now fetched without namespace filter (`GET /manifest`) so asset metadata can be queried generically across all namespaces

## 0.21.1 (2026-06-03)
### Satellite Firmware
* Fixed: `hannah_asset` — asset server HTTPS requests failed due to missing CA certificate; added Thawte TLS RSA CA G1 cert to `fetch_manifest()` and `download_asset()` (same CA as OTA)

## 0.21.0 (2026-06-03)
### Hannah Core
* Added: Asset manifest fetch at startup — reads `duration_s`, `sample_rate`, `channels`, `bits_per_sample` from asset server manifest (`asset_server.url` + `asset_server.token` in `config.yaml`)
* Changed: Timer alert now plays `timer_jingle` asset on all target satellites before TTS; TTS is pre-synthesized so jingle and announcement are sequenced precisely (`play_asset` → sleep `duration_s + 0.1 s` → TTS PCM)
* Added: `mqtt_handler.publish_play_asset(device, asset_id)` — publishes `{"asset_id": …}` to `hannah/satellite/{device}/play_asset`

### Satellite Firmware
* Added: `hannah_asset` component — fetches asset manifest at boot, downloads/caches WAV files in SPIFFS (sha256-based cache validation via NVS), plays WAV assets on demand with proper WAV chunk scanning
* Added: MQTT topic `hannah/satellite/{device}/play_asset` — payload `{"asset_id": …}` triggers async WAV playback via `hannah_audio_play()`
* Added: `HANNAH_ASSET_SERVER_URL` + `HANNAH_ASSET_SERVER_TOKEN` Kconfig options (set via CI as `sdkconfig.defaults.ci`)
* Changed: SPIFFS partition expanded from 1.9 MB to 9 MB

## 0.20.0 (2026-06-01)
### Hannah Core
* Fixed: Notifications played back at wrong pitch — Azure TTS output (24 kHz) was not resampled before sending to satellite, causing 2/3-speed playback and a noticeably deeper voice; use `_resample_to_16k()` helper
* Added: `TriggerPlink` gRPC RPC — Hannah plays an 880 Hz plink tone on the satellite and holds virtual PTT for `record_duration` seconds so the collector can trigger guided Hey-Hannah recordings remotely
* Added: `plink.py` — generates 880 Hz sine plink PCM (200 ms, 16 kHz, 16-bit mono) or loads from a WAV file
* Added: `_on_trigger_plink` in `main.py` — plays plink audio on satellite, then holds virtual PTT for the requested duration
* Added: `SatelliteCaptureRequest.sample_type` field (`"noise"` or `"hey_hannah"`) — collector signals which training mode to use
* Changed: `mqtt_handler.publish_sampling_mode` now sends JSON payload `{"enabled": …, "type": …}` instead of plain boolean; added `publish_virtual_ptt` to toggle `hannah/satellite/{device}/ptt`
* Changed: `grpc_server.RequestSatelliteCapture` forwards `sample_type` to the capture callback

### Hardware (PCB Rev. 4)
* Added: SD card slot (SPI)
* Changed: LED data pin moved from GPIO 5 to GPIO 3; `sdkconfig.defaults.rev4` updated accordingly

### Satellite Firmware
* Added: `sdkconfig.defaults.rev4` — build target for PCB Rev. 4 with updated GPIO assignments
* Added: Virtual PTT via MQTT `hannah/satellite/{device}/ptt` — `"true"`/`"1"` activates PTT, `"false"`/`"0"` releases; allows Hannah Core to trigger recordings without a physical button press
* Added: `hey_hannah` capture sub-mode — in this mode the mic streams only while PTT is active (physical or virtual) and sends `audio_end` on PTT release; pre-flush clears any buffered noise before each recording; speaker output is allowed so the plink tone is audible
* Changed: `noise` capture sub-mode behaviour unchanged — continuous auto-flush every 5 s, pre-flush on PTT press; speaker is muted in this mode
* Changed: `hannah/satellite/{device}/sampling` payload is now JSON with `enabled`/`type` fields
* Changed: capture LED animation is now more distinctly purple (higher blue component relative to red)
* Added: LED state transition logging — each state change is logged (`LED X → Y`)

## 0.19.0 (2026-05-31)
### Hannah Core
* Added: Wakeword Collector integration — satellites can be put in capture mode via gRPC; Hannah relays raw PCM to the collector instead of STT pipeline; DND is set automatically; MQTT `hannah/satellite/{device}/sampling` notifies satellite firmware (firmware-side pending)
* Added: `RequestSatelliteCapture`, `ReleaseSatelliteCapture`, `StreamSatelliteAudio` gRPC RPCs for wakeword training data capture
* Added: `SatelliteCaptureRequest`, `SatelliteCaptureResponse`, `SatelliteAudioChunk` gRPC messages

### Satellite Firmware
* Added: Sampling mode via MQTT `hannah/satellite/{device}/sampling` — when `{"enabled":true}` is received, speaker output is blocked, any running TTS queue is cleared, and LED shows `LED_STATE_CAPTURE` (purple pulsing); restored to normal on `{"enabled":false}` or auto-release

## 0.18.6 (2026-05-31)
### Hannah Core
* Fixed: NLU timer trigger now recognizes Whisper-truncated "erinner" (prefix match instead of exact set match)
* Fixed: Announcements (proactive / timer / trigger) played back at wrong pitch — Azure TTS output (24 kHz) was not resampled before sending to satellite, causing 2/3-speed playback and a noticeably deeper voice; extracted `_resample_to_16k()` helper used by both announcement and satellite audio paths

## 0.18.5 (2026-05-31)
### Hannah Core
* Fixed: `SetTimer` intent not handled in gRPC/satellite audio path (`_handle_text`) — fell through to `iobroker.execute()` causing "Kein Raum erkannt" warning and no timer being set; timer is now created correctly from all input paths

### Proto
* Added: `TimerNotReady` message — Hannah can signal a temporary degraded state (e.g. ioBroker disconnected) over the `TimerConnect` stream; Timer Service should hold `TimerFired` events until a subsequent `TimerReady` is received; Hannah Core does not yet send this message

## 0.18.4 (2026-05-31)
### Hannah Core
* Fixed: NLU responses no longer contain raw internal category names (e.g. "light") — mapped to German labels: Lichter, Steckdosen, Klimageräte, Rollläden, Sensoren

## 0.18.3 (2026-05-31)
### Satellite Firmware
* Fixed: false wakeword trigger immediately after boot — wakeword frontend is now fed audio during the 5-second warmup period so model state is fully initialized before detection begins; previously the uninitialized frontend caused a consistent false trigger ~200 ms after warmup ended
* Fixed: TTS audio chunks silently dropped during playback — speaker queue depth increased from 8 to 256 entries; send is now blocking with a 2-second timeout to apply backpressure instead of discarding chunks
* Fixed: OTA reliability — mbedTLS TLS IN buffer reduced from 16 KB to 8 KB via `MBEDTLS_ASYMMETRIC_CONTENT_LEN`; frees internal RAM headroom consumed by DSR_16S PDM downsampling

## 0.18.2 (2026-05-31)
### Satellite Firmware
* Fixed: PDM microphone channel selection corrected — SPH0641LU4H-1 with SEL=VDD outputs on the right channel (index 1), not left; previously all captured audio was zero
* Fixed: PDM digital gain increased from default (1×) to 8× — default gain produced inaudibly quiet signal for the SPH0641LU4H-1

## 0.18.1 (2026-05-31)
### Hardware (PCB Rev. 4)
* Changed: SW1 (EN) tap rerouted closer to ESP pin for more clearance to C3/C4/R3
* Changed: SW2 (IO0) rerouted for more clearance between R6 and button body; AMP_LRC/AMP_BCLK traces rerouted away from button area
* Changed: UART connector (J4) TX/RX swapped to match adapter pinout without crossing cables

### Satellite Firmware
* Changed: wakeword enable/disable is now a runtime decision — `CONFIG_HANNAH_WAKEWORD_ENABLED=y` compiles in the wakeword code, NVS `wakeword_enabled` decides at boot whether wakeword or PTT mode is active
* Added: VAD silence timeout (`vad_silence_ms`) is now stored in NVS and configurable via web UI (200–10000 ms); default remains 1500 ms
* Fixed: after wakeword detection, VAD cannot end the stream for the first 2 seconds — prevents cutoff during the natural pause between wakeword and spoken command

## 0.18.0 (2026-05-30)
### AutoDeploy
* Changed: revision field from update server is now compared alongside version — same version but higher revision triggers redeployment; revision is persisted in state file
* Changed: download URL is now taken from the server response `url` field; `device=<id>` query parameter added to download requests

### Satellite Firmware
* Changed: OTA now compares server `revision` field in addition to version — same version but higher revision triggers an update; revision is persisted in NVS after successful OTA
* Changed: OTA download URL now includes `device=<id>` query parameter (matching the `/latest` check request)

## 0.17.0 (2026-05-30)
### Hardware (PCB Rev. 4)
* Changed: PCB revision bumped from 3 to 4
* Fixed: ALPS SKRPABE010 button LCSC part numbers corrected for all 6 buttons (Mute, Vol-, Vol+, PTT, EN, IO0)
* Changed: ESP32-S3-WROOM-1U LCSC part number updated to N16R8 variant (was accidentally N16R2 in Rev. 3)

### Satellite Firmware
* Added: LED animations per state — BOOT rotating white, WAKE pulsing blue, STREAM rotating blue arc, SPEAK green breathing, MUTE dim static red, ERROR fast red blink; driven by a 50 Hz FreeRTOS task

## 0.16.3 (2026-05-30)
### Satellite Firmware
* Fixed: LED stays in SPEAK (green) state until the speaker task has finished playing all TTS audio — previously `status=idle` from the server would immediately reset the LED while chunks were still queued for playback

## 0.16.2 (2026-05-30)
### Hardware (PCB Rev. 3)
* Fixed: ALPS SKRPABE010 footprint corrected — contacts were bridged on the wrong axis causing EN and IO0 to be permanently pulled to GND; all 6 button footprints (EN, IO0, Mute, Vol-, Vol+, PTT) replaced
### Satellite Firmware
* Added: `sdkconfig.defaults.rev2` — build target for PCB Rev. 2 (PDM mics, BMP280, external LED ring, corrected GPIO assignments)

## 0.16.1 (2026-05-30)
### Hannah Core
* Added: INFO log in `Notify` gRPC handler — logs severity and text on every received notification to diagnose duplicate delivery

## 0.16.0 (2026-05-30)
### Hannah Core
* Changed: `SetTimer` voice intent now routes through the external Timer Service — generates a UUID timer_id, persists metadata in `HannahTimerStore`, and calls `grpc_servicer.timer_create()`; in-process `TimerManager` removed for timer commands
* Added: NLU label extraction for timer commands — "erinnere mich in X Minuten an Y" triggers `SetTimer` and extracts Y as label; response includes label if detected ("Timer für 40 Minuten gesetzt: Spazierengehen.")

## 0.15.0 (2026-05-29)
### Hannah Core
* Added: `say` action type in routines — routines can now speak text via TTS as part of their action sequence; optional `room` parameter (default: `all`)
* Added: Hannah Timer Service gRPC interface — `TimerConnect` bidirectional stream, `TimerReady` signal sent after ioBroker device snapshot; `HannahTimerStore` (SQLite) persists timer metadata (label, room, roomie_id, fire_at) locally; on `TimerFired`, Hannah looks up metadata and plays TTS announcement

## 0.14.7 (2026-05-29)
### Hannah Core
* Changed: notification reformulation prompt now uses Hannah's persona ("24-jährige Mitbewohnerin") and per-severity tone tuning for more natural, less formal spoken notifications

## 0.14.6 (2026-05-29)
### Hannah Core
* Fixed: `get_active_devices` now correctly uses the `on` state as the sole indicator of activity when present — previously a non-zero `level` alone would mark a device as active even if `on=false` (e.g. lights with a saved level but physically off)
* Changed: `get_active_devices` output now includes total device count (e.g. "5 von 47") to give the LLM context for relative statements

## 0.14.5 (2026-05-29)
### Hannah Core
* Added: INFO-level payload size logging in tool agent — logs message count and character count per iteration, and tool result size after each dispatch
* Fixed: tool agent now blocks duplicate tool calls (same name + same arguments) server-side and returns an error forcing the LLM to call `speak` instead of looping
* Changed: tool agent query tools now return human-readable text instead of raw JSON — `get_all_devices`, `get_active_devices`, `get_devices_in_room`, `get_devices_by_category`, `get_device_state` all return formatted strings that LLMs can directly use for spoken answers

## 0.14.4 (2026-05-29)
### Hannah Core
* Added: `get_active_devices` tool — returns only active devices (on=true or level>0) with current state; ideal for "was läuft gerade?"
* Added: `get_devices_in_room(room)` tool — returns devices in a specific room with state keys; for targeted queries and control
* Added: `get_devices_by_category(category)` tool — returns devices of a category with state keys; for bulk actions like "alle Lichter aus"
* Changed: `get_all_devices` now returns only id/name/room/category (no state_keys, no current) — pure discovery tool to reduce token usage

## 0.14.3 (2026-05-29)
### Hannah Core
* Fixed: `get_all_devices` tool no longer returns `current` state values — payload was too large for LLM token budget; use `get_device_state` for current values

## 0.14.2 (2026-05-29)
### CI
* Fixed: `upload:core` CI job did not include `main.py` in the release archive — services deployed via autodeploy were missing the entry point

## 0.14.1 (2026-05-29)
### Hannah Core
* Fixed: LLM tool calls in `chat_with_tools` could hang indefinitely on large Ollama responses — added explicit `stream: false` to payload and explicit `(connect, read)` timeout tuple
* Added: INFO-level iteration logging in `tool_agent.run()` for observability in journalctl

## 0.14.0 (2026-05-28)
### Hannah Core
* Added: LLM Tool Agent (`hannah/tool_agent.py`) — handles complex requests via OpenAI-compatible function-calling; tools: `get_all_devices`, `get_device_state`, `set_device_state`, `speak`
* Changed: `Unknown` and `Smalltalk` intents now both route to the Tool Agent instead of bare `llm.chat()`
* Changed: `LLMClient` — added `chat_with_tools(messages, tools)` method; `OpenAICompatibleLLM` implements native tool calling, other backends fall back to regular `chat()`
* Changed: `get_all_devices` tool now returns `state_keys` (list of available state names) and `current` (actual values) separately to prevent LLM misreading key names as values
* Added: tool usage rules appended to system prompt in every Tool Agent call (always use `speak`, no repeated tool calls)

### Scripts
* Added: `scripts/hannah_shell.py` — interactive text shell for testing NLU/Tool Agent via gRPC `SubmitText` without Telegram

## 0.13.1 (2026-05-27)
* Fixed: MQTT-triggered mute/unmute now correctly updates the LED state (was only updated on button press)

## 0.13.0 (2026-05-27)
### Proto
* Changed: `AgentSatelliteUpdate` — added optional `volume` (int32) and `mute` (bool) fields
* Changed: `AgentSatelliteControl` — added optional `device_id` (string) for per-satellite targeting

### Hannah Core
* Changed: satellite volume/mute now reported via `volume/state` / `mute/state` topics (satellite-initiated); Hannah subscribes to these instead of command topics
* Changed: `_on_agent_satellite_control` for volume/mute now publishes `volume/set` / `mute/set` commands to satellites (previously published state topics)
* Added: mute room-replication — when one satellite reports a mute state change, Hannah replicates `mute/set` to all satellites in the same room
* Added: global volume command (`hannah/volume`) now sends `volume/set` to all satellites
* Removed: PCM volume scaling in Hannah Core (`_scale_pcm`, `_get_volume`); volume is applied satellite-side

### Satellite Firmware
* Added: Vol+/Vol- buttons now publish new level to `hannah/satellite/<device>/volume/state`
* Added: subscribe to `hannah/satellite/<device>/volume/set`; received value is applied to local playback volume
* Added: change detection in `hannah_net_set_mute()` — state is only published if it actually changed

## 0.12.5 (2026-05-27)
### CI
* Fixed: `skip_if_unchanged` calls were removed from all upload jobs for the v0.12.4 release to force a full upload — this commit restores them

### Hannah Core
* Removed: all ioBroker-facing MQTT publishes (transcript, speaking, satellite_status, rooms, online, global dnd/mute, text commands); ioBroker communication is now exclusively via gRPC
* Removed: REST API client code from `iobroker.py` (`requests`, `_get_enum`, `_get_objects`); device data is now fully gRPC-driven
* Removed: `publish_fn` parameter from `ResidentsClient` (unused)
* Removed: PCM volume scaling in Hannah Core (`_scale_pcm`); volume will be applied satellite-side
* Kept: per-satellite MQTT for volume/mute/dnd control, announcements/notifications, OTA/BLE/sensors

### Satellite Firmware
* Fixed: mute command topic changed from `…/mute` to `…/mute/set`; state feedback published on `…/mute/state`
* Fixed: mute value parsing now accepts `true`/`false` in addition to `1`/`0`

## 0.12.4 (2026-05-26)
### CI
* Fixed: Upload jobs fetched tags without pruning deleted ones (`--tags`) — replaced with `--tags --prune --prune-tags` so stale tags in the runner cache no longer cause `skip_if_unchanged` to compare against a non-existent previous tag

## 0.12.3 (2026-05-26)
### AutoDeploy
* New: Generates a persistent device ID (UUID v4) on first start, stored in `/var/lib/hannah/autodeploy-device-id`; sent as `?device=<uuid>` with every `/latest` poll to enable accurate per-installation device counting on the Update Server

### Satellite Firmware
* New: Sends `?device=<device_id>` (NVS-backed device ID) with every OTA `/latest` request to enable accurate per-device counting on the Update Server

## 0.12.2 (2026-05-26)
### CI
* Fixed: `skip_if_unchanged` caused SIGPIPE (exit 141) — replaced `grep | head -1` with `awk`

## 0.12.1 (2026-05-26)
### AutoDeploy
* Fixed: `UnboundLocalError` for `current` variable in `deploy_component()` — `state.get(name)` was called after `get_latest()` which already needed it

### CI
* Changed: Renamed job groups for clarity — `test:python` → `test:core`, `test:go` → `test:proxy`, `test:satellite` → `test:satellite:pi`, `build:amd64/arm64` → `build:proxy:amd64/arm64`, `publish:amd64/arm64` → `publish:proxy:amd64/arm64`
* Changed: `PACKAGE_NAME` variable renamed to `PROXY_PACKAGE_NAME`
* New: Upload jobs skip the Update-Server upload if the component directory has no changes since the previous release tag (`skip_if_unchanged` function in `.upload`)

## 0.12.0 (2026-05-25)
### Satellite Firmware
* Changed: OTA update-check requests now include `?current=<version>` so the Update-Server can track installed version distribution

### AutoDeploy
* Changed: `get_latest()` now passes the currently installed version as `current` query parameter to the Update-Server

## 0.11.0 (2026-05-25)
### Hannah Core
* New: Connect sound — Hannah plays `core/sounds/satellite_connected.wav` (if present) on the satellite when it registers via the proxy
* New: Timer — "Hannah, stelle einen Timer auf 20 Minuten" fires TTS on the source satellite when the countdown ends
* New: Alarm — "Hannah, stelle einen Wecker auf 7 Uhr 30" sets a persistent alarm that fires on the configured `alarm.satellite` (falls back to source satellite); survives Hannah restarts via `alarms.json`

## 0.10.0 (2026-05-25)
### Hannah Core
* New: `climate` device type — NLU recognizes `SetMode` (`SetMode`: cool/heat/dry/fan_only/auto) and `SetFanSpeed` (low/medium/high/auto) intents; German compound words ("Klimaanlage", "Klimaanlagen") map to `climate` category
* New: Climate device query answers report on/off state, operating mode, current temperature, target temperature, and fan speed

## 0.9.1 (2026-05-25)
### Satellite Firmware
* Fixed: `ota_channel` buffer increased from 16 to 32 bytes — channel names longer than 15 characters (e.g. `satellite-esp-dev`) were silently truncated

### CI
* Changed: GitLab Generic Registry publish jobs and Hannah Update-Server upload jobs split into separate stages (`publish` and `upload`) with clearer naming
* Changed: Upload jobs use `{latestTag}-dev` (e.g. `v0.9.0-dev`) as version fallback when `FORCE_PUBLISH` runs without a tag

### AutoDeploy
* New: `autodeploy.py` — polls Update-Server channels and deploys updates; supports self-update
* New: `install.sh` — downloads and installs the AutoDeploy agent from the Update-Server, sets up Python venv and systemd service
* Fixed: State was not saved before service restart, causing an infinite redeploy loop on self-update
* Fixed: Replacing a running executable raised `ETXTBSY` — file is now unlinked before copy
* Changed: `hannah-autodeploy.service` sets `REQUESTS_CA_BUNDLE` to system trust store

## 0.9.0 (2026-05-24)
### Satellite Firmware
* New: `hannah_sensors` now publishes readings every 30s to `hannah/satellite/{device}/sensors` (retained, QoS 1); JSON payload includes `temperature`, `pressure`, `humidity`, and optionally `gas_resistance` (BME680 only)

### Hannah Core
* New: Subscribes to `hannah/satellite/+/sensors`; forwards readings to the ioBroker adapter via `AgentSensorUpdate` gRPC command

### Proto
* New: `AgentSensorUpdate` message — carries `device`, `temperature`, `pressure`, `humidity`, `gas_resistance`
* New: `sensor_update = 8` added to `AgentCommand.command` oneof

## 0.8.3 (2026-05-24)
### Satellite Firmware
* New: OTA rollback — `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` enabled; firmware marks itself valid after first successful MQTT connection, otherwise the bootloader automatically reverts to the previous partition on the next reboot
* New: OTA rollback loop prevention — after a rollback, the previously invalid partition version is compared against the server's latest; if they match, `ota/failed` (with `reason: rollback`) is published instead of `ota/pending` to prevent an update loop
* New: OTA channel config (`HANNAH_OTA_CHANNEL`) — Kconfig string, NVS-backed, configurable via WebUI; appended as `?channel=<value>` to the update server request; devkit default: `dev`
* New: Dev-channel semver comparison — when channel is not `stable`, the git-describe commit offset is compared when the semver base is equal (e.g. `0.8.2-12` > `0.8.2-11`)
* Fixed: git-describe offset parsing in semver comparison was broken for versions without a patch-level dot suffix — replaced manual loop with `strchr` (regression introduced in 0.8.1)

### Scripts
* New: `scripts/upload-dev-firmware.ps1` — builds (devkit config) and uploads firmware to the OTA server; supports `-NoBuild`, `-Channel`, `-List`, `-Delete`, `-Version`; reads credentials from `.env`

### CI
* Changed: firmware is now uploaded to the `stable` channel (`?channel=stable`) instead of the implicit default

## 0.8.2 (2026-05-24)
### Satellite Firmware
* Fixed: `.history_trim` VS Code Local History directory was accidentally tracked as a git submodule — removed from index and added to `.gitignore`; fixes CI submodule init failure

## 0.8.1 (2026-05-24)
### Satellite Firmware
* Changed: OTA version check uses semver comparison instead of strict string equality — downgrades and git-describe suffixes (e.g. `0.8.0-1-gabcdef`) are no longer treated as available updates

## 0.8.0 (2026-05-24)
### Satellite Firmware
* New: `hannah_ble` component — passive BLE scanner for indoor localisation; MAC-based watchlist from `hannah/satellite/{device}/ble/watchlist`; RSSI reports to `hannah/satellite/{device}/ble/report`; rate-limited per MAC (Kconfig: `HANNAH_BLE_REPORT_INTERVAL_MS`); NimBLE host in dedicated FreeRTOS task; BLE/WiFi coexistence via `CONFIG_ESP_COEX_SW_COEXIST_ENABLE`

### Hannah Core
* New: `ble_location.py` — `BleLocationEngine` aggregates per-satellite RSSI reports per BLE tag; "strongest RSSI wins" room determination; configurable stale timeout; fires `on_location_change` callback on every room transition
* New: Subscribes to `hannah/satellite/+/ble/report`; routes reports to `BleLocationEngine`
* New: Publishes BLE watchlist (retained) to each satellite on connect via `publish_ble_watchlist()`
* New: On location change, publishes `hannah/ble/{label}/location` (retained JSON) and pushes `AgentBleUpdate` to ioBroker adapter

### Proto
* New: `AgentBleUpdate` message — carries `label`, `mac`, `room`, `satellite`, `rssi` for the `AgentConnect` stream
* New: `ble_update = 7` added to `AgentCommand.command` oneof

### ioBroker Adapter
* New: `BleWatcher` class — handles `ble_update` commands; creates/updates `hannah.0.ble.{label}.{room,satellite,rssi}` states on first update

## 0.7.0 (2026-05-23)
### Satellite Firmware
* New: `hannah_ota` publishes firmware version to `hannah/satellite/{device}/firmware` (retained, QoS 1) after boot — enables firmware visibility in ioBroker
* Changed: OTA MQTT topics renamed from `hannah/{device}/ota/*` to `hannah/satellite/{device}/ota/*` for consistency with the satellite topic namespace
* Fixed: OTA-pending MQTT handler never fired due to wrong topic-part count (was checking `len==3`, correct is `len==5`)

### Hannah Core
* New: Subscribes to `hannah/satellite/+/firmware`; stores firmware version per satellite and fires `satellite.firmware` gRPC event (`SubscribeEvents` stream)
* New: Pushes firmware version and `update_available` flag to the ioBroker adapter via `AgentFirmwareEvent` over the `AgentConnect` stream
* New: `TriggerFirmwareUpdate` gRPC RPC — triggers immediate OTA for a satellite (bypasses residents check), called by the ioBroker adapter `update_now` button
* New: On `ota/pending` event, `update_available=true` is pushed to the adapter immediately so the ioBroker state updates without waiting for a full reconnect
* Changed: OTA publish/subscribe topics updated to `hannah/satellite/{device}/ota/*`
* Fixed: MQTT topic typo `hannah/satelite/` → `hannah/satellite/` throughout `mqtt_handler.py` and `config.example.yaml` — mute/volume/dnd/announcement/status/online topics now match the firmware's subscription patterns

### Proto
* New: `FirmwareEventProto` message — carries `device` and `version` for the `SubscribeEvents` stream
* New: `AgentFirmwareEvent` message — carries `device`, `version`, and `update_available` bool for the `AgentConnect` stream
* New: `TriggerFirmwareUpdateRequest` message and `TriggerFirmwareUpdate` RPC

## 0.6.0 (2026-05-23)
### Hardware
* New: Hardware Rev 3 PCB — iterates on Rev 2; ESP32-S3-WROOM-1U (external U.FL antenna, no keep-out conflict with LED ring); hierarchical schematic (Audio, Supplementals, Power_Control sub-sheets); AHT20 humidity sensor integrated directly on board sharing BMP280 I2C bus; LD2410 24GHz radar presence sensor header (5-pin: 5V, GND, TX, RX, OUT); 24× SK6812MINI-E LED ring directly on PCB at 3.3V (replaces JST connector + SN74AHCT125D level shifter); BMP280 I2C bus unified with shared SDA/SCL (was on separate GPIOs); I2C pull-up resistors moved to root sheet; fixed mic power circuit bug (R10 was on MOSFET drain instead of gate)

### Satellite Firmware
* New: `hannah_config` component — NVS-backed configuration (WiFi credentials, device ID, OTA token/URL); persists across reboots, readable at runtime via `hannah_config_get()`
* New: `hannah_webserver` component — HTTP setup UI served in AP mode; WiFi network picker (APSTA scan), device settings (device ID, OTA token/URL), live log viewer (ring buffer, 1s polling)
* New: WiFi provisioning — AP fallback when no credentials are stored; APSTA mode for simultaneous scan and serve; credentials written to NVS on submit
* New: Factory reset — hold Mute button at boot to erase WiFi credentials and force AP provisioning mode
* New: `hannah_ota` component — periodic update check against the Hannah update server (`GET /latest` with Bearer token); compares server version against running firmware; publishes `hannah/{device}/ota/pending` when an update is available; flashes new firmware via `esp_https_ota` on `ota/ok` and restarts
* New: Wake-Word detection (microWakeWord, TFLite Micro) — hey_hannah inception model embedded as C array; MicroResourceVariables support for streaming state; TFLite arena allocated from PSRAM
* New: PTT button (GPIO12), Vol+/Vol- buttons (GPIO13/14) with software volume control
* New: Custom partition table — 2MB app partition to fit firmware with embedded TFLite model
* New: BMP280 sensor support — reads temperature and pressure every 30s via I2C (IO8/IO9); logged locally, Hannah channel TBD
* New: Wake-Word VAD: adaptive noise-floor threshold (measured in IDLE, set to 2× noise EMA on trigger); 10s hard streaming timeout as safety net
* Fixed: Wake-Word VAD onset bypassed after wakeword detection — VAD now starts in speaking=1 state so silence detection begins immediately
* Fixed: ESP32 satellite re-registered every ~12s — `udp_connect()` was called on every MQTT reconnect via the retained `hannah/server` message; now skipped if the proxy address is unchanged and the socket is already connected.
* Fixed: ESP32 satellite microphone (INMP441) now uses 32-bit I2S slot width — the previous 16-bit slot width provided only 16 BCLK cycles per channel, too few for the INMP441 to output valid audio (resulting in noise). Stereo→mono downmix updated accordingly.
* Fixed: ESP32 heartbeat interval reduced from 30s to 10s, eliminating a race condition with the proxy's 30s heartbeat timeout that caused continuous re-registration on every heartbeat cycle.
* Fixed: ESP32 MQTT reconnect loop after WiFi drop — random suffix appended to client ID prevents duplicate-ID conflicts while the broker still holds the old TCP session
* Fixed: Mute LED stays red after unmute — LED now immediately returns to idle state when mute is toggled off

### Hannah Core
* New: Hannah Core subscribes to `hannah/+/ota/pending`; sends `ota/ok` immediately if no resident is home, otherwise queues the device and releases all pending updates when the last resident leaves

## 0.5.3 (2026-05-12)
* New: NLU compound word splitting — "Schlafzimmerlicht" is split into "Schlafzimmer Licht" before parsing using known room name words as prefixes and category keywords as suffixes
* Fixed: Telegram `/systemmessages` command threw `AttributeError: system_messages` — generated `hannah_pb2.py` in the telegram service was out of sync with the proto definition and missing field 7 (`system_messages`)

## 0.5.2 (2026-05-09)
* Fixed: Proxy UDP server now clears any open audio session on satellite re-registration — previously a session accumulated indefinitely across ESP reboots (no `audio_end` sent), causing gRPC `ResourceExhausted` on the first successful `audio_end`
* Fixed: Proxy gRPC client max receive message size raised to 32 MB (was 4 MB default)

## 0.5.1 (2026-05-06)
* Fixed: NLU rooms dict was stale after adapter snapshot — NLU was initialized before the device snapshot arrived and never received the updated rooms/devices; room detection failed for all queries
* Fixed: Telegram device menu threw `Can't parse entities` for devices with `_` in category name (e.g. `temperature_sensor`) — category label is now sanitized before use in Markdown
* Fixed: Telegram device menu now shows `Soll` temperature for thermostat devices (`expected` state)

## 0.5.0 (2026-05-06)
* New: `AgentDevice` proto carries a `device_type` field (field 5) — resolved by the adapter from `common.hannah.type`, `common.role`, or function enum IDs; Hannah uses this instead of deriving the category from the state ID path
* New: NLU recognizes `SetTemperature` intent — detects temperature values ("22 Grad", "21,5°C") and maps them to the `expected` state on thermostat devices
* New: Extended device category support — `temperature_sensor`, `thermostat`, `window`, `door`, `blind` (in addition to `light` and `socket`)
* Fixed: Pi satellite `max_heartbeat_wait` reduced from 15s to 5s — prevents heartbeat cycle from exceeding the proxy's 30s timeout window

## 0.4.5 (2026-05-06)
* Fixed: LLM classifier now correctly routes device state queries (e.g. "Welche Lichter sind an?") as COMMAND instead of SMALLTALK, preventing them from bypassing NLU when smalltalk mode is active

## 0.4.4 (2026-05-06)
* New: STT supports Azure Cognitive Services as primary backend — fallback chain: Azure → Remote (faster-whisper-server) → Local

## 0.4.3 (2026-05-04)
* Fixed: Auto-deploy now also pulls `/opt/hannah-telegram` before restarting the Telegram service, so the service actually runs the updated code.

## 0.4.2 (2026-05-04)
* Fixed: Proxy and UDP server now send `reregister` to satellites that send heartbeats or audio without being registered — prevents satellites from silently losing their registration without reconnecting.

## 0.4.1 (2026-05-04)
* Changed: Auto-deploy script now only triggers on new release tags instead of every commit to master.
* Fixed: Auto-deploy `git fetch --tags` now uses `--force` to prevent failure when local tags diverge from remote.

## 0.4.0 (2026-05-03)
* New: `AgentDevice` carries a `floor` field — provided by the ioBroker adapter, resolved from `common.floor` or from the state ID path (known abbreviations: EG, OG, UG, DG, KG, ZG).

## 0.3.1 (2026-05-02)
* Fixed: Release-Cycle

## 0.3.0 (2026-05-02)
* New: Device discovery via gRPC adapter snapshot — Hannah Core no longer queries the ioBroker REST API; device structure (room, name, functions, current value) is pushed by the adapter on connect
* New: Resident snapshot on connect — all known residents are forwarded by the adapter via gRPC, replacing the previous API-based lookup
* New: `_state_cache` for roomless states (weather, car tracker, etc.) — extra-prefix states are cached separately from the device structure and kept up to date via state updates
* New: Satellite offline detection — heartbeat watchdog marks satellites as offline after 30s (3 missed heartbeats), both in Go Proxy and Python UDP server
* Removed: ioBroker REST API dependency — `requests`-based state reads replaced by local cache lookup
* Removed: MQTT transport layer — all ioBroker communication now runs exclusively over gRPC

## 0.2.1 (2026-05-01)
* Fixed: Hannah must detect if a satellite silently went offline

## 0.2.0 (2026-04-30)
* New: AgentNotification — ioBroker adapter sends notifications via gRPC
* New: Notify unary RPC replaces AgentMessage notification stream
* New: compatibility with iobroker.hannah v0.2.0

## 0.1.2 (2026-04-30)
* New: AgentSetResident + AgentSatelliteUpdate, satellite state sync
* New: move residents.set_presence to gRPC
* New: ESP32 satellite end-to-end audio working
* New: AgentTextAnswer — Hannah pushes text command answer to adapter
* New: satellite_control + onConnected fix + _on_satellite_change gRPC push
* New: compatibility with iobroker.hannah v0.1.0
* Fixed: fix timing issue

## 0.1.1 (2026-04-28)
* Fixed: optimistic cache update in control_direct

## 0.1.0 (2026-04-28)
* initial Release
