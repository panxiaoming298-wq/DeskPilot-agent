fn main() {
    let manifest_root = std::path::PathBuf::from(
        std::env::var_os("CARGO_MANIFEST_DIR").expect("Cargo manifest root is unavailable"),
    );
    std::fs::create_dir_all(manifest_root.join("rt"))
        .expect("failed to create the generated Python Command Profile resource root");
    tauri_build::build()
}
