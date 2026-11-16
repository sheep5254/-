
#pragma once
#include "CameraApi.h"
#include "utils/common/image.hpp"
#include "utils/drivers/camera.hpp"
#include "utils/logger.hpp"
#include "utils/scheduler/scheduler.hpp"
#include <algorithm>
#include <opencv2/core/types.hpp>
#include <opencv2/opencv.hpp>
#include <utility>
#ifdef USE_TRT
    #include "utils/cuda/cvtcolor.hpp"
#endif
namespace awakening {
class MvCamera: public Camera {
public:
    MvCamera(const YAML::Node& config, Scheduler& scheduler):
        Camera(&scheduler),
        scheduler_ref_(scheduler) {
        config_ = config;
        running_ = true;
        CameraSdkInit(0);
    }
    ~MvCamera() {
        stop();
    }
    void stop() override;
    void restart() override;
    bool is_running() const override {
        return running_;
    }
    ImageFrame read() override;
    bool start_capture() override;
    template<typename Tag>
    void start(std::string source_name) {
        using IO = IOPair<Tag, ImageFrame>;
        source_snapshot_id_ = scheduler_ref_.register_source<IO>(source_name);
        CameraPlay(h_camera_);
        running_ = true;
        daemon_thread_ = std::thread(&MvCamera::run_loop<IO>, this);
    }
    template<typename IO>
    void run_loop() {
        while (running_) {
            mv_capture_loop<IO>();
            if (!running_) {
                break;
            }
            restart();
        }
    }
    template<typename IO>
    void mv_capture_loop() {
        AWAKENING_INFO("Starting image capture loop!");
        tSdkFrameHead head;
        BYTE* raw;
        int fail_count = 0;
        while (running_) {
            // std::this_thread::sleep_for(std::chrono::milliseconds(1));

            auto status = CameraGetImageBuffer(h_camera_, &head, &raw, 100);
            if (status == CAMERA_STATUS_SUCCESS) {
                const auto current_time = Clock::now();

                const auto half_exposure =
                    std::chrono::microseconds(static_cast<long>(get_ExposureTime() / 2));
                auto img = cv::Mat(cv::Size(head.iWidth, head.iHeight), CV_8UC1, raw);
                ImageFrame frame;
#ifdef USE_TRT
                if (use_cuda_cvt_) {
                    static utils::__cuda::CvtColor cvt;
                    cvt.process(img, frame.src_img, cv::COLOR_BayerRG2BGR);
                } else {
#endif
                    cv::cvtColor(img, frame.src_img, cv::COLOR_BayerRG2BGR);
#ifdef USE_TRT
                }
#endif
                CameraReleaseImageBuffer(h_camera_, raw);

                apply_flip(frame.src_img); // 倒装补偿
                frame.timestamp = current_time - half_exposure;

                frame.format = PixelFormat::BGR;
                scheduler_ref_.runtime_push_source<IO>(
                    source_snapshot_id_,
                    [f = std::move(frame)]() {
                        return std::make_tuple(std::optional<typename IO::second_type>(std::move(f))
                        );
                    }
                );
            } else {
                AWAKENING_ERROR("Failed to get image buffer!");
                fail_count++;
                if (fail_count > 10) {
                    AWAKENING_ERROR("Too many failures, exiting image capture loop.");
                    break;
                }
            }
        }
        AWAKENING_INFO("Exiting image capture loop.");
    }
    void init() override;
    void load(const YAML::Node& config);
    double get_ExposureTime() const override;
    void set_ExposureTime(double exposure_time) override;
    void set_rgb_gain(int r_gain, int b_gain, int g_gain);
    void set_analog_gain(double analog_gain);
    void set_gamma(double gamma);
    // 相机倒装补偿（和海康路径的 flip 语义一致，配置项 mv_camera.flip）：
    //   none      -> -2 不翻转
    //   vertical  ->  0 上下翻转
    //   horizontal->  1 左右翻转
    //   "180"     -> -1 上下+左右（= 旋转 180°，倒装相机用这个）
    // 相机本身不支持硬件镜像（实测 CameraGetHardwareMirror 返回 -4），只能软件翻。
    void apply_flip(cv::Mat& img) const {
        if (flip_code_ >= -1) {
            cv::Mat out;
            cv::flip(img, out, flip_code_);
            img = std::move(out);
        }
    }
    bool initialize_camera();
    std::atomic<bool> running_ { false };
    YAML::Node config_;
    CameraHandle h_camera_;
    tSdkCameraCapbility t_capability_; // 设备描述信息
    std::thread daemon_thread_;
    Scheduler& scheduler_ref_;
    bool use_cuda_cvt_ = false;
    int flip_code_ = -2; // -2=none 0=vertical 1=horizontal -1=180°
};
} // namespace awakening
