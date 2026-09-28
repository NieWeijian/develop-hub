/*
 * RMSL control protocol (shared definitions, host side).
 *
 * Keep in sync with src/ctl/ctl_proto.h (device side).
 *
 * Transport is the UVC extension unit (see ctl_service.h): SET_CUR carries
 * [cmd u32][payload...], GET_CUR returns the response in 60-byte
 * length-prefixed chunks.  Command ids are stable so host tools keep working
 * across control-transport changes.
 */
#ifndef CTL_PROTO_H
#define CTL_PROTO_H

#include <cstddef>
#include <cstdint>

namespace rmsl_ctl {

enum Command : uint32_t {
    kCmdGetVersion = 0x00000001u,
    kCmdGetSn = 0x00000002u,
    kCmdPing = 0x000000F0u,
    kCmdFileList = 0x00001000u,
    kCmdFileGet = 0x00001001u,
    kCmdFilePut = 0x00001002u,      /* single-shot put: [path\0 + data] */
    kCmdFilePutOpen = 0x00001003u,  /* begin chunked put: [path\0] */
    kCmdFilePutChunk = 0x00001004u, /* append data chunk (<= 56 bytes) */
    kCmdFilePutClose = 0x00001005u, /* fsync + rename the temp file */
    kCmdGetParam = 0x00001100u,     /* response: human-readable param conf text */
    kCmdSetAlgoMode = 0x00001101u,  /* request payload: "WxH"; async, poll status */
    kCmdGetAlgoMode = 0x00001102u,  /* response: "WxH status=N requested=N target=..." */
    kCmdGetDeviceMode = 0x00001103u, /* response: device mode string */
    kCmdSetDeviceMode = 0x00001104u, /* request payload: mode; writes param + reboot */
    kCmdRestartApp = 0x00001105u,   /* restart struct_light_app only (calib reload) */
    kCmdGetSyncRole = 0x00001106u,  /* response: "master" | "slave" | "unknown" */
    kCmdGetCpuSerial = 0x00001107u, /* response: SoC CPU serial (efuse id) */
    kCmdGetLog = 0x00001110u,       /* response: tail of current module log */
    kCmdCleanLog = 0x00001111u,     /* clears archived logs + truncates current */
    /* FT telemetry (app snapshots into the param store; served over XU) */
    kCmdGetFtStatus = 0x00001120u,
    kCmdGetFtFlash = 0x00001121u,
    kCmdGetFtDdr = 0x00001122u,
    kCmdGetFtMipi = 0x00001123u,
    kCmdGetFtAdc = 0x00001124u,
    kCmdGetFtAging = 0x00001125u,
    kCmdSetFtAging = 0x00001126u,   /* payload: -2/-1/0/1, writes /data/aging_result */
    /* Runtime troubleshooting status snapshots */
    kCmdGetAppStatus = 0x00001130u,    /* response: app process alive / pid */
    kCmdGetSensorStatus = 0x00001131u, /* response: v4l2 sc132gs sensor list */
    kCmdGetVicapStatus = 0x00001132u,  /* response: /proc/rkcif* dump */
    kCmdGetIspStatus = 0x00001133u,    /* response: /proc/rkisp* dump */
    kCmdGetImuStatus = 0x00001134u,    /* response: imu app + IIO icm42607 info */
};

/* Control commands. Value payloads are 4-byte LE ints, except kCtlCmdSetFov
 * (float) and kCtlCmdSetSn (string). */
enum ControlCmd : uint32_t {
    /* Deprecated/removed: calibration is expressed by usb_mode (rgb+ir).
     * Keep the number reserved so old host tools get an explicit error. */
    kCtlCmdCalibMode = 0xFF000001u,
    kCtlCmdRotateMode = 0xFF000002u,
    kCtlCmdProOnOff = 0xFF000003u,
    kCtlCmdProCurrent = 0xFF000004u,
    kCtlCmdAlgoAsyncMode = 0xFF000100u,
    kCtlCmdAlgoIterCnt = 0xFF000101u,
    kCtlCmdAlgoFps = 0xFF000102u,
    kCtlCmdAlgoModelType = 0xFF000103u,
    kCtlCmdAlgoDenoise = 0xFF000104u,
    kCtlCmdAlgoFullSize = 0xFF000105u,
    kCtlCmdSetFov = 0xFF000106u,        /* payload: 4-byte float */
    kCtlCmdSetFovCropSize = 0xFF000107u, /* payload: "WxH" string */
    kCtlCmdAlgoUncertain = 0xFF000108u,
    kCtlCmdAlgoDiscontinue = 0xFF000109u,
    kCtlCmdIrBtnr = 0xFF00010Au,
    kCtlCmdRgbBtnr = 0xFF00010Bu,
    kCtlCmdAlgoUncertainDelta = 0xFF00010Cu,
    kCtlCmdUsbProfile = 0xFF00010Du,
    kCtlCmdSetSn = 0xFF00010Eu,        /* payload: SN string */
    kCtlCmdEnterDfu = 0xFFFFFFFBu, /* payload: "part[,part...]" */
    kCtlCmdFactoryReset = 0xFFFFFFFCu,
    kCtlCmdReboot = 0xFFFFFFFDu,
    kCtlCmdAdbMode = 0xFFFFFFFEu,
    kCtlCmdRebootLoader = 0xFFFFFFFFu,
};

constexpr size_t kMaxPayload = 60000; /* in-memory response cap for XU chunking */

/* Large downloads are read from the 1024-byte XU data channel (selector 2);
 * every other response stays on the 60-byte control channel (selector 1). */
inline bool is_large_response(uint32_t cmd) {
    switch (cmd & 0xFFFFu) {
    case kCmdFileList:
    case kCmdFileGet:
    case kCmdGetParam:
    case kCmdGetLog:
    case kCmdGetFtStatus:
    case kCmdGetFtFlash:
    case kCmdGetFtDdr:
    case kCmdGetFtMipi:
    case kCmdGetFtAdc:
    case kCmdGetFtAging:
    case kCmdGetSensorStatus:
    case kCmdGetVicapStatus:
    case kCmdGetIspStatus:
    case kCmdGetImuStatus:
        return true;
    default:
        return false;
    }
}

/* kCtlCmdAdbMode requires this magic in the payload (4-byte LE). Internal use
 * only: old hosts/tools that send the bare ADB_MODE command are rejected, so
 * customers cannot flip the module into ADB with legacy commands. */
inline constexpr uint32_t kAdbModeMagic = 0xADB5EED1u;

} // namespace rmsl_ctl

#endif /* RMSL_CTL_PROTO_H */
