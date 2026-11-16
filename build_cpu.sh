#!/usr/bin/env bash
#
# Build awakening on a machine WITHOUT an NVIDIA GPU.
#
# Why not `./run.sh build`?
#   run.sh forces clang/clang++, which cannot compile this codebase here:
#   clang 14 does not accept `-std=c++23`. gcc >= 12 does, so the CPU build uses
#   the newest available gcc. TensorRT/CUDA is disabled and OpenVINO (CPU) is
#   used for inference.
#
# ROS2 (Humble) is enabled because the vision node publishes the three topics
# (cmd_gimbal_joint / cmd_shoot / tracker/target) for the navigation stack.
# Open3D - required only by the optional radar target - is not installed, so
# radar/sentry targets are disabled instead.
#
# 本脚本会自动适配本机工具链，不需要手动改：
#   - 编译器：优先 g++-13，其次 g++-12，再退到默认 g++（要求支持 C++23）
#   - 构建工具：有 ninja 用 ninja，没有就用 Unix Makefiles
#   - 相机 SDK：HikSDK 是必需的（海康）；MvSDK(迈德威视) 缺失时自动装一个空的
#     INTERFACE target，因为 CMakeLists 会无条件链接它，而相关代码本来就在
#     USE_MvSDK 宏后面，不会编译进去。
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$ROOT/build"

export MVCAM_SDK_PATH=/opt/MVS
export MVCAM_COMMON_RUNENV=/opt/MVS/lib
export MVCAM_GENICAM_CLPROTOCOL=/opt/MVS/lib/CLProtocol
export ALLUSERSPROFILE=/opt/MVS/MVFG

# ---- ROS2 环境：auto_aim_interfaces 是发布 tracker/target 必需的 ----
has_auto_aim_interfaces() {
    [ -n "${AMENT_PREFIX_PATH:-}" ] \
        && printf '%s\n' "$AMENT_PREFIX_PATH" | tr ':' '\n' | grep -q '/auto_aim_interfaces$'
}

find_auto_aim_ws() {
    local base hit
    for base in "$HOME" "$HOME/Lin" "$HOME/Misc" "$HOME/github" "$HOME/Downloads"; do
        [ -d "$base" ] || continue
        hit=$(find "$base" -maxdepth 5 -type d -name auto_aim_interfaces \
                  -path '*/install/auto_aim_interfaces' 2>/dev/null | head -1)
        [ -n "$hit" ] || continue
        (cd "$hit/../.." && pwd)
        return 0
    done
    return 1
}

if ! has_auto_aim_interfaces; then
    # ROS 的 setup.bash 会引用未绑定变量，和 set -u 冲突 —— source 时临时关掉
    set +u
    [ -f /opt/ros/humble/setup.bash ] && source /opt/ros/humble/setup.bash
    # 只需要能 find_package 到 auto_aim_interfaces 即可（自瞄与导航是独立进程）。
    if [ -n "${AWAKENING_ROS_WS:-}" ] && [ -f "$AWAKENING_ROS_WS/install/setup.bash" ]; then
        echo "source 指定工作空间: $AWAKENING_ROS_WS/install/setup.bash"
        # shellcheck disable=SC1090
        source "$AWAKENING_ROS_WS/install/setup.bash"
    elif ws="$(find_auto_aim_ws)"; then
        echo "自动 source ROS 工作空间: $ws/install/setup.bash"
        # shellcheck disable=SC1090
        source "$ws/install/setup.bash"
    else
        echo "错误：找不到 auto_aim_interfaces（发布 tracker/target 需要）。" >&2
        echo "      请先 source 含该接口包的工作空间再构建。" >&2
        exit 1
    fi
    set -u
fi

# ---- OpenVINO（CPU 推理）----
if [ -f /opt/intel/openvino_2024.6.0/setupvars.sh ]; then
    set +u
    # shellcheck disable=SC1091
    source /opt/intel/openvino_2024.6.0/setupvars.sh >/dev/null 2>&1 || true
    set -u
fi

# ---- 编译器：需要 C++23，优先 gcc 13/12 ----
if [ -z "${CC:-}" ] || [ -z "${CXX:-}" ]; then
    for v in 13 12; do
        if command -v "gcc-$v" >/dev/null 2>&1 && command -v "g++-$v" >/dev/null 2>&1; then
            CC="gcc-$v"; CXX="g++-$v"
            break
        fi
    done
