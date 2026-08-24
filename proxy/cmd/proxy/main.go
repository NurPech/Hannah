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
	)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

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

	// Wire: audio session complete → Hannah gRPC pipeline → TTS back to satellite.
	// Speaker identification happens on Hannah's side now (#210), not here.
	udpServer.OnAudio(func(device string, pcm []byte) {
		udpServer.SendStatus(device, "processing")

		resp, err := hannahClient.SubmitSatelliteAudio(ctx, device, pcm)
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

	udpServer.OnSatelliteChange(func(device, address, seed string, registered bool) {
		if registered {
			paired, err := hannahClient.NotifySatelliteRegistered(ctx, device, address, seed)
			if err != nil {
				slog.Warn("NotifySatelliteRegistered failed", "device", device, "err", err)
			} else if paired {
				udpServer.SendPaired(device)
				slog.Info("satellite paired", "device", device)
			}
		} else {
			if err := hannahClient.NotifySatelliteGone(ctx, device); err != nil {
				slog.Warn("NotifySatelliteGone failed", "device", device, "err", err)
			}
		}
	})

	// Register with Hannah Core (bidirectional stream):
	// - disables Hannah's UDP server while we're connected
	// - receives PlayAudioCommand for server-initiated announcements
	// - onReady: fires when Hannah's ProxyAck arrives → safe to bind UDP now
	go hannahClient.RunProxy(ctx, cfg.ProxyID, cfg.UDP.AdvertiseHost, int32(udpPort),
		func(deviceID string, pcm []byte, sampleRate int32, isLast bool) {
			udpServer.SendStatus(deviceID, "speaking")
			udpServer.SendTTSChunk(deviceID, pcm, int(sampleRate))
			if isLast {
				udpServer.SendTTSEnd(deviceID, int(sampleRate))
				udpServer.SendStatus(deviceID, "idle")
				slog.Info("announcement complete", "device", deviceID)
			}
		},
		func() {
			if err := udpServer.Start(); err != nil {
				slog.Error("failed to start UDP server", "err", err)
			}
			// Re-notify Hannah about all satellites already connected to the proxy
			// (handles Hannah restarts where _proxy_satellites is wiped).
			for device, info := range udpServer.RegisteredDevicesFull() {
				if _, err := hannahClient.NotifySatelliteRegistered(ctx, device, info.Address, ""); err != nil {
					slog.Warn("re-notify satellite failed", "device", device, "err", err)
				} else {
					slog.Info("re-notified Hannah about existing satellite", "device", device)
				}
			}
		},
		func() {
			// Lost (or never got) the gRPC connection to Hannah Core — stop accepting
			// satellite UDP traffic instead of silently blackholing it (#140). Restarted
			// by the onReady callback above once the connection comes back.
			slog.Warn("Hannah Core unreachable — stopping satellite UDP server")
			udpServer.Close()
		},
	)

	slog.Info("proxy running — Ctrl+C to stop")
	<-ctx.Done()
	slog.Info("shutting down")
}
