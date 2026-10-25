# Findings log

Results as they are produced, in the order they were found, with the evidence for each. This is
the raw material for the mid-semester report (week 8) and the results chapter. Numbers come from
files under `reports/`; rerun the listed command to reproduce them.

---

## F1. The trace total is too flat to compare scalers (2026-10-10)

`pas data build` with `trace.signal: total`: the sum of all functions has a daily
autocorrelation of 0.62 but swings only about 1.5× across a day (≈200–300 RPS once scaled).
Scaled to the cluster, the autoscaler would move between roughly 11 and 15 replicas. → ADR-010.

## F2. A single application gives a realistic, wide-range signal (2026-10-10)

`pas data apps --plot` ranks the 60 busiest applications on sustained daily range (p95/p5 of the
15-minute median), daily regularity and idle share (`reports/figures/apps.png`,
`reports/apps.csv`). A first ranking based on the coefficient of variation favoured an app
whose load is mostly one-minute bursts of up to 300 RPS on a 2–35 RPS base. The CV rewards
bursts, so the metric was changed to a burst-robust median.

Chosen: `70ac91b1c221…`.

| Statistic (scaled, 14 days) | Value |
|---|---|
| Mean / peak RPS | 55.6 / 375.7 |
| Daily autocorrelation (15-min median) | 0.85 |
| Sustained daily range | > 100× (idle at night) |
| Idle minutes | 8 % |
| Scale factor to cluster | 1.081 (99.5th percentile → 300 RPS = 75 % of 20 pods × 20 RPS) |

Two night-time surges occur **at the same minute every day** (18:02 and 20:47), from almost idle
to ≈60–140 RPS. These are predictable sudden surges, the case where prediction should help
most. They became window W4.

Replay windows (`pas data build`, test days only):

| Window | Start (trace time) | Minutes | Peak RPS | Peak replicas |
|---|---|---|---|---|
| W1 ramp | 07-27 06:10 | 120 | 333.7 | 17 |
| W2 peak | 07-28 07:20 | 120 | 261.2 | 14 |
| W3 = W2 + flash crowd at min 60 | 07-28 07:20 | 120 | 380.0 (capped) | 19 |
| W4 scheduled surge | 07-28 20:30 | 30 | 142.3 | 8 |
| WD demo ramp | 07-27 07:00 | 30 | 268.3 | 14 |

## F3. Fitting ARIMA on log load needs a bias correction (2026-10-11)

The first ARIMA(2,0,1) under-predicted 58–67 % of the time on held-out days: exp of a log-scale
forecast is the median, not the mean. With the log-normal correction and order (1,1,1), selected
on validation days, under-prediction at 1 minute fell to 33 % on the test days. → ADR-012.

## F4. Offline accuracy on the test days (RQ1) (2026-10-11)

`pas backtest naive last arima lstm lstm-p90` (days 13–14, a forecast every 5 minutes, 574
origins per horizon). MASE is relative to the in-sample seasonal-naive error on the training
days; < 1 is better than "same minute yesterday" on training data.

| Model | MASE h=1 | h=3 | h=5 | h=10 | Under-predicted h=3 | Inference |
|---|---|---|---|---|---|---|
| Seasonal naive | 1.78 | 1.81 | 1.76 | 1.76 | 41 % | <1 ms |
| Last value | **0.33** | **0.56** | 0.67 | 0.78 | 49 % | <1 ms |
| ARIMA(1,1,1)+Fourier | 0.36 | 0.67 | 0.87 | 1.33 | 26 % | 84 ms |
| LSTM (MSE) | 0.58 | 0.66 | **0.64** | **0.65** | 53 % | 5 ms |
| LSTM (P90) | 0.72 | 0.73 | 0.83 | 0.88 | 21 % | 5 ms |

Quantile forecasts (`--quantile 0.9`, plus LSTM-P90). A well-calibrated P90 is exceeded 10 % of
the time. Lower pinball loss is better.

| Model | Exceeded (h=1 / h=3 / h=10) | Pinball h=1 | h=3 | h=10 |
|---|---|---|---|---|
| Seasonal naive P90 | 10 / 10 / 10 % | 10.6 | 10.8 | 10.4 |
| Last value P90 | 12 / 13 / 12 % | **1.8** | 3.7 | 4.9 |
| ARIMA P90 | 3 / 3 / 3 % | 3.1 | 5.4 | 10.6 |
| LSTM P90 | 15 / 21 / 20 % | 2.0 | **2.4** | **2.5** |

Observations:

1. **Persistence is strong at short horizons.** Nothing beats "last value" at 1–3 minutes.
   Within the lead window of a pod that becomes ready in about 1–2 minutes, a predictive scaler
   may therefore gain little over reactive scaling plus headroom. This makes the start-up-delay
   sensitivity study (H5) central.
2. **The LSTM is the best model from 5 minutes on** (MASE 0.64–0.65, flat across horizons),
   i.e. exactly when provisioning lag is long (warm-up 60–120 s or new nodes).
3. **Same-minute-yesterday is poor on these days** (MASE 1.78). Days 13–14 differ from their
   previous days more than the training days do. Its only strength is the scheduled surges (W4).
4. **ARIMA's prediction interval is too wide** (3 % exceedance instead of 10 %), so `arima-p90`
   will over-provision. The LSTM-P90 is slightly under-calibrated but has the lowest pinball loss
   from 3 minutes on: the best candidate for uncertainty-aware scaling (E1).

Next: live runs on the local kind cluster (capacity, lag, then the experiment matrix; ADR-013)
to see whether forecast accuracy translates into fewer SLO-violation minutes (RQ2).
