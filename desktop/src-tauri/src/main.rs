// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // Elevated helper mode for the "Clean memory" button (ADR-003): purge and exit, no window.
    if std::env::args().any(|a| a == desktop_lib::memory_cleaner::PURGE_FLAG) {
        std::process::exit(desktop_lib::memory_cleaner::perform_privileged_purge());
    }
    desktop_lib::run()
}
