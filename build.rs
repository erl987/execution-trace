#![allow(clippy::unwrap_used)]

use std::env;
use std::path::PathBuf;

fn main() {
    // Skip codegen when the `enabled` feature is off; the proto module and
    // encode module are both gated on that feature so no generated file is needed.
    if env::var("CARGO_FEATURE_ENABLED").is_err() {
        return;
    }

    println!("cargo:rerun-if-changed=proto/tracing.proto");

    let out_file = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("tracing.pb.rs");

    let mut generator = micropb_gen::Generator::new();
    generator.encode_decode(micropb_gen::EncodeDecode::Both);
    generator.add_protoc_arg("-Iproto");

    // name carries the name of the source task/ISR (for spans) or the marker label.
    // It appears only on NAME_REGISTERED frames, once per distinct name (§5.5).
    generator.configure(
        ".tracing.TraceFrame.name",
        micropb_gen::Config::new()
            .string_type("heapless::String<$N>")
            .max_bytes(32),
    );

    generator
        .compile_protos(&["proto/tracing.proto"], &out_file)
        .unwrap();
}
