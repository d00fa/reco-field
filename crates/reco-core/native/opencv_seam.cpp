#include <opencv2/core.hpp>
#include <opencv2/core/version.hpp>
#include <opencv2/stitching/detail/seam_finders.hpp>
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <exception>
#include <vector>

extern "C" const char* reco_opencv_version() { return CV_VERSION; }

extern "C" int reco_opencv_graphcut(const uint8_t* left, const uint8_t* right,
                                    uint32_t width, uint32_t height, uint8_t* ownership,
                                    char* error, size_t error_length) noexcept {
    auto fail = [&](const char* message) {
        if (error && error_length) std::snprintf(error, error_length, "%s", message);
        return -1;
    };
    if (!left || !right || !ownership || !width || !height ||
        width > 768 || uint64_t(width) * height > 250000) return fail("invalid seam image dimensions or buffers");
    try {
        const cv::Size size(static_cast<int>(width), static_cast<int>(height));
        std::vector<cv::UMat> images(2), masks(2);
        size_t overlap = 0, exclusive_left = 0, exclusive_right = 0;
        for (int camera = 0; camera < 2; ++camera) {
            const uint8_t* source = camera ? right : left;
            cv::Mat image(size, CV_32FC3), mask(size, CV_8UC1);
            for (uint32_t y = 0; y < height; ++y) {
                auto* pixels = image.ptr<cv::Vec3f>(y);
                auto* valid = mask.ptr<uint8_t>(y);
                for (uint32_t x = 0; x < width; ++x) {
                    const size_t i = (size_t(y) * width + x) * 4;
                    pixels[x] = cv::Vec3f(source[i + 2], source[i + 1], source[i]);
                    valid[x] = source[i + 3] ? 255 : 0;
                    if (!camera) {
                        const bool a = left[i + 3] != 0, b = right[i + 3] != 0;
                        overlap += a && b; exclusive_left += a && !b; exclusive_right += b && !a;
                    }
                }
            }
            image.copyTo(images[camera]); mask.copyTo(masks[camera]);
        }
        if (overlap < 16 || exclusive_left < height || exclusive_right < height)
            return fail("insufficient overlap or exclusive camera coverage for graph cut");
        cv::detail::GraphCutSeamFinder finder("COST_COLOR");
        finder.find(images, std::vector<cv::Point>{cv::Point(0, 0), cv::Point(0, 0)}, masks);
        const cv::Mat a = masks[0].getMat(cv::ACCESS_READ), b = masks[1].getMat(cv::ACCESS_READ);
        for (uint32_t y = 0; y < height; ++y) {
            const auto* ma = a.ptr<uint8_t>(y); const auto* mb = b.ptr<uint8_t>(y);
            for (uint32_t x = 0; x < width; ++x) {
                const size_t p = size_t(y) * width + x, i = p * 4;
                const bool va = left[i + 3] != 0, vb = right[i + 3] != 0;
                if (va && vb && !ma[x] && !mb[x]) return fail("graph cut left a hole in overlap");
                const bool use_right = vb && (!va || (mb[x] && !ma[x]));
                ownership[p] = use_right ? 255 : 0;
            }
        }
        return 0;
    } catch (const cv::Exception& e) { return fail(e.what()); }
      catch (const std::exception& e) { return fail(e.what()); }
      catch (...) { return fail("unknown OpenCV seam failure"); }
}
