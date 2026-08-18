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
//	{"type":"register",  "device":"rpi-test", "listen_port":7776}
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
	"time"
)

const (
	typeControl = 0x01
	typeAudio   = 0x02
	typeTTS     = 0x03

	maxPacket = 65535
	ttsChunk  = 1400 // max bytes per TTS UDP packet — keeps packets below WiFi MTU (no IP fragmentation)

	// workerQueueCapacity bounds each device's pending-packet queue. Handling
	// a queued packet is just a mutex-protected append, so this only needs
	// headroom against brief scheduling jitter, not sustained backlog.
	workerQueueCapacity = 64
)

// AudioCallback is called when a complete audio session has been received.
// device is the satellite name; pcm is raw 16-bit signed mono at 16000 Hz.
type AudioCallback func(device string, pcm []byte)

// SessionStartCallback is called when the first audio chunk of a new session arrives.
type SessionStartCallback func(device string)

// SatelliteChangeCallback is called when a satellite registers or disconnects.
// On connect (registered=true): address is the satellite's IP, seed is the one-time pairing
// token (empty after pairing or if not provisioned).
// On disconnect (registered=false): address and seed are both "".
type SatelliteChangeCallback func(device, address, seed string, registered bool)

const heartbeatTimeout = 30 * time.Second // 3 × 10s heartbeat interval

type satellite struct {
	audioAddr     *net.UDPAddr // source address of audio packets
	ttsAddr       *net.UDPAddr // destination for TTS + control (may differ from audioAddr port)
	lastHeartbeat time.Time
}

type audioSession struct {
	chunks [][]byte
}

// packetJob is one raw UDP datagram queued for a specific device's worker.
type packetJob struct {
	pkt  []byte
	addr *net.UDPAddr
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
	satellites map[string]*satellite     // device → satellite
	sessions   map[string]*audioSession  // device → current session
	workers    map[string]chan packetJob // device → packet worker queue, preserves per-device order

	onAudio           AudioCallback
	onSessionStart    SessionStartCallback
	onSatelliteChange SatelliteChangeCallback
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
		workers:    make(map[string]chan packetJob),
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
	go s.watchdog()
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

// SendTTSChunk sends a single PCM chunk to a registered satellite (≤ttsChunk bytes per UDP packet).
// Packets are throttled to playback rate so the satellite's lwIP socket buffer does not overflow
// on long TTS responses (without throttling all packets arrive in a burst and most are dropped).
// Does not send tts_end — call SendTTSEnd when the last chunk is delivered.
func (s *Server) SendTTSChunk(device string, pcm []byte, sampleRate int) {
	s.mu.Lock()
	sat := s.satellites[device]
	conn := s.conn
	s.mu.Unlock()
	if sat == nil || conn == nil {
		return
	}
	const bytesPerSample = 2 // 16-bit mono
	for offset := 0; offset < len(pcm); offset += ttsChunk {
		end := offset + ttsChunk
		if end > len(pcm) {
			end = len(pcm)
		}
		chunk := pcm[offset:end]
		pkt := append([]byte{typeTTS}, chunk...)
		conn.WriteToUDP(pkt, sat.ttsAddr) //nolint:errcheck
		// Sleep proportional to the audio duration of this chunk so the satellite
		// receives packets at roughly the playback rate, keeping its ring buffer
		// topped up without overflowing the socket receive buffer.
		time.Sleep(time.Duration(len(chunk)) * time.Second / time.Duration(sampleRate*bytesPerSample))
	}
}

// SendTTSEnd sends a tts_end control message to signal the satellite that playback is complete.
func (s *Server) SendTTSEnd(device string, sampleRate int) {
	s.mu.Lock()
	sat := s.satellites[device]
	s.mu.Unlock()
	if sat == nil {
		return
	}
	s.sendControl(map[string]any{"type": "tts_end", "sample_rate": sampleRate}, sat.ttsAddr)
}

// SendTTS sends raw PCM audio to a registered satellite followed by a tts_end control message.
// Kept for the SubmitSatelliteAudio response path where the full blob arrives at once.
func (s *Server) SendTTS(device string, pcm []byte, sampleRate int) {
	s.mu.Lock()
	sat := s.satellites[device]
	s.mu.Unlock()
	if sat == nil {
		slog.Warn("SendTTS: satellite not registered", "device", device)
		return
	}
	s.SendTTSChunk(device, pcm, sampleRate)
	s.SendTTSEnd(device, sampleRate)
	slog.Info("TTS sent", "device", device, "bytes", len(pcm), "sample_rate", sampleRate)
}

// RegisteredDevices returns the device IDs of all registered satellites.
func (s *Server) RegisteredDevices() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.satellites))
	for d := range s.satellites {
		out = append(out, d)
	}
	return out
}

// SatelliteInfo holds the address for a registered satellite.
type SatelliteInfo struct {
	Address string
}

// RegisteredDevicesFull returns a snapshot of {device: SatelliteInfo} for all registered satellites.
func (s *Server) RegisteredDevicesFull() map[string]SatelliteInfo {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]SatelliteInfo, len(s.satellites))
	for d, sat := range s.satellites {
		out[d] = SatelliteInfo{Address: sat.audioAddr.IP.String()}
	}
	return out
}

