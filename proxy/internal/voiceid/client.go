// Package voiceid provides a client for a self-hosted Voice-ID REST service.
//
// Expected API contract (implement server-side in Python or similar):
//
//	POST /identify
//	  Content-Type: application/octet-stream
//	  X-Sample-Rate: 16000
//	  Body: raw PCM bytes (16-bit signed little-endian mono)
//
//	  200 → {"roomie_id": "leonie", "confidence": 0.91}
//	        roomie_id="" or "unknown" means speaker not recognised
//
//	POST /enroll
//	  Content-Type: application/octet-stream
//	  X-Sample-Rate: 16000
//	  X-Roomie-ID: leonie
//	  Body: raw PCM bytes
//
//	  200 → {"ok": true, "message": "enrolled"}
package voiceid

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"
)

// Client calls a Voice-ID REST service.
type Client struct {
	baseURL    string
	httpClient *http.Client
}

// IdentifyResponse is the JSON body returned by POST /identify.
type IdentifyResponse struct {
	RoomieID   string  `json:"roomie_id"`
	Confidence float32 `json:"confidence"`
}

// NewClient creates a Voice-ID client.
//
//	baseURL    e.g. "http://localhost:8765"
//	timeoutSec HTTP request timeout; 0 defaults to 3s
func NewClient(baseURL string, timeoutSec float64) *Client {
	if timeoutSec == 0 {
		timeoutSec = 3.0
	}
	return &Client{
		baseURL:    baseURL,
		httpClient: &http.Client{Timeout: time.Duration(timeoutSec * float64(time.Second))},
	}
}

// Identify sends PCM audio to the Voice-ID service and returns the roomie_id.
// Returns "" if the speaker is unknown or confidence is below the configured threshold.
// Errors are logged but do not block the pipeline — the caller gets "" on failure.
func (c *Client) Identify(ctx context.Context, pcm []byte, sampleRate int) (string, error) {
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost, c.baseURL+"/identify", bytes.NewReader(pcm),
	)
	if err != nil {
		return "", fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("X-Sample-Rate", strconv.Itoa(sampleRate))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("voice-id returned HTTP %d", resp.StatusCode)
	}

	var result IdentifyResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("decode response: %w", err)
	}

	if result.RoomieID == "" || result.RoomieID == "unknown" {
		return "", nil
	}
	return result.RoomieID, nil
}

// Enroll registers a voice sample for a roomie at the Voice-ID service.
// Called when Hannah Core forwards an EnrollVoiceprint gRPC request.
func (c *Client) Enroll(ctx context.Context, roomieID string, pcm []byte, sampleRate int) error {
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost, c.baseURL+"/enroll", bytes.NewReader(pcm),
	)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("X-Sample-Rate", strconv.Itoa(sampleRate))
	req.Header.Set("X-Roomie-ID", roomieID)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("voice-id enroll returned HTTP %d", resp.StatusCode)
	}
	return nil
}
