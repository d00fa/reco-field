fn main() {
    println!("cargo:rerun-if-changed=native/opencv_seam.cpp");
    #[cfg(feature = "opencv-seam")]
    {
        let opencv = pkg_config::Config::new()
            .cargo_metadata(false)
            .atleast_version("4.5")
            .probe("opencv4")
            .expect("opencv-seam requires OpenCV development files (pkg-config opencv4)");
        let mut build = cc::Build::new();
        build
            .cpp(true)
            .file("native/opencv_seam.cpp")
            .flag_if_supported("-std=c++17");
        let mut headers = false;
        for path in &opencv.include_paths {
            if path
                .join("opencv2/stitching/detail/seam_finders.hpp")
                .exists()
            {
                build.include(path);
                headers = true;
            }
        }
        // Some JetPack packages retain a build-time /usr/local prefix in opencv4.pc.
        if !headers
            && std::path::Path::new(
                "/usr/include/opencv4/opencv2/stitching/detail/seam_finders.hpp",
            )
            .exists()
        {
            build.include("/usr/include/opencv4");
            headers = true;
        }
        assert!(headers, "OpenCV stitching headers were not found");
        build.compile("reco_opencv_seam");
        for path in opencv.link_paths {
            if path.exists() {
                println!("cargo:rustc-link-search=native={}", path.display());
            }
        }
        for library in ["opencv_stitching", "opencv_imgproc", "opencv_core"] {
            println!("cargo:rustc-link-lib={library}");
        }
    }
}
