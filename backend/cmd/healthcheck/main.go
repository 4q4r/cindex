// Command healthcheck probes the Go API from inside its distroless container.
package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"time"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:8000/healthz", nil)
	if err == nil {
		var resp *http.Response
		resp, err = http.DefaultClient.Do(req)
		if err == nil {
			defer func() { _ = resp.Body.Close() }()
			if resp.StatusCode == http.StatusOK {
				return
			}
			err = fmt.Errorf("health endpoint returned %s", resp.Status)
		}
	}
	_, _ = fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
