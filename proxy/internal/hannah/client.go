// Package hannah provides a gRPC client to Hannah Core.
package hannah

import (
	"context"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	pb "github.com/NurPech/hannah-proto-go/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// PlayAudioFunc is called when Hannah pushes a PlayAudioCommand via the proxy stream.
// isLast is true on the final chunk of a TTS response — the proxy should send tts_end after it.
// Calls for the same device are serialised and arrive in order; calls for different
// devices may run concurrently (see playAudioDispatcher).
type PlayAudioFunc func(deviceID string, pcm []byte, sampleRate int32, isLast bool)

// Client is a gRPC client to Hannah Core.
type Client struct {
	conn *grpc.ClientConn
	stub pb.HannahServiceClient
}

// NewClient dials Hannah Core at address (e.g. "192.168.8.1:50051").
func NewClient(address string) (*Client, error) {
	conn, err := grpc.NewClient(address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(32*1024*1024)),
		// compat_version (hannah-proto#9/hannah#217) runs additively next to
		// the version_interceptor.go pair above, not as a replacement — a
		// breaking change scoped to one message no longer has to reject
		// every client, only calls that actually use the affected message.
		grpc.WithChainUnaryInterceptor(versionUnaryInterceptor, pb.CompatVersionUnaryClientInterceptor),
		grpc.WithChainStreamInterceptor(versionStreamInterceptor, pb.CompatVersionStreamClientInterceptor),
	)
	if err != nil {
		return nil, fmt.Errorf("grpc dial %q: %w", address, err)
	}
	return &Client{conn: conn, stub: pb.NewHannahServiceClient(conn)}, nil
}

// Close tears down the gRPC connection.
func (c *Client) Close() {
	c.conn.Close()
}

// SubmitSatelliteAudio sends a complete audio session to Hannah and waits for
// the pipeline result (STT → NLU → TTS).
// pcm must be raw 16-bit signed mono at 16000 Hz.
// Speaker identification now happens on Hannah's side (#210) — the proxy no
// longer resolves or forwards a speaker_user_id.
func (c *Client) SubmitSatelliteAudio(ctx context.Context, deviceID string, pcm []byte) (*pb.SubmitSatelliteAudioResponse, error) {
	return c.stub.SubmitSatelliteAudio(ctx, &pb.SubmitSatelliteAudioRequest{
		DeviceId:   deviceID,
		AudioPcm:   pcm,
		SampleRate: 16000,
	})
}

// NotifySatelliteRegistered tells Hannah Core that a satellite has connected via the proxy.
// deviceID is the satellite's eFuse MAC (e.g. "e072a1d01adc"); seed is the one-time pairing
// token (empty if not provisioned). Returns paired=true if Core confirmed pairing of the seed.
func (c *Client) NotifySatelliteRegistered(ctx context.Context, deviceID, address, seed string) (paired bool, err error) {
	resp, err := c.stub.NotifySatelliteRegistered(ctx, &pb.SatelliteRegistration{
		DeviceId: deviceID,
		Address:  address,
		Seed:     seed,
	})
	if err != nil {
		return false, err
	}
	return resp.GetMessage() == "paired", nil
}

// NotifySatelliteGone tells Hannah Core that a satellite has disconnected from the proxy.
func (c *Client) NotifySatelliteGone(ctx context.Context, deviceID string) error {
	_, err := c.stub.NotifySatelliteGone(ctx, &pb.SatelliteRegistration{
		DeviceId: deviceID,
	})
	return err
}

