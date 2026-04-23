package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

type Config struct {
	ProxyID string     `yaml:"proxy_id"`
	Hannah  HannahCfg  `yaml:"hannah"`
	UDP     UDPCfg     `yaml:"udp"`
	VoiceID VoiceIDCfg `yaml:"voice_id"`
}

type HannahCfg struct {
	// gRPC address of Hannah Core, e.g. "192.168.8.1:50051"
	Address string `yaml:"address"`
}

type UDPCfg struct {
	// UDP listen address for satellite connections, e.g. ":7775"
	ListenAddr string `yaml:"listen_addr"`
	// AdvertiseHost is the IP address published to satellites via MQTT discovery.
	// If empty, Hannah Core will auto-detect its own IP (same as before proxy).
	// Set this to the proxy's LAN IP so satellites connect to the proxy instead.
	AdvertiseHost string `yaml:"advertise_host"`
}

type VoiceIDCfg struct {
	// Enabled: false = Voice-ID disabled, speaker_roomie_id is always ""
	Enabled bool `yaml:"enabled"`
	// BaseURL: HTTP base URL of the Voice-ID service, e.g. "http://localhost:8765"
	BaseURL string `yaml:"base_url"`
	// TimeoutSec: HTTP request timeout in seconds (default: 3.0)
	TimeoutSec float64 `yaml:"timeout_sec"`
}

func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if cfg.ProxyID == "" {
		cfg.ProxyID = "hannah-proxy"
	}
	if cfg.Hannah.Address == "" {
		return nil, fmt.Errorf("hannah.address is required")
	}
	if cfg.UDP.ListenAddr == "" {
		cfg.UDP.ListenAddr = ":7775"
	}
	return &cfg, nil
}
