#![cfg_attr(not(feature = "std"), no_std)]
#![doc(
    html_logo_url = "https://www.rust-lang.org/logos/rust-logo-128x128-blk.png",
    html_favicon_url = "https://www.rust-lang.org/favicon.ico"
)]
// README examples require the `enabled` feature; suppress doc-tests when it is off.
#![cfg_attr(feature = "enabled", doc = include_str!("../README.md"))]

#[cfg(feature = "enabled")]
pub mod encode;
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
pub use encode::SequenceEncoder;
#[cfg(feature = "enabled")]
pub use sink::TraceTransport;
pub use sink::{NoopSink, TraceSink, TracingError};
pub use types::SourceType;
#[cfg(feature = "enabled")]
pub use types::TraceEvent;
