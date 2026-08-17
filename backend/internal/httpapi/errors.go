package httpapi

import (
	"encoding/json"
	"net/http"
)

// writeEnvelope writes a DRF-style error envelope:
// {"type": "validation_error", "errors": [{"code": "error", "detail": ...,
// "attr": ...}]}. Parity with apps.core.exceptions.
func writeEnvelope(w http.ResponseWriter, status int, envelopeType string, items []ErrorItem) {
	errs := items
	if errs == nil {
		errs = []ErrorItem{}
	}
	payload := ErrorEnvelope{
		Type:   strptr(envelopeType),
		Errors: &errs,
	}
	writeJSON(w, status, payload)
}

// writeDetailError emits a DRF-style bare {"detail": "..."} error (used for
// 404s and throttling, matching rest_framework.exceptions responses).
func writeDetailError(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]string{"detail": detail})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func strptr(s string) *string { return &s }

func intptr(n int) *int { return &n }
