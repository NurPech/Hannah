package config

import (
	"fmt"
	"os"

	"sigs.k8s.io/yaml"
)

type Config struct {
	ProxyID string    `json:"proxy_id"`
	Hannah  HannahCfg `json:"hannah"`
	UDP     UDPCfg    `json:"udp"`
}

type HannahCfg struct {
	// gRPC address of Hannah Core, e.g. "192.168.8.1:50051"
	Address string `json:"address"`
}

type UDPCfg struct {
	// UDP listen address for satellite connections, e.g. ":7775"
	ListenAddr string `json:"listen_addr"`
	// AdvertiseHost is the IP address published to satellites via MQTT discovery.
	// If empty, Hannah Core will auto-detect its own IP (same as before proxy).
	// Set this to the proxy's LAN IP so satellites connect to the proxy instead.
	AdvertiseHost string `json:"advertise_host"`
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