fi
if [ -z "${CC:-}" ] || [ -z "${CXX:-}" ]; then
    CC="gcc"; CXX="g++"
    gcc_major="$("$CC" -dumpversion | cut -d. -f1)"
    if [ "${gcc_major:-0}" -lt 12 ]; then
        echo "错误：$CC 版本 $gcc_major 不支持 C++23，需要 gcc >= 12。" >&2
        exit 1
    fi
fi
echo "使用编译器: $CC / $CXX"

# ---- 构建工具：有 ninja 用 ninja，否则 Makefiles ----
if command -v ninja >/dev/null 2>&1; then
    GENERATOR="Ninja"; BUILD_CMD=(ninja -C "$BUILD")
else
    GENERATOR="Unix Makefiles"; BUILD_CMD=(make -C "$BUILD")
fi
echo "使用生成器: $GENERATOR"

# 生成器变了就必须清缓存，否则 cmake 会报 generator 不匹配
if [ -f "$BUILD/CMakeCache.txt" ]; then
    cached_gen="$(sed -n 's/^CMAKE_GENERATOR:INTERNAL=//p' "$BUILD/CMakeCache.txt" | head -1)"
    if [ -n "$cached_gen" ] && [ "$cached_gen" != "$GENERATOR" ]; then
        echo "生成器变更（$cached_gen -> $GENERATOR），清理旧缓存"
        rm -rf "$BUILD"
    fi
fi

# ---- 缺失的相机 SDK 用空 target 兜底 ----
# CMakeLists 无条件链接 HikSDK::HikSDK / MvSDK::MvSDK，但对应代码在
# USE_HikSDK / USE_MvSDK 宏后面；某个 SDK 没装时给个空 INTERFACE target
# 就能正常链接，不会编进任何该 SDK 的代码。
mkdir -p "$BUILD"
cat > "$BUILD/compat_sdk.cmake" <<'EOF'
# 由 build_cpu.sh 自动生成，勿手改。
function(_awakening_stub_sdk_if_missing target header)
    if(TARGET ${target})
        return()
    endif()
    find_path(hdr_${header}
        NAMES ${header}.h
        PATHS /usr/include /usr/local/include
              "$ENV{MVSDK_PATH}/include" "$ENV{MVCAM_SDK_PATH}/include"
        NO_DEFAULT_PATH)
    find_path(hdr_${header} NAMES ${header}.h)
    if(NOT hdr_${header})
        add_library(${target} INTERFACE IMPORTED GLOBAL)
        message(STATUS "compat_sdk: ${header}.h 未找到 -> 为空 target ${target} 兜底")
    endif()
endfunction()
_awakening_stub_sdk_if_missing(MvSDK::MvSDK CameraApi)
_awakening_stub_sdk_if_missing(HikSDK::HikSDK MvCameraControl)
EOF

# ---- 迈德威视(MindVision) SDK ----
# SDK 不在系统里，用 TJ2.x 里自带的那份（include/ + lib/amd64/libMVSDK.so），
# 由本脚本安装到 ~/.local/mvsdk（含 include/ 和 lib/ 两级，正好是 FindMvSDK 期望的布局）。
# 装了才会定义 USE_MvSDK，config 里 camera.type 才能写 mv。
if [ -z "${MVSDK_PATH:-}" ] && [ -f "$HOME/.local/mvsdk/include/CameraApi.h" ]; then
    export MVSDK_PATH="$HOME/.local/mvsdk"
fi
if [ -n "${MVSDK_PATH:-}" ]; then
    echo "MvSDK(MindVision): $MVSDK_PATH"
else
    echo "MvSDK(MindVision): 未找到 —— camera.type: mv 将不可用"
fi

cmake -S "$ROOT" -B "$BUILD" -G "$GENERATOR" \
    -DCMAKE_C_COMPILER="$CC" \
    -DCMAKE_CXX_COMPILER="$CXX" \
    -DCMAKE_PROJECT_INCLUDE="$BUILD/compat_sdk.cmake" \
    -DBUILD_WITH_TRT=OFF \
    -DBUILD_WITH_OPENVINO=ON \
    -DBUILD_WITH_RERUN=OFF \
    -DBUILD_RUNTIME_TESTS=OFF \
    -DBUILD_RADAR=OFF \
    -DBUILD_SENTRY=OFF

"${BUILD_CMD[@]}" standard -j"$(( $(nproc) - 1 ))"

echo "built: $ROOT/bin/standard"