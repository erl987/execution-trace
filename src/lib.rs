#![cfg_attr(not(feature = "std"), no_std)]
#![doc(
    html_logo_url = "https://www.rust-lang.org/logos/rust-logo-128x128-blk.png",
    html_favicon_url = "https://www.rust-lang.org/favicon.ico"
)]
// README examples require the `enabled` feature; suppress doc-tests when it is off.
#![cfg_attr(feature = "enabled", doc = include_str!("../README.md"))]

#[cfg(feature = "enabled")]
pub mod encode;

/// The producer-side sequence counter.
///
/// Numbering at record time rather than in the transport is what makes loss at
/// the producer queue visible: a frame dropped there has already burned a
/// number, so the host sees a gap instead of a contiguous stream with events
/// simply missing (§2.6).
#[cfg(feature = "enabled")]
mod sequence {
    /// Folds a freely running counter into the wire's sequence range.
    ///
    /// Applied on read rather than by keeping the stored counter in range:
    /// `fetch_add` cannot wrap at a non-power-of-two bound atomically, and 2³² is
    /// an exact multiple of 16 384, so taking the modulus of a wrapping `u32`
    /// yields the same sequence either way.
    fn wrap(raw: u32) -> u32 {
        raw % crate::encode::SEQUENCE_MODULUS
    }

    /// Takes the next sequence number.
    ///
    /// One `fetch_add` — on Cortex-M4 an `LDREX`/`STREX` pair, so no critical
    /// section and nothing for a preempting ISR to block on. The counter is
    /// process-global because every producer and the transport itself must draw
    /// from one space for a gap to mean anything.
    #[cfg(not(test))]
    pub fn next_sequence() -> u32 {
        use core::sync::atomic::{AtomicU32, Ordering};
        static SEQUENCE: AtomicU32 = AtomicU32::new(0);
        wrap(SEQUENCE.fetch_add(1, Ordering::Relaxed))
    }

    // Under test the counter is per-thread. The harness runs each test on its own
    // thread, so a shared global would make every test that asserts an absolute
    // number depend on whichever others happened to run first. The production
    // path above is the one §5.7 specifies; `wrap`, which is where the only
    // arithmetic lives, is shared by both and tested directly.
    #[cfg(test)]
    std::thread_local! {
        static SEQUENCE: core::cell::Cell<u32> = const { core::cell::Cell::new(0) };
    }

    #[cfg(test)]
    pub fn next_sequence() -> u32 {
        SEQUENCE.with(|c| {
            let taken = c.get();
            c.set(taken.wrapping_add(1));
            wrap(taken)
        })
    }

    /// Restarts this thread's counter. Tests only.
    #[cfg(test)]
    pub fn reset_sequence() {
        SEQUENCE.with(|c| c.set(0));
    }

    #[cfg(test)]
    mod tests {
        use super::wrap;
        use crate::encode::SEQUENCE_MODULUS;

        #[test]
        fn wrap_folds_into_the_sequence_range() {
            assert_eq!(wrap(0), 0);
            assert_eq!(wrap(SEQUENCE_MODULUS - 1), SEQUENCE_MODULUS - 1);
            assert_eq!(wrap(SEQUENCE_MODULUS), 0, "wraps to zero, not to 16384");
            assert_eq!(wrap(SEQUENCE_MODULUS + 1), 1);
        }

        #[test]
        fn wrap_is_continuous_across_the_u32_boundary() {
            // 2^32 is an exact multiple of the modulus, so a counter rolling over
            // u32::MAX stays continuous in sequence space — which is what lets the
            // stored counter run free instead of being masked on every increment.
            assert_eq!(wrap(u32::MAX), SEQUENCE_MODULUS - 1);
            assert_eq!(wrap(u32::MAX.wrapping_add(1)), 0);
        }
    }
}

#[cfg(feature = "enabled")]
pub use sequence::next_sequence;
#[cfg(all(test, feature = "enabled"))]
pub(crate) use sequence::reset_sequence;
mod sink;
mod types;

#[cfg(feature = "enabled")]
mod proto {
    #![allow(
        dead_code,
        non_camel_case_types,
        non_snake_case,
        non_upper_case_globals,
        unused_imports,
        unused_parens,
        unused_mut,
        unused_variables,
        unused_labels,
        clippy::useless_conversion,
        clippy::needless_borrow,
        clippy::collapsible_if,
        clippy::collapsible_match,
        clippy::match_single_binding,
        clippy::let_and_return,
        clippy::needless_late_init,
        clippy::derivable_impls,
        clippy::borrow_deref_ref,
        clippy::blocks_in_conditions
    )]
    include!(concat!(env!("OUT_DIR"), "/tracing.pb.rs"));
}

#[cfg(feature = "enabled")]
pub use encode::{FrameKind, RawTraceFrame, TimeBase, TraceEncoder};
#[cfg(feature = "enabled")]
pub use sink::TraceTransport;
pub use sink::{NoopSink, TraceSink, TracingError};
pub use types::SourceType;
#[cfg(feature = "enabled")]
pub use types::TraceEvent;
