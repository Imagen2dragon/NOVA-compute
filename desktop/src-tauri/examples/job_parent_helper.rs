use desktop_lib::job_object::JobObject;
use std::io::Write;
use std::time::Duration;

fn main() {
    let job = match JobObject::new() {
        Ok(j) => j,
        Err(e) => {
            eprintln!("Failed to create JobObject: {}", e);
            std::process::exit(1);
        }
    };

    let child = match job.spawn_child_suspended(
        "powershell.exe -NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 30\"",
    ) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to spawn child: {}", e);
            std::process::exit(2);
        }
    };

    println!("CHILD_PID:{}", child.pid);
    let _ = std::io::stdout().flush();

    // Keep helper parent process alive waiting to be killed
    std::thread::sleep(Duration::from_secs(60));
}
