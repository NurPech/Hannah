package config

import (
	"fmt"
	"os"
	"reflect"
	"strconv"
	"strings"

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

const envPrefix = "HANNAH_PROXY_"

func Load(path string) (*Config, error) {
	var cfg Config
	data, err := os.ReadFile(path)
	switch {
	case err == nil:
		if err := yaml.Unmarshal(data, &cfg); err != nil {
			return nil, fmt.Errorf("parse %s: %w", path, err)
		}
	case os.IsNotExist(err):
		// Config-Datei ist optional, sofern genug per Env kommt (siehe applyEnvOverrides
		// unten) — anders als bei einer vorhandenen, aber kaputten Datei ist das kein
		// Nutzerfehler, sondern der Normalfall für rein env-basierte Deployments (Docker).
	default:
		return nil, fmt.Errorf("read %s: %w", path, err)
	}

	applyEnvOverrides(reflect.ValueOf(&cfg).Elem(), nil)

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

// applyEnvOverrides walks val's fields via reflection (recursing into nested structs
// like HannahCfg/UDPCfg) and overrides each one whose HANNAH_PROXY_<PATH> environment
// variable is set, without any hardcoded allowlist. val must be an addressable struct
// value (e.g. reflect.ValueOf(&cfg).Elem()). Path segments come from the existing
// `json:"..."` tags. "." between nesting levels always becomes "__" (even without
// ambiguity) — otherwise reversing an env var name back into a nested path would be
// ambiguous, same reasoning as Core (#327). Plain "_" inside a tag name stays untouched
// (hannah.address -> HANNAH_PROXY_HANNAH__ADDRESS).
func applyEnvOverrides(val reflect.Value, pathPrefix []string) {
	typ := val.Type()
	for i := 0; i < typ.NumField(); i++ {
		field := typ.Field(i)
		fieldVal := val.Field(i)

		tag := strings.Split(field.Tag.Get("json"), ",")[0]
		if tag == "" {
			tag = strings.ToLower(field.Name)
		}
		path := append(append([]string{}, pathPrefix...), tag)

		if fieldVal.Kind() == reflect.Struct {
			applyEnvOverrides(fieldVal, path)
			continue
		}

		envName := envPrefix + strings.ToUpper(strings.Join(path, "__"))
		raw, ok := os.LookupEnv(envName)
		if !ok {
			continue
		}
		setField(fieldVal, raw)
	}
}

// setField assigns raw (always a string, since it comes from an env var) to fieldVal,
// coerced to whatever type the field already is — best-effort, no schema declared.
func setField(fieldVal reflect.Value, raw string) {
	switch fieldVal.Kind() {
	case reflect.String:
		fieldVal.SetString(raw)
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
			fieldVal.SetInt(n)
		}
	case reflect.Bool:
		if b, err := strconv.ParseBool(raw); err == nil {
			fieldVal.SetBool(b)
		}
	}
}