// SendPaired sends a "paired" control message to a registered satellite to signal
// that the pairing seed has been accepted and can be cleared from NVS.
func (s *Server) SendPaired(device string) {
	s.mu.Lock()
	sat := s.satellites[device]
	s.mu.Unlock()
	if sat == nil {
		return
	}
	s.sendControl(map[string]any{"type": "paired", "device": device}, sat.ttsAddr)
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
		s.dispatch(pkt, addr)
	}
}

// dispatch routes a packet to its device's worker goroutine so packets from
// the same satellite are always handled in arrival order — audio chunks
// must not be reordered, and audio_end must never race ahead of the last
// audio chunk it follows (both used to run as one goroutine per packet,
// which could interleave or truncate a recording under scheduler jitter).
// Different satellites still run fully concurrently, one worker each.
//
// Packets that can't be attributed to a device (audio from an unregistered
// IP, malformed control JSON) carry no session state and are handled
// inline — order doesn't matter for those.
func (s *Server) dispatch(pkt []byte, addr *net.UDPAddr) {
	device := s.routingDevice(pkt, addr)
	if device == "" {
		go s.handle(pkt, addr)
		return
	}
	s.workerChan(device) <- packetJob{pkt: pkt, addr: addr}
}

// routingDevice extracts the device a packet belongs to without fully
// processing it, so it can be queued to the right worker before handle()
// parses it for real.
func (s *Server) routingDevice(pkt []byte, addr *net.UDPAddr) string {
	switch pkt[0] {
	case typeControl:
		var msg struct {
			Device string `json:"device"`
		}
		if err := json.Unmarshal(pkt[1:], &msg); err != nil {
			return ""
		}
		return msg.Device
	case typeAudio:
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.findDeviceByIP(addr.IP.String())
	default:
		return ""
	}
}

// workerChan returns device's packet queue, starting its worker goroutine
// lazily on first use. The channel send in dispatch happens without holding
// s.mu, so a full queue for one device only blocks packets for that device.
func (s *Server) workerChan(device string) chan packetJob {
	s.mu.Lock()
	defer s.mu.Unlock()
	ch, ok := s.workers[device]
	if !ok {
		ch = make(chan packetJob, workerQueueCapacity)
		s.workers[device] = ch
		go s.runWorker(ch)
	}
	return ch
}

func (s *Server) runWorker(ch chan packetJob) {
	for job := range ch {
		s.handle(job.pkt, job.addr)
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
		seed, _ := msg["seed"].(string)
		listenPort := addr.Port
		if lp, ok := msg["listen_port"].(float64); ok {
			listenPort = int(lp)
		}
		ttsAddr := &net.UDPAddr{IP: addr.IP, Port: listenPort}
		s.mu.Lock()
		s.satellites[device] = &satellite{audioAddr: addr, ttsAddr: ttsAddr, lastHeartbeat: time.Now()}
		delete(s.sessions, device) // verwaiste Session verwerfen (z.B. nach ESP-Neustart ohne audio_end)
		s.mu.Unlock()
		slog.Info("satellite registered", "device", device, "audio_from", addr, "tts_to_port", listenPort)
		s.sendControl(map[string]any{"type": "registered", "ok": true}, addr)
		if s.onSatelliteChange != nil {
			go s.onSatelliteChange(device, addr.IP.String(), seed, true)
		}

	case "audio_end":
		s.mu.Lock()
		sess := s.sessions[device]
		delete(s.sessions, device)
		s.mu.Unlock()
		if sess == nil {
			slog.Debug("audio_end without active session", "device", device)
			return
		}
		pcm := sess.pcm()
		slog.Info("audio session complete", "device", device, "bytes", len(pcm))
		if s.onAudio != nil {
			go s.onAudio(device, pcm)
		}

	case "heartbeat":
		s.mu.Lock()
		sat, registered := s.satellites[device]
		if registered {
			sat.audioAddr = addr
			sat.lastHeartbeat = time.Now()
		}
		s.mu.Unlock()
		if registered {
			s.sendControl(map[string]any{"type": "heartbeat_ack", "device": device}, addr)
		} else {
			slog.Info("heartbeat from unregistered satellite — requesting re-registration", "device", device, "addr", addr)
			s.sendControl(map[string]any{"type": "reregister"}, addr)
		}

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
		s.sendControl(map[string]any{"type": "reregister"}, addr)
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

func (s *Server) checkTimeouts() {
	now := time.Now()
	var timedOut []string
	s.mu.Lock()
	for device, sat := range s.satellites {
		if now.Sub(sat.lastHeartbeat) > heartbeatTimeout {
			timedOut = append(timedOut, device)
			delete(s.satellites, device)
		}
	}
	s.mu.Unlock()
	for _, device := range timedOut {
		slog.Warn("satellite heartbeat timeout — marking offline", "device", device)
		if s.onSatelliteChange != nil {
			s.onSatelliteChange(device, "", "", false)
		}
	}
}

func (s *Server) watchdog() {
	ticker := time.NewTicker(heartbeatTimeout / 3)
	defer ticker.Stop()
	for range ticker.C {
		s.mu.Lock()
		if s.conn == nil {
			s.mu.Unlock()
			return
		}
		s.mu.Unlock()
		s.checkTimeouts()
	}
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
