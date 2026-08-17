// Package redis provides the Redis client used for rate limiting and
// distributed locking.
package redis

import (
	"context"
	"fmt"
	"time"

	goredis "github.com/redis/go-redis/v9"
)

// Open creates a Redis client. The caller must Close it.
func Open(ctx context.Context, url string) (*goredis.Client, error) {
	opts, err := goredis.ParseURL(url)
	if err != nil {
		return nil, fmt.Errorf("parse redis url: %w", err)
	}
	client := goredis.NewClient(opts)
	if err := client.Ping(ctx).Err(); err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("ping redis: %w", err)
	}
	return client, nil
}

// PingTimeout bounds the startup ping.
const PingTimeout = 5 * time.Second
