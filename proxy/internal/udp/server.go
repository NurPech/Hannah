// Package udp implements the satellite UDP protocol.
//
// Protocol (1-byte type prefix):
//
//	0x01 + JSON  = Control message  (both directions)
//	0x02 + PCM   = Audio data       (satellite → proxy, raw 16kHz 16-bit mono)
//	0x03 + PCM   = TTS audio        (proxy → satellite, same format)
//
// Control messages from satellite:
//
//	{"type":"register",  "device":"rpi-test", "room":"Wohnzimmer", "listen_port":7776}
//	{"type":"audio_end", "device":"rpi-test"}
//	{"type":"heartbeat", "device":"rpi-test"}
//
// Control responses from proxy:
//
//	{"type":"registered",    "ok":true}
//	{"type":"heartbeat_ack", "device":"rpi-test"}
//	{"type":"status",        "state":"processing"}
//	{"type":"tts_end",       "sample_rate":24000}
package udp

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"sync"
)

const (
	typeControl = 0x01
	typeAudio   = 0x02
	typeTTS     = 0x03

	maxPacket = 65535
	ttsChunk  = 60_000 // max bytes per TTS UDP packet
)

// AudioCallback is called when a complete audio session has been received.
// device is the satellite name; room is as reported at registration.
// pcm is raw 16-bit signed mono at 16000 Hz.
type AudioCallback func(device, room string, pcm []byte)

// SessionStartCallback is called when the first audio chunk of a new session arrives.
type SessionStartCallback func(device string)

// SatelliteChangeCallback is called when a satellite registers or disconnects.
// registered=true on connect, false on disconnect.
type SatelliteChangeCallback func(device, room string, registered bool)

type satellite struct {
	audioAddr *net.UDPAddr // source address of audio packets
	ttsAddr   *net.UDPAddr // destination for TTS + control (may differ from audioAddr port)
	room      string
}

type audioSession struct {
	chunks [][]byte
}

func (s *audioSession) pcm() []byte {
	total := 0
	for _, c := range s.chunks {
		total += len(c)
	}
	out := make([]byte, 0, total)
	for _, c := range s.chunks {
		out = append(out, c...)
	}
	return out
}

// Server receives satellite audio over UDP and sends TTS/control back.
type Server struct {
	addr string
	conn *net.UDPConn

	mu         sync.Mutex
	satellites map[string]*satellite    // device → satellite
	sessions   map[string]*audioSession // device → current session

	onAudio            AudioCallback
	onSessionStart     SessionStartCallback
	onSatelliteChange  SatelliteChangeCallback
}

// NewServer creates a UDP server but does not bind yet.
// Call Start() to bind and begin receiving packets.
// This allows the proxy to register with Hannah Core first (which stops
// Hannah's own UDP server) before claiming the port.
func NewServer(addr string) *Server {
	return &Server{
		addr:       addr,
		satellites: make(map[string]*satellite),
		sessions:   make(map[string]*audioSession),
	}
}

// Start binds the UDP port and begins the receive loop.
// Idempotent — safe to call when already started (no-op).
func (s *Server) Start() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn != nil {
		return nil // already running
	}
	udpAddr, err := net.ResolveUDPAddr("udp", s.addr)
	if err != nil {
		return fmt.Errorf("resolve udp addr %q: %w", s.addr, err)
	}
	conn, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		return fmt.Errorf("listen udp %q: %w", s.addr, err)
	}
	s.conn = conn
	go s.loop()
	slog.Info("UDP server listening", "addr", s.addr)
	return nil
}

// Close shuts down the UDP server.
func (s *Server) Close() {
	s.mu.Lock()
	conn := s.conn
	s.conn = nil
	s.mu.Unlock()
	if conn != nil {
		conn.Close()
	}
}

// OnAudio registers the callback invoked when a complete audio session is ready.
func (s *Server) OnAudio(fn AudioCallback) {
	s.onAudio = fn
}

// OnSessionStart registers the callback invoked when a new audio session begins.
func (s *Server) OnSessionStart(fn SessionStartCallback) {
	s.onSessionStart = fn
}

// OnSatelliteChange registers the callback invoked when a satellite registers or disconnects.
func (s *Server) OnSatelliteChange(fn SatelliteChangeCallback) {
	s.onSatelliteChange = fn
}

// SendStatus sends a status control message to a registered satellite.
// Known states: idle, listening, processing, speaking.
func (s *Server) SendStatus(device, state string) {
	s.mu.Lock()
	sat := s.satellites[device]
	s.mu.Unlock()
	if sat == nil {
		return
	}
	s.sendControl(map[string]any{"type": "status", "state": state}, sat.ttsAddr)
}

