// hannah-proxy — gRPC satellite audio proxy for Hannah Core.
//
// Receives UDP audio from satellites, forwards to Hannah Core via gRPC,
// and plays the TTS response back to the satellite.
// While connected, Hannah Core disables its own UDP server.
// If the proxy disconnects, Hannah Core re-enables UDP automatically.
//
// Usage:
//
//	proxy --config config.yaml
package main

import (
	"context"
	"flag"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"dev.kernstock.net/gessinger/voice/hannah/proxy/internal/config"
	"dev.kernstock.net/gessinger/voice/hannah/proxy/internal/hannah"
	"dev.kernstock.net/gessinger/voice/hannah/proxy/internal/udp"
	"dev.kernstock.net/gessinger/voice/hannah/proxy/internal/voiceid"
)

// version is injected at build time via -ldflags="-X main.version=<tag>".
var version = "dev"

func main() {
	cfgPath := flag.String("config", "config.yaml", "path to config.yaml")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		slog.Error("failed to load config", "path", *cfgPath, "err", err)
		os.Exit(1)
	}

	// Parse UDP port from listen address for the heartbeat advertise fields.
	_, portStr, err := net.SplitHostPort(cfg.UDP.ListenAddr)
	if err != nil {
		slog.Error("invalid udp.listen_addr", "addr", cfg.UDP.ListenAddr, "err", err)
		os.Exit(1)
	}
	udpPort, err := strconv.Atoi(portStr)
	if err != nil {
		slog.Error("invalid UDP port", "port", portStr, "err", err)
		os.Exit(1)
	}

	slog.Info("hannah-proxy starting",
		"version", version,
		"proxy_id", cfg.ProxyID,
		"hannah", cfg.Hannah.Address,
		"udp", cfg.UDP.ListenAddr,
		"advertise_host", cfg.UDP.AdvertiseHost,
		"voice_id", cfg.VoiceID.Enabled,
	)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// Voice-ID client (optional)
	var voiceIDClient *voiceid.Client
	if cfg.VoiceID.Enabled {
		if cfg.VoiceID.BaseURL == "" {
			slog.Error("voice_id.enabled=true but base_url is empty")
			os.Exit(1)
		}
		voiceIDClient = voiceid.NewClient(
			cfg.VoiceID.BaseURL,
			cfg.VoiceID.TimeoutSec,
		)
		slog.Info("voice-id enabled", "base_url", cfg.VoiceID.BaseURL)
	}

	// gRPC client → Hannah Core
	hannahClient, err := hannah.NewClient(cfg.Hannah.Address)
	if err != nil {
		slog.Error("failed to create Hannah gRPC client", "err", err)
		os.Exit(1)
	}
	defer hannahClient.Close()

	// UDP server — created here but not bound yet.
	// Binding happens in the onReady callback below, after Hannah Core has
	// confirmed it stopped its own UDP server (ProxyAck).  This allows the
	// proxy to run on the same host as Hannah without a port conflict.
	udpServer := udp.NewServer(cfg.UDP.ListenAddr)
	defer udpServer.Close()

	// Wire: audio session complete → [Voice-ID] → Hannah gRPC pipeline → TTS back to satellite
	udpServer.OnAudio(func(device, room string, pcm []byte) {
		udpServer.SendStatus(device, "processing")

		// Speaker identification — runs before STT so Hannah can personalise the response.
		// If Voice-ID is disabled or fails, speakerID stays "" (anonymous).
		speakerID := ""
		if voiceIDClient != nil {
			id, err := voiceIDClient.Identify(ctx, pcm, 16000)
			if err != nil {
				slog.Warn("voice-id identify failed", "device", device, "err", err)
			} else if id != "" {
				slog.Info("speaker identified", "device", device, "roomie_id", id)
				speakerID = id
			}
		}

		resp, err := hannahClient.SubmitSatelliteAudio(ctx, device, room, pcm, speakerID)
		if err != nil {
			slog.Error("SubmitSatelliteAudio failed", "device", device, "err", err)
			udpServer.SendStatus(device, "idle")
			return
		}

		slog.Info("pipeline result",
			"device", device,
			"transcript", resp.Transcript,
			"intent", resp.IntentName,
			"answer", resp.Answer,
			"tts_bytes", len(resp.AudioPcm),
			"speaker", speakerID,
		)

		if len(resp.AudioPcm) > 0 {
			udpServer.SendStatus(device, "speaking")
			udpServer.SendTTS(device, resp.AudioPcm, int(resp.SampleRate))
		}
		udpServer.SendStatus(device, "idle")
	})

	udpServer.OnSessionStart(func(device string) {
		// Satellite started sending audio — signal "listening" so LED can react
		udpServer.SendStatus(device, "listening")
	})

	udpServer.OnSatelliteChange(func(device, room string, registered bool) {
		if registered {
			if err := hannahClient.NotifySatelliteRegistered(ctx, device, room); err != nil {
				slog.Warn("NotifySatelliteRegistered failed", "device", device, "err", err)
			}
		} else {
			if err := hannahClient.NotifySatelliteGone(ctx, device, room); err != nil {
				slog.Warn("NotifySatelliteGone failed", "device", device, "err", err)
			}
		}
	})

	// Register with Hannah Core (bidirectional stream):
	// - disables Hannah's UDP server while we're connected
	// - receives PlayAudioCommand for server-initiated announcements
	// - onReady: fires when Hannah's ProxyAck arrives → safe to bind UDP now
	go hannahClient.RunProxy(ctx, cfg.ProxyID, cfg.UDP.AdvertiseHost, int32(udpPort),
		func(deviceID string, pcm []byte, sampleRate int32) {
			slog.Info("announcement from Hannah", "device", deviceID, "bytes", len(pcm))
			udpServer.SendStatus(deviceID, "speaking")
			udpServer.SendTTS(deviceID, pcm, int(sampleRate))
			udpServer.SendStatus(deviceID, "idle")
		},
		func() {
			if err := udpServer.Start(); err != nil {
				slog.Error("failed to start UDP server", "err", err)
			}
			// Re-notify Hannah about all satellites already connected to the proxy
			// (handles Hannah restarts where _proxy_satellites is wiped).
			for device, room := range udpServer.RegisteredDevices() {
				if err := hannahClient.NotifySatelliteRegistered(ctx, device, room); err != nil {
					slog.Warn("re-notify satellite failed", "device", device, "err", err)
				} else {
					slog.Info("re-notified Hannah about existing satellite", "device", device, "room", room)
				}
			}
		},
	)

	slog.Info("proxy running — Ctrl+C to stop")
	<-ctx.Done()
	slog.Info("shutting down")
}
