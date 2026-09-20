package config

import (
	"os"
	"path/filepath"
	"testing"
)

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

func TestLoadMissingFile(t *testing.T) {
	t.Setenv("HANNAH_PROXY_HANNAH__ADDRESS", "192.168.1.5:50051")
	cfg, err := Load(filepath.Join(t.TempDir(), "does-not-exist.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Hannah.Address != "192.168.1.5:50051" {
		t.Errorf("hannah.address mismatch: %q", cfg.Hannah.Address)
	}
	if cfg.ProxyID != "hannah-proxy" {
		t.Errorf("proxy_id default mismatch: %q", cfg.ProxyID)
	}
}

func TestLoadMissingFileWithoutAddressStillErrors(t *testing.T) {
	_, err := Load(filepath.Join(t.TempDir(), "does-not-exist.yaml"))
	if err == nil {
		t.Fatal("expected error when hannah.address is missing from both file and env")
	}
}

func TestEnvOverridesExistingValue(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeFile(t, path, "hannah:\n  address: 192.168.8.15:50051\n")
	t.Setenv("HANNAH_PROXY_HANNAH__ADDRESS", "192.168.1.5:50051")

	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Hannah.Address != "192.168.1.5:50051" {
		t.Errorf("hannah.address mismatch: %q", cfg.Hannah.Address)
	}
}

func TestEnvOverrideDoesNotClobberSiblingField(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeFile(t, path, "hannah:\n  address: 192.168.8.15:50051\nudp:\n  listen_addr: \":7775\"\n  advertise_host: 192.168.8.5\n")
	t.Setenv("HANNAH_PROXY_UDP__ADVERTISE_HOST", "10.0.0.1")

	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.UDP.AdvertiseHost != "10.0.0.1" {
		t.Errorf("udp.advertise_host mismatch: %q", cfg.UDP.AdvertiseHost)
	}
	if cfg.UDP.ListenAddr != ":7775" {
		t.Errorf("udp.listen_addr should be untouched, got: %q", cfg.UDP.ListenAddr)
	}
}

func TestOtherHannahPrefixedVarsAreNotAbsorbed(t *testing.T) {
	// Same collision Core hit in CI (#327): other HANNAH_* variables exist in the
	// project for unrelated purposes and must not affect this component's config.
	t.Setenv("HANNAH_ASSET_SERVER_BASE_URL", "https://hannah-asset.example.com")
	t.Setenv("HANNAH_CORE_MQTT__HOST", "192.168.1.5")
	t.Setenv("HANNAH_PROXY_HANNAH__ADDRESS", "192.168.1.5:50051")

	cfg, err := Load(filepath.Join(t.TempDir(), "does-not-exist.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Hannah.Address != "192.168.1.5:50051" {
		t.Errorf("hannah.address mismatch: %q", cfg.Hannah.Address)
	}
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
