//! Compiles the video and sound decoder (media/pensieve-media.swift) with swiftc. src/media.rs puts its bytes into
//! the pensieve binary, so that one file installs both.

use std::path::PathBuf;
use std::process::Command;

fn main() {
    let src = "media/pensieve-media.swift";
    println!("cargo:rerun-if-changed={src}");
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("pensieve-media");
    let status = Command::new("swiftc")
        .args(["-O", "-swift-version", "5", "-target", "arm64-apple-macosx14.0", src, "-o"])
        .arg(&out)
        .status()
        .unwrap_or_else(|e| panic!("swiftc is not here ({e}): install the Xcode Command Line Tools"));
    assert!(status.success(), "swiftc failed on {src}");
}
