// Command demo-api is the workload under test: an HTTP service with a fixed CPU cost per
// request, a configurable warm-up before it reports ready, and Prometheus metrics.
//
// Endpoints:
//
//	GET /work?units=N  burn N units of CPU work (default DEFAULT_WORK_UNITS)
//	GET /healthz       liveness: 200 while the process is running
//	GET /readyz        readiness: 200 after STARTUP_DELAY_SECONDS, 503 while warming or draining
//	GET /metrics       Prometheus metrics
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg, err := LoadConfig(os.Getenv)
	if err != nil {
		logger.Error("invalid configuration", "error", err)
		os.Exit(2)
	}

	reg := prometheus.NewRegistry()
	srv := NewServer(cfg, reg, logger)
	httpServer := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()

	go srv.WarmUp(ctx)
	go func() {
		logger.Info("listening", "addr", httpServer.Addr, "startup_delay", cfg.StartupDelay.String(),
			"default_work_units", cfg.DefaultWorkUnits)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("server failed", "error", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	// Fail readiness first so Kubernetes removes the pod from the Service endpoints, then let
	// in-flight requests finish.
	srv.SetDraining()
	logger.Info("draining", "for", cfg.DrainDelay.String())
	time.Sleep(cfg.DrainDelay)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		logger.Error("shutdown", "error", err)
	}
	logger.Info("stopped")
}