// SendTTS sends raw PCM audio to a registered satellite in ≤60 KB chunks,
// followed by a tts_end control message.
func (s *Server) SendTTS(device string, pcm []byte, sampleRate int) {
	s.mu.Lock()
	sat := s.satellites[device]
	conn := s.conn
	s.mu.Unlock()
	if sat == nil {
		slog.Warn("SendTTS: satellite not registered", "device", device)
		return
	}
	if conn == nil {
		return
	}
	for offset := 0; offset < len(pcm); offset += ttsChunk {
		end := offset + ttsChunk
		if end > len(pcm) {
			end = len(pcm)
		}
		pkt := append([]byte{typeTTS}, pcm[offset:end]...)
		conn.WriteToUDP(pkt, sat.ttsAddr) //nolint:errcheck
	}
	s.sendControl(map[string]any{"type": "tts_end", "sample_rate": sampleRate}, sat.ttsAddr)
	slog.Info("TTS sent", "device", device, "bytes", len(pcm), "sample_rate", sampleRate)
}

// RegisteredDevices returns a snapshot of {device: room} for all registered satellites.
func (s *Server) RegisteredDevices() map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]string, len(s.satellites))
	for d, sat := range s.satellites {
		out[d] = sat.room
	}
	return out
}

// ------------------------------------------------------------------
// Internal

func (s *Server) loop() {
	s.mu.Lock()
	conn := s.conn
	s.mu.Unlock()
	buf := make([]byte, maxPacket)
	for {
		n, addr, err := conn.ReadFromUDP(buf)
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			slog.Warn("UDP read error", "err", err)
			continue
		}
		if n < 2 {
			continue
		}
		pkt := make([]byte, n)
		copy(pkt, buf[:n])
		go s.handle(pkt, addr)
	}
}

func (s *Server) handle(pkt []byte, addr *net.UDPAddr) {
	switch pkt[0] {
	case typeControl:
		s.handleControl(pkt[1:], addr)
	case typeAudio:
		s.handleAudio(pkt[1:], addr)
	default:
		slog.Debug("unknown packet type", "type", fmt.Sprintf("0x%02x", pkt[0]))
	}
}

func (s *Server) handleControl(payload []byte, addr *net.UDPAddr) {
	var msg map[string]any
	if err := json.Unmarshal(payload, &msg); err != nil {
		slog.Warn("invalid control packet", "err", err, "addr", addr)
		return
	}
	t, _ := msg["type"].(string)
	device, _ := msg["device"].(string)

	switch t {
	case "register":
		room, _ := msg["room"].(string)
		listenPort := addr.Port
		if lp, ok := msg["listen_port"].(float64); ok {
			listenPort = int(lp)
		}
		ttsAddr := &net.UDPAddr{IP: addr.IP, Port: listenPort}
		s.mu.Lock()
		s.satellites[device] = &satellite{audioAddr: addr, ttsAddr: ttsAddr, room: room}
		s.mu.Unlock()
		slog.Info("satellite registered", "device", device, "room", room,
			"audio_from", addr, "tts_to_port", listenPort)
		s.sendControl(map[string]any{"type": "registered", "ok": true}, addr)
		if s.onSatelliteChange != nil {
			go s.onSatelliteChange(device, room, true)
		}

	case "audio_end":
		s.mu.Lock()
		sess := s.sessions[device]
		delete(s.sessions, device)
		sat := s.satellites[device]
		s.mu.Unlock()
		if sess == nil {
			slog.Debug("audio_end without active session", "device", device)
			return
		}
		room := ""
		if sat != nil {
			room = sat.room
		}
		pcm := sess.pcm()
		slog.Info("audio session complete", "device", device, "bytes", len(pcm))
		if s.onAudio != nil {
			go s.onAudio(device, room, pcm)
		}

	case "heartbeat":
		s.mu.Lock()
		if sat, ok := s.satellites[device]; ok {
			sat.audioAddr = addr
		}
		s.mu.Unlock()
		s.sendControl(map[string]any{"type": "heartbeat_ack", "device": device}, addr)

	default:
		slog.Debug("unknown control type", "type", t, "addr", addr)
	}
}

func (s *Server) handleAudio(payload []byte, addr *net.UDPAddr) {
	s.mu.Lock()
	device := s.findDeviceByIP(addr.IP.String())
	if device == "" {
		s.mu.Unlock()
		slog.Warn("audio from unregistered IP — satellite must register first", "addr", addr)
		return
	}
	isNew := false
	if _, exists := s.sessions[device]; !exists {
		s.sessions[device] = &audioSession{}
		isNew = true
	}
	s.sessions[device].chunks = append(s.sessions[device].chunks, payload)
	s.mu.Unlock()

	if isNew && s.onSessionStart != nil {
		go s.onSessionStart(device)
	}
}

func (s *Server) sendControl(msg map[string]any, addr *net.UDPAddr) {
	s.mu.Lock()
	conn := s.conn
	s.mu.Unlock()
	if conn == nil {
		return
	}
	data, _ := json.Marshal(msg)
	conn.WriteToUDP(append([]byte{typeControl}, data...), addr) //nolint:errcheck
}

// findDeviceByIP returns the first device name matching the given IP.
// Must be called with s.mu held.
func (s *Server) findDeviceByIP(ip string) string {
	for device, sat := range s.satellites {
		if sat.audioAddr.IP.String() == ip {
			return device
		}
	}
	return ""
}
