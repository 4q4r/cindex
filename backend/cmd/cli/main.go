// Command cli is the operator toolbox: migrations and admin commands.
package main

import (
	"log/slog"
	"os"

	"github.com/4q4r/cindex/backend/internal/config"
	"github.com/4q4r/cindex/backend/internal/platform/db"
	"github.com/4q4r/cindex/backend/migrations"
	"github.com/spf13/cobra"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	root := &cobra.Command{
		Use:   "cindex-cli",
		Short: "CIndex operator toolbox",
	}
	root.AddCommand(migrateCmd(logger))
	if err := root.Execute(); err != nil {
		logger.Error("command failed", "error", err)
		os.Exit(1)
	}
}

func migrateCmd(logger *slog.Logger) *cobra.Command {
	return &cobra.Command{
		Use:   "migrate",
		Short: "Apply pending database migrations",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			cfg, err := config.Load()
			if err != nil {
				return err
			}
			pool, err := db.Open(cmd.Context(), cfg.DatabaseURL)
			if err != nil {
				return err
			}
			defer pool.Close()
			if err := db.Migrate(cmd.Context(), pool, migrations.FS); err != nil {
				return err
			}
			logger.Info("migrations applied")
			return nil
		},
	}
}
