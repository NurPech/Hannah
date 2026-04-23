MODULE := dev.kernstock.net/gessinger/voice/hannah/proxy

.PHONY: build run proto tidy

build:
	go build -o bin/proxy ./cmd/proxy

run:
	go run ./cmd/proxy --config config.yaml

tidy:
	go mod tidy

# Generates Go proto stubs from ../core/proto/hannah.proto into proto/hannah/.
# Requires: protoc, protoc-gen-go, protoc-gen-go-grpc
#   go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
#   go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
proto:
	@which protoc > /dev/null 2>&1 || \
		(echo "protoc not found — see https://grpc.io/docs/protoc-installation/" && exit 1)
	@which protoc-gen-go > /dev/null 2>&1 || \
		go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
	@which protoc-gen-go-grpc > /dev/null 2>&1 || \
		go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
	mkdir -p proto/hannah
	protoc \
		-I ../core/proto \
		--go_out=proto/hannah \
		--go_opt=paths=source_relative \
		--go_opt=Mhannah.proto=$(MODULE)/proto/hannah \
		--go-grpc_out=proto/hannah \
		--go-grpc_opt=paths=source_relative \
		--go-grpc_opt=Mhannah.proto=$(MODULE)/proto/hannah \
		../core/proto/hannah.proto
	@echo "✓ Proto stubs generated in proto/hannah/"
