package hannah

import (
	_ "embed"
	"strings"
)

// ProtoVersion is Hannah's protocol version (#60), embedded at build time from
// the local PROTO_VERSION copy (see ../../gen_proto.sh — mitkopiert von
// hannah-proto, da die Proxy-Release-Binary keinen Dateisystemzugriff auf das
// proto/-Submodule hat).
//
//go:embed PROTO_VERSION
var protoVersionRaw string

var ProtoVersion = strings.TrimSpace(protoVersionRaw)
