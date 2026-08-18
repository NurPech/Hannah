package udp

import (
	"encoding/json"
	"net"
	"testing"
	"time"
)

// makeServer creates a Server without binding a port.
// Callbacks can be registered before calling Start().
func makeServer() *Server {
	return NewServer("127.0.0.1:0")
}

// makeAddr builds a UDPAddr from an IP string and port.
func makeAddr(ip string, port int) *net.UDPAddr {
	return &net.UDPAddr{IP: net.ParseIP(ip), Port: port}
}

// controlPayload encodes a control message as JSON bytes.
func controlPayload(t *testing.T, msg map[string]any) []byte {
	t.Helper()
	b, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	return b
}

// --- handleControl: register ------------------------------------------------

func TestRegister_AddsSatellite(t *testing.T) {
	s := makeServer()
	addr := makeAddr("192.168.1.100", 7776)

	s.handleControl(controlPayload(t, map[string]any{
		"type":   "register",
		"device": "wohnzimmer-esp",
	}), addr)

	s.mu.Lock()
	_, ok := s.satellites["wohnzimmer-esp"]
	s.mu.Unlock()

	if !ok {
		t.Fatal("satellite was not registered")
	}
}

func TestRegister_CallsCallback(t *testing.T) {
	s := makeServer()

	done := make(chan struct{}, 1)
	s.OnSatelliteChange(func(device, address, seed string, registered bool) {
		if device == "wohnzimmer-esp" && registered {
			done <- struct{}{}
		}
	})

	s.handleControl(controlPayload(t, map[string]any{
		"type":   "register",
		"device": "wohnzimmer-esp",
	}), makeAddr("192.168.1.100", 7776))

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("onSatelliteChange was not called")
	}
}

func TestRegister_SetsLastHeartbeat(t *testing.T) {
	s := makeServer()
	before := time.Now()

	s.handleControl(controlPayload(t, map[string]any{
		"type":   "register",
		"device": "wohnzimmer-esp",
	}), makeAddr("192.168.1.100", 7776))

	s.mu.Lock()
	ts := s.satellites["wohnzimmer-esp"].lastHeartbeat
	s.mu.Unlock()

	if ts.Before(before) {
		t.Error("lastHeartbeat was not set on registration")
	}
}

// --- handleControl: heartbeat -----------------------------------------------

func TestHeartbeat_UpdatesTimestamp(t *testing.T) {
	s := makeServer()
	addr := makeAddr("192.168.1.100", 7776)
	old := time.Now().Add(-10 * time.Second)

	s.mu.Lock()
	s.satellites["wohnzimmer-esp"] = &satellite{
		audioAddr: addr, ttsAddr: addr,
		lastHeartbeat: old,
	}
	s.mu.Unlock()

	s.handleControl(controlPayload(t, map[string]any{
		"type":   "heartbeat",
		"device": "wohnzimmer-esp",
	}), addr)

	s.mu.Lock()
	newTs := s.satellites["wohnzimmer-esp"].lastHeartbeat
	s.mu.Unlock()

	if !newTs.After(old) {
		t.Error("lastHeartbeat was not updated by heartbeat")
	}
}

// --- checkTimeouts ----------------------------------------------------------

func TestCheckTimeouts_RemovesStale(t *testing.T) {
	s := makeServer()
	addr := makeAddr("192.168.1.100", 7776)

	s.mu.Lock()
	s.satellites["stale-sat"] = &satellite{
		audioAddr: addr, ttsAddr: addr,
		lastHeartbeat: time.Now().Add(-31 * time.Second),
	}
	s.mu.Unlock()

	s.checkTimeouts()

	s.mu.Lock()
	_, exists := s.satellites["stale-sat"]
	s.mu.Unlock()

	if exists {
		t.Error("stale satellite should have been removed")
	}
}

func TestCheckTimeouts_CallsCallbackWithFalse(t *testing.T) {
	s := makeServer()
	addr := makeAddr("192.168.1.100", 7776)

	done := make(chan bool, 1)
	s.OnSatelliteChange(func(device, address, seed string, registered bool) {
		done <- registered
	})

	s.mu.Lock()
	s.satellites["stale-sat"] = &satellite{
		audioAddr: addr, ttsAddr: addr,
		lastHeartbeat: time.Now().Add(-31 * time.Second),
	}
	s.mu.Unlock()

	s.checkTimeouts()

	select {
	case registered := <-done:
		if registered {
			t.Error("callback should signal offline (registered=false)")
		}
	case <-time.After(time.Second):
		t.Fatal("onSatelliteChange was not called")
	}
}

func TestCheckTimeouts_FreshNotRemoved(t *testing.T) {
	s := makeServer()
	addr := makeAddr("192.168.1.100", 7776)

	s.mu.Lock()
	s.satellites["fresh-sat"] = &satellite{
		audioAddr: addr, ttsAddr: addr,
		lastHeartbeat: time.Now(),
	}
	s.mu.Unlock()

	s.checkTimeouts()

	s.mu.Lock()
	_, exists := s.satellites["fresh-sat"]
	s.mu.Unlock()

	if !exists {
		t.Error("fresh satellite should not have been removed")
	}
}

