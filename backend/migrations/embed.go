// Package migrations embeds the tern SQL migrations shipped with the binary.
package migrations

import "embed"

// FS embeds the tern SQL migrations shipped with the binary.
//
//go:embed *.sql
var FS embed.FS
