from __future__ import annotations

from sqlalchemy import select

from .db import SessionLocal
from .models import TestCase, TestCaseRisk


PACKAGE = "/home/linaro/Desktop/DEV-structlight/qa_factoryV2.0.4/RMSL321_factoryV2.0.4_QA_long_ota_package"
RUNNER = f"{PACKAGE}/develop_hub_case_runner"


CASES = [
    {
        "code": "QA-MODULE-DETECT",
        "name": "检测 RMSL321 模组",
        "description": "确认 3588 通过 USB 枚举到 UVC 运行态模组（VID:PID 2207:001a）。不打开视频流。",
        "command": "lsusb -d 2207:001a | grep -q .",
        "timeout_seconds": 30,
        "risk_level": TestCaseRisk.READ_ONLY,
    },
    {
        "code": "QA-L01-DEVICE-CHECK",
        "name": "UVC 节点枚举",
        "description": "检查当前设备模式所需的 RGB、DEPTH 或 IR V4L2 节点；运行必要的设备与算法基线准备。",
        "command": f"sudo -n {RUNNER} device-check",
        "timeout_seconds": 360,
        "risk_level": TestCaseRisk.READ_ONLY,
    },
    {
        "code": "QA-PROJECTOR-ON",
        "name": "投射器开启",
        "description": "通过控制通道设置 speckle projector=on，并执行一次 ping 确认控制通道仍在线；执行后保持开启。",
        "command": f"sudo -n {RUNNER} projector-on",
        "timeout_seconds": 60,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-PROJECTOR-OFF",
        "name": "投射器关闭",
        "description": "通过控制通道设置 speckle projector=off，并执行一次 ping 确认控制通道仍在线；执行后保持关闭。",
        "command": f"sudo -n {RUNNER} projector-off",
        "timeout_seconds": 60,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-L03-USB-PROFILE",
        "name": "USB Profile 切换",
        "description": "执行 1 轮 high→medium→low→high，逐次重启、回读 usb_profile，并在最后验证视频流恢复；最终保持 high。",
        "command": f"sudo -n {RUNNER} usb-profile 1",
        "timeout_seconds": 1800,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-L05-FOV-SWITCH",
        "name": "FOV 遍历与回读",
        "description": "遍历模组 FOV 列表：断流、设置 FOV、恢复取流、回读校验，并在结束时恢复原始 FOV。",
        "command": f"sudo -n {RUNNER} fov-switch 1",
        "timeout_seconds": 1800,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-L06-CTL-CHANNEL",
        "name": "控制通道完整检查",
        "description": "检查版本、SN、ping、参数与状态，执行 32KB 文件回环和日志读取；单 case 模式不执行 restart-app。",
        "command": f"sudo -n {RUNNER} ctl-channel",
        "timeout_seconds": 600,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-L07-STREAM-30S",
        "name": "RGB+DEPTH/IR 取流 30 秒",
        "description": "检查 RGB、DEPTH 的 fps 与 drops，并在 IR 节点存在时验证 RGB/DEPTH/IR 均能输出帧。",
        "command": f"sudo -n {RUNNER} stream 30",
        "timeout_seconds": 300,
        "risk_level": TestCaseRisk.READ_ONLY,
    },
    {
        "code": "QA-L08-ALGO-SWITCH",
        "name": "算法分辨率切换",
        "description": "执行 1 轮 640x352↔512x448 双向切换，每次等待模组重启并验证取流；最终恢复 640x352。",
        "command": f"sudo -n {RUNNER} algo-switch 1",
        "timeout_seconds": 1200,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-DEVICE-MODE-SWITCH",
        "name": "设备模式切换",
        "description": "执行 1 轮 rgb+depth↔rgb+ir 双向切换并验证对应视频节点；最终恢复 rgb+depth。",
        "command": f"sudo -n {RUNNER} device-mode 1",
        "timeout_seconds": 1200,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-L10-ISP-3A-10",
        "name": "ISP 3A Stress（10轮）",
        "description": "使用 uvc_host_isp_test 执行 10 轮 RGB+IR outstream 与 3A get/set，要求 summary=SUCCESS。",
        "command": f"sudo -n {RUNNER} isp-3a 10",
        "timeout_seconds": 900,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-L11-FACTORY-QUICK",
        "name": "FT 厂测 Quick",
        "description": "执行 uvc_host_ft --quick --json，要求退出码为 0 且 overall=PASS。",
        "command": f"sudo -n {RUNNER} factory-quick",
        "timeout_seconds": 900,
        "risk_level": TestCaseRisk.STATE_CHANGE,
    },
    {
        "code": "QA-L13-STABILITY-300S",
        "name": "稳定性测试（300秒）",
        "description": "并行运行 RGB+DEPTH、IMU 与周期性 ctl ping 300 秒，检查进程存活、USB 在线和 IMU 完成标记。",
        "command": f"sudo -n {RUNNER} stability 300",
        "timeout_seconds": 900,
        "risk_level": TestCaseRisk.READ_ONLY,
    },
    {
        "code": "QA-L12-OTA-AB",
        "name": "DFU OTA A/B 完整刷写",
        "description": "真实擦写 NAND：使用包内 bin/update_img 的 oem/system/boot，循环刷写 A/B 槽并尽量结束在生产槽 A；受每槽最多 3 次磨损保护。",
        "command": f"sudo -n {RUNNER} ota-ab",
        "timeout_seconds": 2400,
        "risk_level": TestCaseRisk.FIRMWARE_WRITE,
    },
]


def main() -> None:
    created = 0
    updated = 0
    with SessionLocal() as db:
        existing = {
            item.code: item
            for item in db.scalars(select(TestCase).where(TestCase.plugin_release_id.is_(None)))
        }
        for definition in CASES:
            test_case = existing.get(definition["code"])
            if test_case is None:
                test_case = TestCase(code=definition["code"])
                db.add(test_case)
                created += 1
            else:
                updated += 1
            test_case.name = definition["name"]
            test_case.description = definition["description"]
            test_case.command = definition["command"]
            test_case.expected_exit_code = 0
            test_case.timeout_seconds = definition["timeout_seconds"]
            test_case.risk_level = definition["risk_level"]
            test_case.enabled = True
        db.commit()
    print(f"RMSL321 QA catalog synchronized: {created} created, {updated} updated")


if __name__ == "__main__":
    main()