func TestCheckTimeouts_PartialTimeout(t *testing.T) {
	s := makeServer()
	addr := makeAddr("192.168.1.100", 7776)

	s.mu.Lock()
	s.satellites["stale-sat"] = &satellite{
		audioAddr: addr, ttsAddr: addr,
		lastHeartbeat: time.Now().Add(-31 * time.Second),
	}
	s.satellites["fresh-sat"] = &satellite{
		audioAddr:     makeAddr("192.168.1.101", 7776),
		ttsAddr:       makeAddr("192.168.1.101", 7776),
		lastHeartbeat: time.Now(),
	}
	s.mu.Unlock()

	s.checkTimeouts()

	s.mu.Lock()
	_, staleExists := s.satellites["stale-sat"]
	_, freshExists := s.satellites["fresh-sat"]
	s.mu.Unlock()

	if staleExists {
		t.Error("stale satellite should have been removed")
	}
	if !freshExists {
		t.Error("fresh satellite should not have been removed")
	}
}

// --- audioSession.pcm -------------------------------------------------------

func TestAudioSession_PCM_ConcatenatesChunks(t *testing.T) {
	sess := &audioSession{
		chunks: [][]byte{
			{0x01, 0x02},
			{0x03, 0x04},
			{0x05},
		},
	}
	got := sess.pcm()
	want := []byte{0x01, 0x02, 0x03, 0x04, 0x05}
	if len(got) != len(want) {
		t.Fatalf("pcm() len = %d, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("pcm()[%d] = 0x%02x, want 0x%02x", i, got[i], want[i])
		}
	}
}

func TestAudioSession_PCM_Empty(t *testing.T) {
	sess := &audioSession{}
	got := sess.pcm()
	if len(got) != 0 {
		t.Errorf("pcm() on empty session = %v, want []", got)
	}
}

// --- dispatch: per-device ordering -------------------------------------------
//
// Before routing packets to a per-device worker, every UDP datagram was
// handled in its own goroutine (`go s.handle(pkt, addr)`), so audio chunks
// for the same satellite could be appended out of order under scheduler
// jitter, and audio_end could race ahead of the last audio chunk it follows.
// These guard against that regression.

func TestDispatch_PreservesAudioOrderPerDevice(t *testing.T) {
	const n = 100
	s := makeServer()
	addr := makeAddr("192.168.1.100", 7776)

	s.mu.Lock()
	s.satellites["dev1"] = &satellite{audioAddr: addr, ttsAddr: addr, lastHeartbeat: time.Now()}
	s.mu.Unlock()

	got := make(chan []byte, 1)
	s.OnAudio(func(device string, pcm []byte) {
		got <- pcm
	})

	for i := 0; i < n; i++ {
		s.dispatch([]byte{typeAudio, byte(i)}, addr)
	}
	s.dispatch(append([]byte{typeControl}, controlPayload(t, map[string]any{
		"type":   "audio_end",
		"device": "dev1",
	})...), addr)

	select {
	case pcm := <-got:
		if len(pcm) != n {
			t.Fatalf("got %d bytes, want %d", len(pcm), n)
		}
		for i := 0; i < n; i++ {
			if pcm[i] != byte(i) {
				t.Fatalf("byte %d out of order: got %d, want %d", i, pcm[i], i)
			}
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for onAudio callback")
	}
}

func TestDispatch_DifferentDevicesConcurrent(t *testing.T) {
	s := makeServer()
	addrA := makeAddr("192.168.1.101", 7776)
	addrB := makeAddr("192.168.1.102", 7776)

	s.mu.Lock()
	s.satellites["a"] = &satellite{audioAddr: addrA, ttsAddr: addrA, lastHeartbeat: time.Now()}
	s.satellites["b"] = &satellite{audioAddr: addrB, ttsAddr: addrB, lastHeartbeat: time.Now()}
	s.mu.Unlock()

	release := make(chan struct{})
	bDone := make(chan struct{}, 1)
	s.OnSessionStart(func(device string) {
		if device == "a" {
			<-release // blocks worker "a" until the test releases it
		} else {
			bDone <- struct{}{}
		}
	})

	s.dispatch([]byte{typeAudio, 1}, addrA) // blocks worker "a" in OnSessionStart

	select {
	case <-bDone:
		t.Fatal("device b callback ran before being dispatched")
	case <-time.After(20 * time.Millisecond):
	}

	s.dispatch([]byte{typeAudio, 1}, addrB)

	select {
	case <-bDone:
	case <-time.After(time.Second):
		t.Fatal("device b was not served while device a was still blocked — devices are not running concurrently")
	}

	close(release)
}
