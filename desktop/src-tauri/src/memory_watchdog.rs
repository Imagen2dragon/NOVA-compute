//! Live memory watchdog for a running llama-server (Project Owner decision 2026-11-07).
//!
//! The Low-Memory Streaming profile admits a load with only 4 GiB of free RAM, so safety
//! while the model is loading and generating comes from watching the machine, not from a
//! large up-front requirement. The watchdog samples free physical RAM and system commit and
//! tells the caller to stop the server when:
//!   - free RAM stays below `critical_avail_gib` for `sustain_secs` (sustained thrashing), or
//!   - system commit reaches `critical_commit_pct` (risk of allocation failures system-wide), or
//!   - memory telemetry fails `max_consecutive_failures` times in a row (fail closed).
//!
//! It never closes or touches any other application.

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct WatchdogPolicy {
    pub critical_avail_gib: f64,
    pub sustain_secs: f64,
    pub critical_commit_pct: f64,
    pub max_consecutive_failures: u32,
    pub interval_ms: u64,
}

impl Default for WatchdogPolicy {
    fn default() -> Self {
        Self {
            critical_avail_gib: 1.5,
            sustain_secs: 5.0,
            critical_commit_pct: 90.0,
            max_consecutive_failures: 4,
            interval_ms: 500,
        }
    }
}

#[derive(Debug, Default)]
pub struct MemoryWatchdog {
    low_since_secs: Option<f64>,
    consecutive_failures: u32,
    min_avail_gib: Option<f64>,
    max_commit_pct: Option<f64>,
}

impl MemoryWatchdog {
    pub fn new() -> Self {
        Self::default()
    }

    /// Lowest free physical RAM (GiB) observed across all valid samples so far.
    pub fn min_avail_gib_seen(&self) -> Option<f64> {
        self.min_avail_gib
    }

    /// Highest system commit percentage observed across all valid samples so far.
    pub fn max_commit_pct_seen(&self) -> Option<f64> {
        self.max_commit_pct
    }

    /// Feeds one sample taken at `t_secs` (monotonic seconds). `sample` is
    /// `Ok((available_ram_gib, commit_pct))` or the telemetry error.
    /// Returns `Some(reason)` when the server must be stopped.
    pub fn observe(
        &mut self,
        policy: &WatchdogPolicy,
        sample: Result<(f64, f64), String>,
        t_secs: f64,
    ) -> Option<String> {
        let (avail_gib, commit_pct) = match sample {
            Ok((a, c)) if a.is_finite() && c.is_finite() && a >= 0.0 && (0.0..=100.0).contains(&c) => {
                self.consecutive_failures = 0;
                self.min_avail_gib = Some(match self.min_avail_gib {
                    Some(prev) => prev.min(a),
                    None => a,
                });
                self.max_commit_pct = Some(match self.max_commit_pct {
                    Some(prev) => prev.max(c),
                    None => c,
                });
                (a, c)
            }
            Ok((a, c)) => {
                return self.telemetry_failure(
                    policy,
                    format!("invalid memory reading (free RAM {a}, commit {c})"),
                )
            }
            Err(e) => return self.telemetry_failure(policy, e),
        };

        if commit_pct >= policy.critical_commit_pct {
            return Some(format!(
                "System commit reached {:.1}% (limit {:.0}%). The model was stopped to keep Windows stable.",
                commit_pct, policy.critical_commit_pct
            ));
        }

        if avail_gib < policy.critical_avail_gib {
            let since = *self.low_since_secs.get_or_insert(t_secs);
            if t_secs - since >= policy.sustain_secs {
                return Some(format!(
                    "Free RAM stayed below {:.1} GiB for {:.0} s (now {:.2} GiB). The model was stopped to keep Windows responsive. Close some applications and try again.",
                    policy.critical_avail_gib, policy.sustain_secs, avail_gib
                ));
            }
        } else {
            self.low_since_secs = None;
        }
        None
    }

    fn telemetry_failure(&mut self, policy: &WatchdogPolicy, err: String) -> Option<String> {
        self.consecutive_failures += 1;
        if self.consecutive_failures >= policy.max_consecutive_failures {
            Some(format!(
                "Memory telemetry failed {} times in a row ({}). The model was stopped (fail closed).",
                self.consecutive_failures, err
            ))
        } else {
            None
        }
    }
}
