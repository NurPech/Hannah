package config

import "testing"

func TestLoadExampleConfig(t *testing.T) {
	cfg, err := Load("../../config.example.yaml")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ProxyID != "hannah-proxy" {
		t.Errorf("proxy_id mismatch: %q", cfg.ProxyID)
	}
	if cfg.Hannah.Address != "192.168.8.15:50051" {
		t.Errorf("hannah.address mismatch: %q", cfg.Hannah.Address)
	}
	if cfg.UDP.ListenAddr != ":7775" {
		t.Errorf("udp.listen_addr mismatch: %q", cfg.UDP.ListenAddr)
	}
	if cfg.UDP.AdvertiseHost != "192.168.8.5" {
		t.Errorf("udp.advertise_host mismatch: %q", cfg.UDP.AdvertiseHost)
	}
}
