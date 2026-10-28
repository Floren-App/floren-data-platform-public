//! The desktop hub is deliberately thin: no local compute, no local data.
//! It renders the hub UI and opens server-hosted editors/apps in their own windows.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Floren Data Platform");
}
