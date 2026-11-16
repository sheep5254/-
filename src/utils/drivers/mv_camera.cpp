#ifdef USE_MvSDK
    #include "mv_camera.hpp"
namespace awakening {
void MvCamera::stop() {
    if (!running_) {
        return;
    }
    running_ = false;
    stop_source_thread();
    if (daemon_thread_.joinable()) {
        daemon_thread_.join();
    }

    CameraUnInit(h_camera_);

    AWAKENING_INFO("mv_camera has stop ");
}

bool MvCamera::start_capture() {
    CameraPlay(h_camera_);
    running_ = true;
    return true;
}

ImageFrame MvCamera::read() {
    ImageFrame frame;
    tSdkFrameHead head;
    BYTE* raw = nullptr;
    auto status = CameraGetImageBuffer(h_camera_, &head, &raw, 100);
    if (status != CAMERA_STATUS_SUCCESS) {
        AWAKENING_ERROR("Failed to get image buffer!");
        return frame;
    }

    const auto current_time = Clock::now();
    const auto half_exposure = std::chrono::microseconds(static_cast<long>(get_ExposureTime() / 2));
    auto img = cv::Mat(cv::Size(head.iWidth, head.iHeight), CV_8UC1, raw);
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

    apply_flip(frame.src_img); // 倒装补偿（mv_camera.flip）

    frame.timestamp = current_time - half_exposure;
    frame.format = PixelFormat::BGR;
    return frame;
}

void MvCamera::restart() {
    AWAKENING_WARN("Restarting camera");
    CameraUnInit(h_camera_);
    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    load(config_);

    CameraPlay(h_camera_);

    AWAKENING_INFO("Camera restarted successfully!");
    return;
}
void MvCamera::init() {
    load(config_);
}
void MvCamera::load(const YAML::Node& config) {
    if (!initialize_camera()) {
        AWAKENING_ERROR("Failed to initialize camera");
        return;
    }
    CameraGetCapability(h_camera_, &t_capability_);
    // 曝光模式：跟海康的 ExposureAuto 一样，可配成"相机自己调"或"固定值"。
    //   ae_enable: true  -> 开硬件自动曝光（Continuous），亮度自动收敛到 ae_target；
    //                       曝光范围由 ae_exposure_min/max 夹住，避免它为了变亮把曝光
    //                       拉到 100ms 以上把帧率拖到个位数。
    //   ae_enable: false -> 关自动曝光，用 exposure_time 固定（默认行为）。
    // 注意：这台相机上电默认是**自动**的（实测 AeState=1、曝光漂到 7340us），所以
    // 两条分支都必须显式设置并回读确认，不能靠默认值。
    const bool ae_enable = config["ae_enable"] ? config["ae_enable"].as<bool>() : false;
    if (ae_enable) {
        const int ae_target = config["ae_target"] ? config["ae_target"].as<int>() : 45;
        CameraSetAeTarget(h_camera_, ae_target);
        if (config["ae_exposure_min"] && config["ae_exposure_max"]) {
            const double emin = config["ae_exposure_min"].as<double>();
            const double emax = config["ae_exposure_max"].as<double>();
            CameraSetAeExposureRange(h_camera_, emin, emax);
            AWAKENING_INFO("MvCamera: 自动曝光范围 {} ~ {} us", emin, emax);
        }
        CameraSetAeState(h_camera_, true);
        BOOL ae_state = -1;
        CameraGetAeState(h_camera_, &ae_state);
        int target_now = 0;
        CameraGetAeTarget(h_camera_, &target_now);
        auto exposure_time = config["exposure_time"].as<double>();
        set_ExposureTime(exposure_time); // 给个起点，之后由 AE 自己收敛
        AWAKENING_INFO(
            "MvCamera: 自动曝光已开启 (AeState={}, 目标亮度={}) —— 硬件 AE，等价于海康 "
            "ExposureAuto=Continuous",
            static_cast<int>(ae_state),
            target_now
        );
    } else {
        // 关闭自动曝光（AE）。相机上电默认是自动的，曝光会在几帧内自己漂到 7340us
        // 左右，完全盖掉下面设置的固定曝光。这里把返回值回读一次：万一没关成功，
        // 程序会一直带着自动曝光跑，图像亮度随环境变化，所有基于亮度的装甲板阈值
        // 都会失效 —— 必须在启动日志里能看见。
        const auto ae_status = CameraSetAeState(h_camera_, false);
        BOOL ae_state = -1;
        CameraGetAeState(h_camera_, &ae_state);
        if (ae_status != CAMERA_STATUS_SUCCESS || ae_state != 0) {
            AWAKENING_ERROR(
                "MvCamera: 关闭自动曝光失败! status={} AeState={} (1=仍是自动)，"
                "图像亮度会自动变化，识别阈值将不可靠",
                static_cast<int>(ae_status),
                static_cast<int>(ae_state)
            );
        } else {
            AWAKENING_INFO(
                "MvCamera: 自动曝光已关闭 (AeState={}, 手动曝光)", static_cast<int>(ae_state)
            );
        }
    }
    // 倒装补偿：相机是倒着装的，Sensor 出来的图就是倒的（相机自身不支持硬件镜像，
    // 实测 CameraGetHardwareMirror 返回 -4 不支持）。这里用软件翻转，配置 mv_camera.flip。
    const std::string flip_str =
        config["flip"] ? utils::to_upper(config["flip"].as<std::string>()) : std::string("NONE");
    if (flip_str == "VERTICAL" || flip_str == "V") {
        flip_code_ = 0;
    } else if (flip_str == "HORIZONTAL" || flip_str == "H") {
        flip_code_ = 1;
    } else if (flip_str == "180" || flip_str == "ROTATE_180" || flip_str == "BOTH") {
        flip_code_ = -1;
    } else {
        flip_code_ = -2;
    }
    AWAKENING_INFO("MvCamera: flip = {} (code {})", flip_str, flip_code_);
    CameraSetIspOutFormat(h_camera_, CAMERA_MEDIA_TYPE_BGR8);
    CameraSetTriggerMode(h_camera_, 0);
    int frame_speed = config["frame_speed"].as<int>();
    CameraSetFrameSpeed(h_camera_, frame_speed);
    if (!ae_enable) {
        auto exposure_time = config["exposure_time"].as<double>();
        set_ExposureTime(exposure_time);
    }
    if (config["r_gain"] && config["b_gain"] && config["g_gain"]) {
        auto r_gain = config["r_gain"].as<double>();
        auto b_gain = config["b_gain"].as<double>();
        auto g_gain = config["g_gain"].as<double>();
        set_rgb_gain(r_gain, b_gain, g_gain);
    }
    if (config["analog_gain"]) {
        auto analog_gain = config["analog_gain"].as<double>();
        set_analog_gain(analog_gain);
    }
    if (config["gamma"]) {
        auto gamma = config["gamma"].as<double>();
        set_gamma(gamma);
    }
    #ifdef USE_TRT
    use_cuda_cvt_ = config["use_cuda_cvt"].as<bool>();
    #endif
}
void MvCamera::set_ExposureTime(double exposure_time) {
    double exposure_line_time;
    CameraGetExposureLineTime(h_camera_, &exposure_line_time);
    exposure_time = std::clamp(
        exposure_time,
        t_capability_.sExposeDesc.uiExposeTimeMin * exposure_line_time,
        t_capability_.sExposeDesc.uiExposeTimeMax * exposure_line_time
    );
    CameraSetExposureTime(h_camera_, exposure_time);
    AWAKENING_INFO(
        "MvCamera: set exposure_time {}, min {} max {}",
        exposure_time,
        t_capability_.sExposeDesc.uiExposeTimeMin * exposure_line_time,
        t_capability_.sExposeDesc.uiExposeTimeMax * exposure_line_time
    );
}
double MvCamera::get_ExposureTime() const {
    double exposure_time;
    CameraGetExposureTime(h_camera_, &exposure_time);
    return exposure_time;
}
void MvCamera::set_rgb_gain(int r_gain, int b_gain, int g_gain) {
    r_gain = std::clamp(
        r_gain,
        t_capability_.sRgbGainRange.iRGainMin,
        t_capability_.sRgbGainRange.iRGainMax
    );
    b_gain = std::clamp(
        b_gain,
        t_capability_.sRgbGainRange.iBGainMin,
        t_capability_.sRgbGainRange.iBGainMax
    );
    g_gain = std::clamp(
        g_gain,
        t_capability_.sRgbGainRange.iGGainMin,
        t_capability_.sRgbGainRange.iGGainMax
    );
    CameraSetGain(h_camera_, r_gain, g_gain, b_gain);
    AWAKENING_INFO(
        "MvCamera: set rgb_gain = {}, {}, {}, r: min {} max {} , g: min {} max {} , b: min {} max {}",
        r_gain,
        g_gain,
        b_gain,
        t_capability_.sRgbGainRange.iRGainMin,
        t_capability_.sRgbGainRange.iRGainMax,
        t_capability_.sRgbGainRange.iGGainMin,
        t_capability_.sRgbGainRange.iGGainMax,
        t_capability_.sRgbGainRange.iBGainMin,
        t_capability_.sRgbGainRange.iBGainMax
    );
}
void MvCamera::set_analog_gain(double analog_gain) {
    analog_gain = std::clamp(
        analog_gain,
        (double)t_capability_.sExposeDesc.uiAnalogGainMin,
        (double)t_capability_.sExposeDesc.uiAnalogGainMax
    );
    CameraSetAnalogGain(h_camera_, analog_gain);
    AWAKENING_INFO(
        "MvCamera: set analog_gain {}, min {} max {}",
        analog_gain,
        t_capability_.sExposeDesc.uiAnalogGainMin,
        t_capability_.sExposeDesc.uiAnalogGainMax
    );
}
void MvCamera::set_gamma(double gamma) {
    gamma = std::clamp(
        gamma,
        (double)t_capability_.sGammaRange.iMin,
        (double)t_capability_.sGammaRange.iMax
    );
    CameraSetGamma(h_camera_, gamma);
    AWAKENING_INFO(
        "MvCamera: set gamma {}, min {} max {}",
        gamma,
        t_capability_.sGammaRange.iMin,
        t_capability_.sGammaRange.iMax
    );
}
bool MvCamera::initialize_camera() {
    // if (h_camera_ != nullptr) {
    //     CameraUnInit(*h_camera_);
    //     h_camera_ = nullptr;
    // }
    while (running_) {
        int i_camera_counts = 1;
        int i_status = -1;
        tSdkCameraDevInfo t_camera_enum_list;
        i_status = CameraEnumerateDevice(&t_camera_enum_list, &i_camera_counts);
        AWAKENING_INFO("Enumerate state = {}", i_status);
        AWAKENING_INFO("Found camera count = {}", i_camera_counts);

        // 没有连接设备
        if (i_camera_counts == 0) {
            AWAKENING_ERROR("No camera found!");
            std::this_thread::sleep_for(std::chrono::seconds(1));
            continue;
        }
        i_status = CameraInit(&t_camera_enum_list, -1, -1, &h_camera_);

        // 初始化失败
        AWAKENING_INFO("MvCamera: Init state = {}", i_status);
        if (i_status != CAMERA_STATUS_SUCCESS) {
            AWAKENING_ERROR("MvCamera: Init failed!");
            std::this_thread::sleep_for(std::chrono::seconds(1));
            continue;
        }
        AWAKENING_INFO("Camera initialized successfully");
        return true;
    }

    return false;
}
} // namespace awakening
#endif
