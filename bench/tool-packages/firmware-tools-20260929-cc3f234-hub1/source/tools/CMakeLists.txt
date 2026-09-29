cmake_minimum_required(VERSION 3.16)
project(uvc_tools LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
pkg_check_modules(DRM REQUIRED libdrm)
find_package(Threads REQUIRED)

add_library(uvc_tools_v4l2 STATIC common/v4l2_capture.cpp)
target_include_directories(uvc_tools_v4l2 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/common)
target_compile_options(uvc_tools_v4l2 PRIVATE -Wall -Wextra)

add_library(uvc_tools_drm STATIC common/drm_preview.cpp)
target_include_directories(uvc_tools_drm PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/common)
target_include_directories(uvc_tools_drm PRIVATE ${DRM_INCLUDE_DIRS})
target_link_libraries(uvc_tools_drm PUBLIC ${DRM_LIBRARIES})
target_compile_options(uvc_tools_drm PRIVATE -Wall -Wextra)

add_executable(uvc_host_stream uvc_host_stream.cpp)
target_link_libraries(uvc_host_stream PRIVATE uvc_tools_v4l2)
target_compile_options(uvc_host_stream PRIVATE -Wall -Wextra)

add_executable(uvc_host_imu uvc_host_imu.cpp)
target_compile_options(uvc_host_imu PRIVATE -Wall -Wextra)

add_executable(uvc_host_ctl uvc_host_ctl.cpp)
target_link_libraries(uvc_host_ctl PRIVATE uvc_tools_v4l2)
target_compile_options(uvc_host_ctl PRIVATE -Wall -Wextra)

add_executable(uvc_host_preview uvc_host_preview.cpp)
target_link_libraries(uvc_host_preview PRIVATE uvc_tools_v4l2 uvc_tools_drm)
target_compile_options(uvc_host_preview PRIVATE -Wall -Wextra)

add_executable(uvc_host_isp_test uvc_host_isp_test.cpp)
target_link_libraries(uvc_host_isp_test PRIVATE uvc_tools_v4l2)
target_compile_options(uvc_host_isp_test PRIVATE -Wall -Wextra)

add_executable(uvc_host_isp_cli uvc_host_isp_cli.cpp)
target_link_libraries(uvc_host_isp_cli PRIVATE uvc_tools_v4l2 uvc_tools_drm)
target_compile_options(uvc_host_isp_cli PRIVATE -Wall -Wextra)

add_executable(uvc_host_ft uvc_host_ft.cpp)
target_link_libraries(uvc_host_ft PRIVATE uvc_tools_v4l2)
target_compile_options(uvc_host_ft PRIVATE -Wall -Wextra)

add_executable(uvc_host_ota uvc_host_ota.cpp)
target_link_libraries(uvc_host_ota PRIVATE uvc_tools_v4l2)
target_compile_options(uvc_host_ota PRIVATE -Wall -Wextra)
add_executable(uvc_host_multisync uvc_host_multisync.cpp)
target_link_libraries(uvc_host_multisync PRIVATE uvc_tools_v4l2 Threads::Threads)
target_compile_options(uvc_host_multisync PRIVATE -Wall -Wextra)

install(TARGETS uvc_host_ctl uvc_host_stream uvc_host_imu uvc_host_preview uvc_host_isp_test uvc_host_isp_cli uvc_host_ft uvc_host_ota uvc_host_multisync
        RUNTIME DESTINATION bin)
