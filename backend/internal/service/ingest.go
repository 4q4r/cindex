package service

import (
	"context"
	"log/slog"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
)

// ProgressEvent is a coarse live-scan progress update emitted by ingestors.
type ProgressEvent struct {
	Substage      string
	SubstageLabel string
	DoneSources   int
	TotalSources  int
	FailedSources []string
	CompletedKeys []string
	Timing        domain.SourceTiming
	TimingSource  string
}

// ProfileEvent is a per-source profile event emitted by ingestors.
type ProfileEvent struct {
	SourceKey     string
	SourceName    string
	Status        string
	StartTime     time.Time
	EndTime       time.Time
	ArticlesCount int
}

// IngestOptions controls a single live-scan pass.
type IngestOptions struct {
	SourceKeys          []string
	PerSourceLimit      int
	ResumeCompletedKeys []string
	InitialDone         int
	InitialFailed       []string
	Progress            func(ProgressEvent)
	Profile             func(ProfileEvent)
}

// Ingestor performs corpus live scans and source-health reporting. The
// stage-2 implementation is a no-op placeholder: the corpus is still
// populated by the Django connectors sharing the same database. Real
// connectors replace it in stage 4.
type Ingestor interface {
	// IngestQuery runs a live scan for a query. Returns (completedKeys,
	// failedSources, error).
	IngestQuery(ctx context.Context, query string, opts IngestOptions) ([]string, []string, error)
}

// NoopIngestor is the honest placeholder ingestor for stage 2.
type NoopIngestor struct {
	Logger *slog.Logger
}

// IngestQuery does nothing yet and reports the gap loudly.
func (n *NoopIngestor) IngestQuery(_ context.Context, _ string, _ IngestOptions) ([]string, []string, error) {
	if n.Logger != nil {
		n.Logger.Warn("live scan requested but connectors land in stage 4; corpus is fed by the Django stack")
	}
	return nil, nil, nil
}

// SourceStatsService computes aggregated source health with parity to
// apps.search.progress._compute_source_stats.
type SourceStatsService struct {
	Sources *repository.Sources
}

// Compute returns total/failed/live counters. Failed entries use the source
// name when set, otherwise the uppercased key.
func (s *SourceStatsService) Compute(ctx context.Context) (total int, failed []string, live int, err error) {
	sources, err := s.Sources.ListAll(ctx)
	if err != nil {
		return 0, nil, 0, err
	}
	health := make(map[string]string, len(sources))
	names := make(map[string]string, len(sources))
	for _, src := range sources {
		if !src.Active {
			health[src.Key] = "down"
		} else if src.CircuitOpenUntil != nil && src.CircuitOpenUntil.After(time.Now()) {
			health[src.Key] = "down"
		} else {
			health[src.Key] = "healthy"
		}
		if src.Name != "" {
			names[src.Key] = src.Name
		}
	}
	total, failed, live = ComputeSourceStats(health, names)
	return total, failed, live, nil
}

// ComputeSourceStats turns a health map into parity counters: failed entries
// use the display name when present, otherwise the uppercased key; live =
// total - failed, floored at zero.
func ComputeSourceStats(health map[string]string, names map[string]string) (int, []string, int) {
	total := len(health)
	failed := make([]string, 0)
	for key, status := range health {
		if status != "healthy" {
			if name := names[key]; name != "" {
				failed = append(failed, name)
			} else {
				failed = append(failed, upperASCII(key))
			}
		}
	}
	live := total - len(failed)
	if live < 0 {
		live = 0
	}
	return total, failed, live
}

func upperASCII(s string) string {
	b := []byte(s)
	for i := range b {
		if b[i] >= 'a' && b[i] <= 'z' {
			b[i] -= 'a' - 'A'
		}
	}
	return string(b)
}