// RunProxy opens the RegisterProxy bidirectional stream, sends periodic heartbeats,
// and calls onPlayAudio whenever Hannah wants to play audio on a satellite.
//
// udpHost and udpPort are the proxy's UDP advertise address; Hannah will publish
// them to the MQTT discovery topic so satellites connect to the proxy instead of
// Hannah's own UDP server. Pass an empty udpHost to leave the discovery unchanged.
//
// onReady is called once Hannah confirms UDP is disabled (ProxyAck received).
// Use this to start the proxy's own UDP server — by then Hannah has freed the port.
// On reconnect onReady is called again; make it idempotent.
//
// onLost is called every time the stream ends (error or clean shutdown) before the
// next reconnect attempt. Use this to stop/unbind the satellite-facing UDP server —
// without it, satellites keep talking to a proxy that has nowhere to forward to (#140).
// Make it idempotent, same as onReady.
//
// Blocks until ctx is cancelled. Reconnects automatically with a 5s backoff.
func (c *Client) RunProxy(ctx context.Context, proxyID, udpHost string, udpPort int32, onPlayAudio PlayAudioFunc, onReady func(), onLost func()) {
	everConnected := false
	for {
		connected, err := c.runProxyOnce(ctx, proxyID, udpHost, udpPort, onPlayAudio, onReady)
		if connected {
			everConnected = true
		}
		if onLost != nil {
			onLost()
		}
		if ctx.Err() != nil {
			return // clean shutdown
		}
		if everConnected {
			slog.Warn("RegisterProxy stream lost, reconnecting in 5s", "err", err)
		} else {
			slog.Warn("RegisterProxy: initial connection to Hannah Core failed, retrying in 5s", "err", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}

// runProxyOnce runs a single RegisterProxy session. The returned bool is true if
// Hannah Core ever ACKed the connection during this session (distinguishes "never
// connected" from "connected, then dropped" for logging in RunProxy).
func (c *Client) runProxyOnce(ctx context.Context, proxyID, udpHost string, udpPort int32, onPlayAudio PlayAudioFunc, onReady func()) (bool, error) {
	var gotAck atomic.Bool

	stream, err := c.stub.RegisterProxy(ctx)
	if err != nil {
		return false, fmt.Errorf("open RegisterProxy stream: %w", err)
	}

	// Identify ourselves and advertise our UDP address immediately
	if err := stream.Send(&pb.ProxyHeartbeat{
		ProxyId: proxyID,
		UdpHost: udpHost,
		UdpPort: udpPort,
	}); err != nil {
		return false, fmt.Errorf("send initial heartbeat: %w", err)
	}
	slog.Info("RegisterProxy stream opened", "proxy_id", proxyID, "udp_host", udpHost, "udp_port", udpPort)

	// Dispatches PlayAudioCommand chunks to one worker goroutine per device,
	// so a slow/playing satellite never delays chunks for other satellites.
	dispatcher := newPlayAudioDispatcher(onPlayAudio)
	defer dispatcher.stop()

	// Receive loop
	recvErr := make(chan error, 1)
	go func() {
		for {
			cmd, err := stream.Recv()
			if err != nil {
				recvErr <- err
				return
			}
			switch v := cmd.Command.(type) {
			case *pb.ProxyCommand_Ack:
				gotAck.Store(true)
				slog.Info("registered with Hannah Core",
					"udp_disabled", v.Ack.UdpDisabled,
					"message", v.Ack.Message)
				if onReady != nil {
					go onReady()
				}
			case *pb.ProxyCommand_PlayAudio:
				slog.Info("PlayAudioCommand received",
					"device", v.PlayAudio.DeviceId,
					"bytes", len(v.PlayAudio.AudioPcm),
					"sample_rate", v.PlayAudio.SampleRate,
					"is_last", v.PlayAudio.IsLast)
				if onPlayAudio != nil {
					dispatcher.dispatch(v.PlayAudio.DeviceId, v.PlayAudio.AudioPcm, v.PlayAudio.SampleRate, v.PlayAudio.IsLast)
				}
			}
		}
	}()

	// Heartbeat ticker
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			stream.CloseSend() //nolint:errcheck
			return gotAck.Load(), nil
		case err := <-recvErr:
			return gotAck.Load(), fmt.Errorf("recv: %w", err)
		case <-ticker.C:
			if err := stream.Send(&pb.ProxyHeartbeat{
				ProxyId: proxyID,
				UdpHost: udpHost,
				UdpPort: udpPort,
			}); err != nil {
				return gotAck.Load(), fmt.Errorf("send heartbeat: %w", err)
			}
		}
	}
}
