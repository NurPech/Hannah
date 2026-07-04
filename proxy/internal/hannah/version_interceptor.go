package hannah

import (
	"context"

	pb "dev.kernstock.net/gessinger/voice/hannah/proxy/proto/hannah"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// ProtoVersionMetadataKey is the gRPC metadata key Hannah Core's server
// interceptor checks against its own PROTO_VERSION (core/hannah/grpc_interceptors.py, #60).
const ProtoVersionMetadataKey = "x-proto-version"

// versionUnaryInterceptor attaches pb.ProtoVersion to every outgoing unary call.
func versionUnaryInterceptor(ctx context.Context, method string, req, reply interface{}, cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
	ctx = metadata.AppendToOutgoingContext(ctx, ProtoVersionMetadataKey, pb.ProtoVersion)
	return invoker(ctx, method, req, reply, cc, opts...)
}

// versionStreamInterceptor attaches pb.ProtoVersion to every outgoing streaming call
// (e.g. RegisterProxy).
func versionStreamInterceptor(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string, streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	ctx = metadata.AppendToOutgoingContext(ctx, ProtoVersionMetadataKey, pb.ProtoVersion)
	return streamer(ctx, desc, cc, method, opts...)
}
