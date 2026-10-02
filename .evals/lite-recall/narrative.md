# Baseline status

| run group | source comparison | correctness | run p50 (ms) |
| --- | --- | ---: | ---: |
| Baseline, 5 processes | working tree | 1800/1800 | median 0.02000; range 0.01910–0.02480 |
| Sequential no-change control, 5 processes | same source, later group | 1800/1800 | median 0.02775; range 0.02505–0.04020 |
| Paired no-change calibration, 15 pairs | source hashes identical | 10800/10800 | median relative delta −0.81%; paired 95% half-width 0.00209 ms |

The sequential groups drifted by 38.8%, but the counterbalanced calibration reduced the uncertainty enough to resolve the accepted 20% action bar at the observed baseline scale: its 95% paired interval half-width is about 10.5% of baseline p50. The individual paired deltas remain noisy, so candidate comparisons must use all 15 pairs and preserve correctness. No application code changed. Details are in `metrics.md` and the paired summary JSON; regenerable per-call outputs are kept local.
