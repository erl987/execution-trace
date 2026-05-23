#![allow(clippy::unwrap_used)]

use std::env;
use std::path::PathBuf;

fn main() {
    println!("cargo:rerun-if-changed=proto/tracing.proto");

    let out_file = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("tracing.pb.rs");

    let mut generator = micropb_gen::Generator::new();
    generator.encode_decode(micropb_gen::EncodeDecode::Both);
    generator.add_protoc_arg("-Iproto");

    // name carries the name of the source task/ISR (for spans) or the marker label.
    generator.configure(
        ".tracing.TraceEvent.name",
        micropb_gen::Config::new()
            .string_type("heapless::String<$N>")
            .max_bytes(32),
    );

    generator
        .compile_protos(&["proto/tracing.proto"], &out_file)
        .unwrap();
}
