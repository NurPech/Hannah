package hannah

import (
	"context"
	"testing"

	pb "dev.kernstock.net/gessinger/voice/hannah/proxy/proto/hannah"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

func TestProtoVersion_ReadFromEmbeddedFile(t *testing.T) {
	if pb.ProtoVersion == "" {
		t.Fatal("pb.ProtoVersion is empty — embed failed or PROTO_VERSION file missing")
	}
}

func TestVersionUnaryInterceptor_AttachesMetadata(t *testing.T) {
	var capturedCtx context.Context
	invoker := func(ctx context.Context, method string, req, reply interface{}, cc *grpc.ClientConn, opts ...grpc.CallOption) error {
		capturedCtx = ctx
		return nil
	}

	err := versionUnaryInterceptor(context.Background(), "/hannah.HannahService/NotifySatelliteGone", nil, nil, nil, invoker)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	md, ok := metadata.FromOutgoingContext(capturedCtx)
	if !ok {
		t.Fatal("no outgoing metadata attached")
	}
	got := md.Get(ProtoVersionMetadataKey)
	if len(got) != 1 || got[0] != pb.ProtoVersion {
		t.Fatalf("expected %s=%q, got %v", ProtoVersionMetadataKey, pb.ProtoVersion, got)
	}
}

func TestVersionUnaryInterceptor_PreservesExistingMetadata(t *testing.T) {
	var capturedCtx context.Context
	invoker := func(ctx context.Context, method string, req, reply interface{}, cc *grpc.ClientConn, opts ...grpc.CallOption) error {
		capturedCtx = ctx
		return nil
	}

	ctx := metadata.AppendToOutgoingContext(context.Background(), "existing", "value")
	err := versionUnaryInterceptor(ctx, "/hannah.HannahService/NotifySatelliteGone", nil, nil, nil, invoker)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	md, _ := metadata.FromOutgoingContext(capturedCtx)
	if got := md.Get("existing"); len(got) != 1 || got[0] != "value" {
		t.Fatalf("existing metadata was dropped: %v", got)
	}
	if got := md.Get(ProtoVersionMetadataKey); len(got) != 1 || got[0] != pb.ProtoVersion {
		t.Fatalf("version metadata missing: %v", got)
	}
}

func TestVersionStreamInterceptor_AttachesMetadata(t *testing.T) {
	var capturedCtx context.Context
	streamer := func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		capturedCtx = ctx
		return nil, nil
	}

	_, err := versionStreamInterceptor(context.Background(), &grpc.StreamDesc{}, nil, "/hannah.HannahService/RegisterProxy", streamer)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	md, ok := metadata.FromOutgoingContext(capturedCtx)
	if !ok {
		t.Fatal("no outgoing metadata attached")
	}
	got := md.Get(ProtoVersionMetadataKey)
	if len(got) != 1 || got[0] != pb.ProtoVersion {
		t.Fatalf("expected %s=%q, got %v", ProtoVersionMetadataKey, pb.ProtoVersion, got)
	}
}
