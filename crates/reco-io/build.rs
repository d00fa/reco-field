use std::path::PathBuf;
use std::process::Command;

fn pc_major(path: PathBuf) -> Option<u32> {
    let contents = std::fs::read_to_string(path).ok()?;
    contents.lines().find_map(|line| {
        line.strip_prefix("Version:")
            .and_then(|version| version.trim().split('.').next())
            .and_then(|major| major.parse().ok())
    })
}

fn ffmpeg_major(libavutil_major: u32) -> u32 {
    // libavutil's major version tracks FFmpeg releases with an offset.
    match libavutil_major {
        0..=55 => 3,
        56 => 4,
        57 => 5,
        58 => 6,
        59 => 7,
        _ => 8,
    }
}

fn main() {
    println!("cargo:rustc-check-cfg=cfg(reco_ffmpeg_vaapi_vld)");
    println!("cargo:rerun-if-env-changed=RECO_FFMPEG_MAJOR");
    println!("cargo:rerun-if-env-changed=PKG_CONFIG");
    println!("cargo:rerun-if-env-changed=PKG_CONFIG_PATH");
    let pkg_config = std::env::var("PKG_CONFIG").unwrap_or_else(|_| "pkg-config".into());

    let major = std::env::var("RECO_FFMPEG_MAJOR")
        .ok()
        .and_then(|value| value.parse::<u32>().ok())
        .or_else(|| {
            let paths = std::env::var_os("PKG_CONFIG_PATH")?;
            std::env::split_paths(&paths)
                .find_map(|directory| pc_major(directory.join("libavutil.pc")))
                .map(ffmpeg_major)
        })
        .or_else(|| {
            let output = Command::new(&pkg_config)
                .args(["--modversion", "libavutil"])
                .output();
            let output = match output {
                Ok(output) => output,
                Err(error) => {
                    eprintln!("reco-io: cannot run pkg-config: {error}");
                    return None;
                }
            };
            if !output.status.success() {
                eprintln!(
                    "reco-io: pkg-config failed: {}",
                    String::from_utf8_lossy(&output.stderr).trim()
                );
                return None;
            }
            String::from_utf8(output.stdout)
                .ok()?
                .split('.')
                .next()?
                .parse()
                .map(ffmpeg_major)
                .ok()
        });

    if major.is_some_and(|value| value < 5) {
        println!("cargo:rustc-cfg=reco_ffmpeg_vaapi_vld");
    }
}
