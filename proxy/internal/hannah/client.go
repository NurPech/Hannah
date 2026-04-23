// Package hannah provides a gRPC client to Hannah Core.
package hannah

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	pb "dev.kernstock.net/gessinger/voice/hannah/proxy/proto/hannah"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// PlayAudioFunc is called when Hannah pushes a PlayAudioCommand via the proxy stream.
// The proxy should play pcm (raw 16-bit signed mono) on the given satellite.
type PlayAudioFunc func(deviceID string, pcm []byte, sampleRate int32)

// Client is a gRPC client to Hannah Core.
type Client struct {
	conn *grpc.ClientConn
	stub pb.HannahServiceClient
}

// NewClient dials Hannah Core at address (e.g. "192.168.8.1:50051").
func NewClient(address string) (*Client, error) {
	conn, err := grpc.NewClient(address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
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
// speakerRoomieID is the result of a prior Voice-ID lookup; pass "" if unknown.
func (c *Client) SubmitSatelliteAudio(ctx context.Context, deviceID, room string, pcm []byte, speakerRoomieID string) (*pb.SubmitSatelliteAudioResponse, error) {
	return c.stub.SubmitSatelliteAudio(ctx, &pb.SubmitSatelliteAudioRequest{
		DeviceId:        deviceID,
		Room:            room,
		AudioPcm:        pcm,
		SampleRate:      16000,
		SpeakerRoomieId: speakerRoomieID,
	})
}

// NotifySatelliteRegistered tells Hannah Core that a satellite has connected via the proxy.
func (c *Client) NotifySatelliteRegistered(ctx context.Context, deviceID, room string) error {
	_, err := c.stub.NotifySatelliteRegistered(ctx, &pb.SatelliteRegistration{
		DeviceId: deviceID,
		Room:     room,
	})
	return err
}

// NotifySatelliteGone tells Hannah Core that a satellite has disconnected from the proxy.
func (c *Client) NotifySatelliteGone(ctx context.Context, deviceID, room string) error {
	_, err := c.stub.NotifySatelliteGone(ctx, &pb.SatelliteRegistration{
		DeviceId: deviceID,
		Room:     room,
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
// Blocks until ctx is cancelled. Reconnects automatically with a 5s backoff.
func (c *Client) RunProxy(ctx context.Context, proxyID, udpHost string, udpPort int32, onPlayAudio PlayAudioFunc, onReady func()) {
	for {
		err := c.runProxyOnce(ctx, proxyID, udpHost, udpPort, onPlayAudio, onReady)
		if ctx.Err() != nil {
			return // clean shutdown
		}
		slog.Warn("RegisterProxy stream lost, reconnecting in 5s", "err", err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}

func (c *Client) runProxyOnce(ctx context.Context, proxyID, udpHost string, udpPort int32, onPlayAudio PlayAudioFunc, onReady func()) error {
	stream, err := c.stub.RegisterProxy(ctx)
	if err != nil {
		return fmt.Errorf("open RegisterProxy stream: %w", err)
	}

	// Identify ourselves and advertise our UDP address immediately
	if err := stream.Send(&pb.ProxyHeartbeat{
		ProxyId: proxyID,
		UdpHost: udpHost,
		UdpPort: udpPort,
	}); err != nil {
		return fmt.Errorf("send initial heartbeat: %w", err)
	}
	slog.Info("RegisterProxy stream opened", "proxy_id", proxyID, "udp_host", udpHost, "udp_port", udpPort)

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
					"sample_rate", v.PlayAudio.SampleRate)
				if onPlayAudio != nil {
					go onPlayAudio(v.PlayAudio.DeviceId, v.PlayAudio.AudioPcm, v.PlayAudio.SampleRate)
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
			return nil
		case err := <-recvErr:
			return fmt.Errorf("recv: %w", err)
		case <-ticker.C:
			if err := stream.Send(&pb.ProxyHeartbeat{
				ProxyId: proxyID,
				UdpHost: udpHost,
				UdpPort: udpPort,
			}); err != nil {
				return fmt.Errorf("send heartbeat: %w", err)
			}
		}
	}
}
