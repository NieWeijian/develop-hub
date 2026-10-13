"""Evidence boundaries for operator-gated cases; these tests never use hardware."""
import importlib.util
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "special_runtime", Path(__file__).with_name("special_runtime.py")
)
runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime)


def aging_telemetry(elapsed=60, overall=1, cpu_x10=550):
    return (
        'aging_telemetry timestamp="2026-10-08 18:01:00" '
        f'elapsed_sec={elapsed} fps_ir_l=10 fps_ir_r=10 fps_rgb=30 '
        'total_fps=50 sensor_ok=1 imu_ok=1 rk803_ok=1 temp_ok=1 '
        f'temp_cpu_x10={cpu_x10} temp_imu_x10=400 temp_sensor_x10=450 '
        f'i2c=0x1f overall_ok={overall}'
    )


def aging_log(*, elapsed=60, overall=1, cpu_x10=550, final=1, reason="duration_elapsed"):
    return "\n".join([
        "struct_light_aging start",
        "aging: wrote /data/aging_result = -1",
        "aging: duration = 60 sec (0 h)",
        "aging: warm-up window ignored fps_ir_l=10 fps_ir_r=10 fps_rgb=30 total_fps=50",
        aging_telemetry(elapsed, overall, cpu_x10),
        f"aging: final result={final} reason={reason}",
    ])


def analyze_aging(text, *, wall=61):
    return runtime.analyze_aging(
        text, min_seconds=60, max_temperature_c=85, elapsed_wall_seconds=wall
    )


def assert_rejected(result):
    assert result["passed"] is False, result
    assert result["reason"], "A rejected gate must explain the missing or failing evidence"


@pytest.mark.parametrize("sn", ["R3211101263200064", "A" * 56, "BOARD_01-TEST", "abc!@#%+./:"])
def test_validate_sn_accepts_firmware_supported_values(sn):
    runtime.validate_sn(sn)


@pytest.mark.parametrize("sn", ["", "A" * 57, "WITH SPACE", " tab\t", "line\n", "line\r", 'a"b', "a\\b", "模组001", "\x7f"])
def test_validate_sn_rejects_unsafe_or_unrepresentable_values(sn):
    with pytest.raises(ValueError):
        runtime.validate_sn(sn)


@pytest.mark.parametrize("text", ["lasted.log 100\n", "lasted.log\t-10\n", "lasted.log\tnot-a-size\n", "error: cannot open dir\n", "lasted.log\t12\textra\n"])
def test_listing_rejects_malformed_or_error_output(text):
    with pytest.raises(ValueError):
        runtime.parse_listing(text)


def test_aging_accepts_complete_current_run_evidence():
    result = analyze_aging(aging_log())
    assert result["passed"] is True, result


def test_aging_rejects_final_marker_without_current_run():
    assert_rejected(analyze_aging("aging: final result=1 reason=duration_elapsed"))


def test_aging_rejects_telemetry_without_run_start():
    text = aging_telemetry() + "\naging: final result=1 reason=duration_elapsed"
    assert_rejected(analyze_aging(text))


def test_aging_rejects_reused_persistent_pass_flag():
    text = "struct_light_aging start\naging: skip existing result=1\n" + aging_log()
    assert_rejected(analyze_aging(text))


def test_aging_rejects_duration_shorter_than_requested():
    assert_rejected(analyze_aging(aging_log(elapsed=59)))


def test_aging_rejects_firmware_failure_even_with_later_pass():
    text = aging_log().replace(aging_telemetry(), aging_telemetry(30, overall=0) + "\n" + aging_telemetry())
    assert_rejected(analyze_aging(text))


def test_aging_rejects_temperature_exceeding_operator_limit():
    assert_rejected(analyze_aging(aging_log(cpu_x10=851)))


def test_aging_rejects_final_failure():
    assert_rejected(analyze_aging(aging_log(final=0, reason="sensor_fps")))


def test_aging_rejects_pass_without_duration_elapsed_reason():
    assert_rejected(analyze_aging(aging_log(reason="operator_override")))


def test_aging_rejects_incompatible_observed_wall_time():
    assert_rejected(analyze_aging(aging_log(elapsed=60), wall=10))


OTA_START = "2026-10-08T18:00:00+08:00 ota-upgrade-oem-start"
OTA_CUT_AT = "2026-10-08T18:00:10+08:00"
MD5_REJECTION = "\n".join([
    "2026-10-08T17:59:00+08:00 ota-check-firmware-started",
    "2026-10-08T17:59:10+08:00 ota-transfer-finish",
    "2026-10-08T17:59:11+08:00 ota-md5check-fail",
])


def test_ota_corrupt_accepts_checksum_rejection_before_any_flash():
    result = runtime.analyze_ota("corrupt", MD5_REJECTION)
    assert result["passed"] is True, result


@pytest.mark.parametrize("unsafe_marker", ["ota-upgrade-oem-start", "ota-upgrade-oem-success", "ota-done"])
def test_ota_corrupt_rejects_logs_showing_flash_or_success(unsafe_marker):
    text = MD5_REJECTION + "\n2026-10-08T18:00:00+08:00 " + unsafe_marker
    assert_rejected(runtime.analyze_ota("corrupt", text))


def test_ota_corrupt_rejects_generic_failure_without_checksum_evidence():
    assert_rejected(runtime.analyze_ota("corrupt", "2026-10-08T18:00:00+08:00 ota-fail"))


def test_ota_wrong_name_does_not_misclassify_checksum_failure():
    assert_rejected(runtime.analyze_ota("wrong_name", MD5_REJECTION))


def test_ota_wrong_name_accepts_explicit_name_rejection():
    result = runtime.analyze_ota("wrong_name", "2026-10-08T18:00:00+08:00 ota-namecheck-fail")
    assert result["passed"] is True, result


def test_ota_wrong_name_rejects_operator_claim_without_device_marker():
    assert_rejected(runtime.analyze_ota("wrong_name", "Operator confirmed wrong package name was rejected"))


def test_ota_interrupted_accepts_timestamped_flash_window():
    result = runtime.analyze_ota("interrupted", OTA_START, progress_before_cut=OTA_START, cut_at=OTA_CUT_AT)
    assert result["passed"] is True, result


@pytest.mark.parametrize("progress", ["", "upgrade is in progress", "ota-upgrade-oem-start"])
def test_ota_interrupted_requires_timestamped_device_progress(progress):
    assert_rejected(runtime.analyze_ota("interrupted", OTA_START, progress_before_cut=progress, cut_at=OTA_CUT_AT))


@pytest.mark.parametrize("cut_at", ["", "now", "2026-10-08T17:59:59+08:00", "2026-10-08T18:02:01+08:00"])
def test_ota_interrupted_rejects_invalid_or_out_of_window_cut_time(cut_at):
    assert_rejected(runtime.analyze_ota("interrupted", OTA_START, progress_before_cut=OTA_START, cut_at=cut_at))


def test_ota_interrupted_rejects_cut_after_upgrade_already_completed():
    text = OTA_START + "\n2026-10-08T18:00:05+08:00 ota-done"
    assert_rejected(runtime.analyze_ota("interrupted", text, progress_before_cut=text, cut_at=OTA_CUT_AT))


def test_aging_accepts_actual_sdk_colored_serial_format():
    # SDK src/utils/log.h writes the reset sequence AFTER the newline.
    text = "".join("\x1b[0;32m[INFO] monitor_thread(501): " + line + "\r\n\x1b[0m" for line in aging_log().splitlines())
    result = analyze_aging(text)
    assert result["passed"] is True, result


def test_aging_rejects_restart_during_a_run():
    text = aging_log().replace(aging_telemetry(), "struct_light_aging start\n" + aging_telemetry())
    assert_rejected(analyze_aging(text))


@pytest.mark.parametrize("ended", ["fail", "success"])
def test_ota_interrupted_rejects_cut_after_active_partition_ended(ended):
    text = OTA_START + "\n2026-10-08T18:00:05+08:00 ota-upgrade-oem-" + ended
    assert_rejected(runtime.analyze_ota("interrupted", text, progress_before_cut=text, cut_at=OTA_CUT_AT))


def test_ota_interrupted_matches_progress_to_the_same_raw_event():
    old_run = "2026-10-07T18:00:00+08:00 ota-upgrade-oem-start"
    assert_rejected(runtime.analyze_ota("interrupted", old_run, progress_before_cut=OTA_START, cut_at=OTA_CUT_AT))


def test_serial_disconnect_preserves_log_even_when_termios_restore_fails(tmp_path, monkeypatch):
    import errno
    import fcntl
    import stat
    import termios
    from types import SimpleNamespace

    original_exists, original_stat, original_resolve = Path.exists, Path.stat, Path.resolve
    monkeypatch.setattr(Path, "exists", lambda path: True if str(path) == "/dev/ttyUSB99" else original_exists(path))
    monkeypatch.setattr(Path, "stat", lambda path, *args, **kwargs: SimpleNamespace(st_mode=stat.S_IFCHR) if str(path) == "/dev/ttyUSB99" else original_stat(path, *args, **kwargs))
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: path if str(path) == "/dev/ttyUSB99" else original_resolve(path, *args, **kwargs))
    monkeypatch.setattr(runtime.os, "open", lambda *_: 123)
    closed = []
    monkeypatch.setattr(runtime.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(fcntl, "flock", lambda *_: None)
    monkeypatch.setattr(termios, "tcgetattr", lambda _: [0, 0, 0, 0, 0, 0, [0] * 32])
    sets = []
    def set_attributes(*args):
        sets.append(args)
        if len(sets) > 1:
            raise OSError(errno.EIO, "Serial adapter disappeared")
    monkeypatch.setattr(termios, "tcsetattr", set_attributes)
    monkeypatch.setattr(termios, "tcflush", lambda *_: None)
    monkeypatch.setattr(runtime.select, "select", lambda *_: ([123], [], []))
    chunks = iter([b"struct_light_aging start\n", b""])
    monkeypatch.setattr(runtime.os, "read", lambda *_: next(chunks))
    instance = runtime.Runtime({"parameters": {"capture_mode": "serial", "serial_device": "/dev/ttyUSB99", "capture_timeout_seconds": 60}}, tmp_path / "result.json")
    with pytest.raises(runtime.Blocked, match="disconnected"):
        instance.aging_capture()
    assert closed == [123]
    assert (tmp_path / "aging-serial.log").read_bytes() == b"struct_light_aging start\n"
    assert any(item["path"] == "aging-serial.log" for item in instance.result["files"])
    assert instance.result["data"]["capture_ended_at"]


def test_ota_interrupted_accepts_3588_prefix_with_module_local_clock_preserved():
    # The firmware status file omits timezone and its own wall clock may be unset.
    line = "2026-10-08T10:00:00Z 1970-01-01T00:00:15 ota-upgrade-oem-start"
    result = runtime.analyze_ota("interrupted", line, progress_before_cut=line, cut_at=OTA_CUT_AT)
    assert result["passed"] is True, result


def test_ota_interrupted_rejects_progress_from_an_earlier_partition():
    text = "\n".join([OTA_START, "2026-10-08T18:00:05+08:00 ota-upgrade-oem-success", "2026-10-08T18:00:06+08:00 ota-upgrade-system-start"])
    assert_rejected(runtime.analyze_ota("interrupted", text, progress_before_cut=OTA_START, cut_at=OTA_CUT_AT))


def test_ota_interrupted_accepts_current_partition_after_prior_success():
    latest = "2026-10-08T18:00:06+08:00 ota-upgrade-system-start"
    text = "\n".join([OTA_START, "2026-10-08T18:00:05+08:00 ota-upgrade-oem-success", latest])
    result = runtime.analyze_ota("interrupted", text, progress_before_cut=latest, cut_at=OTA_CUT_AT)
    assert result["passed"] is True, result


@pytest.mark.parametrize("variant", ["corrupt", "wrong_name", "interrupted"])
def test_ota_prepare_creates_isolated_inputs_with_variant_correct_trigger(tmp_path, monkeypatch, variant):
    import hashlib

    original = bytes(range(256)) * 4
    source = tmp_path / "firmware-valid.tar"
    source.write_bytes(original)
    instance = runtime.Runtime({
        "device": {"serial": "1234abcd", "usb_port": "1-2"},
        "parameters": {"package_path": str(source), "expected_sha256": hashlib.sha256(original).hexdigest(), "variant": variant},
    }, tmp_path / "step" / "result.json")
    instance.video = "/dev/video88"
    monkeypatch.setattr(instance, "bind", lambda *_: {})
    instance.ota_prepare()
    data = instance.result["data"]
    package, trigger = Path(data["package_path"]), Path(data["trigger_path"])
    assert source.read_bytes() == original
    assert package != source
    expected_md5, trigger_name = trigger.read_text().split()
    assert expected_md5 == hashlib.md5(original).hexdigest()
    assert trigger_name == package.name
    if variant == "corrupt":
        assert sum(a != b for a, b in zip(package.read_bytes(), original)) == 1
        assert hashlib.md5(package.read_bytes()).hexdigest() != expected_md5
    else:
        assert package.read_bytes() == original
        assert hashlib.md5(package.read_bytes()).hexdigest() == expected_md5
    if variant == "wrong_name":
        assert package.name != source.name
    assert len(instance.result["files"]) == 2


def test_incomplete_aging_evidence_does_not_release_active_fixture(tmp_path):
    text = "struct_light_aging start\n" + aging_telemetry()
    instance = runtime.Runtime({
        "parameters": {"capture_mode": "manual", "manual_log": text,
                       "started_at": "2026-10-08T18:00:00+08:00", "ended_at": "2026-10-08T18:01:01+08:00",
                       "duration_seconds": 60, "max_temperature_c": 85},
        "context": {"hardware_active": True, "aging_capture": {"mode": "manual", "hardware_active": True}},
    }, tmp_path / "result.json")
    with pytest.raises(AssertionError):
        instance.aging_verify()
    assert instance.result["data"].get("hardware_active", True) is True


@pytest.mark.parametrize("fault", ["replug", "video_reused", "disappeared"])
def test_connection_guard_prevents_tool_execution_after_target_changes(tmp_path, monkeypatch, fault):
    instance = runtime.Runtime({"device": {"usb_port": "1-2"}}, tmp_path / "result.json")
    instance.video = "/dev/video88"
    instance.usb_generation = ("1", "7")
    original_read, original_resolve = Path.read_text, Path.resolve
    usb_port = Path("/sys/bus/usb/devices/1-2")
    video_device = Path("/sys/class/video4linux/video88/device")
    physical = Path("/virtual/usb1/1-2")
    def read(path, *args, **kwargs):
        if path.parent == usb_port:
            if fault == "disappeared":
                raise FileNotFoundError(str(path))
            return "1" if path.name == "busnum" else ("8" if fault == "replug" else "7")
        return original_read(path, *args, **kwargs)
    def resolve(path, *args, **kwargs):
        if path == usb_port:
            return physical
        if path == video_device:
            return Path("/virtual/usb1/1-3/1-3:1.0") if fault == "video_reused" else physical / "1-2:1.0"
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: pytest.fail("Tool must not run after target connection changes"))
    with pytest.raises(runtime.Blocked):
        instance.call("set-sn", "NEW-SN")
    assert instance.result["commands"] == []


def test_cancellation_marker_stops_pressure_before_the_first_write(tmp_path, monkeypatch):
    instance = runtime.Runtime({"run_id": "12345678-1234-1234-1234-123456789abc", "parameters": {"total_mib": 21}}, tmp_path / "result.json")
    monkeypatch.setattr(instance, "bind", lambda *_: {})
    monkeypatch.setattr(instance, "log_snapshot", lambda: {"/userdata/log": {}, "/userdata/log/archived": {}})
    monkeypatch.setattr(instance, "call", lambda *a, **k: pytest.fail("Cancellation must stop file-put before it is issued"))
    (tmp_path / "CANCEL_REQUESTED").write_text("operator cancellation")
    with pytest.raises(runtime.Blocked, match="cancellation"):
        instance.log_pressure()
    assert instance.result["data"]["successful_bytes"] == 0
    assert instance.result["data"]["files_written"] == []


def uploaded_aging_instance(tmp_path, path):
    return runtime.Runtime({"parameters": {
        "capture_mode": "manual", "manual_log_path": str(path), "manual_log_sha256": runtime.digest(path),
        "started_at": "2026-10-08T18:00:00+08:00", "ended_at": "2026-10-08T18:01:01+08:00",
        "duration_seconds": 60, "max_temperature_c": 85,
    }}, tmp_path / "step" / "result.json")


def test_uploaded_aging_log_accepts_frozen_evidence_in_current_step(tmp_path):
    step = tmp_path / "step"
    step.mkdir()
    path = step / "manual-upload.log"
    path.write_text(aging_log())
    instance = uploaded_aging_instance(tmp_path, path)
    instance.aging_verify()
    assert instance.result["data"]["passed"] is True
    assert instance.result["data"]["hardware_active"] is False


@pytest.mark.parametrize("fault", ["outside_step", "symlink", "modified"])
def test_uploaded_aging_log_rejects_other_steps_links_and_tampering(tmp_path, fault):
    step = tmp_path / "step"
    step.mkdir()
    source = (tmp_path if fault == "outside_step" else step) / "manual-upload.log"
    source.write_text(aging_log())
    path = source
    if fault == "symlink":
        path = step / "linked-log"
        path.symlink_to(source)
    instance = uploaded_aging_instance(tmp_path, path)
    if fault == "modified":
        source.write_text(aging_log() + "\nchanged after upload")
    with pytest.raises(runtime.Blocked, match="outside this action|checksum changed"):
        instance.aging_verify()
    assert instance.result["files"] == []


# Log pressure verdicts must distinguish a failed device operation from missing
# execution evidence. All commands and discovery are stubbed, with no hardware.
def log_runtime(tmp_path, monkeypatch, operation, previous=None):
    tool = tmp_path / 'frozen-tool'
    tool.write_bytes(b'not executed')
    instance = runtime.Runtime({
        'operation': operation,
        'run_id': '12345678-1234-1234-1234-123456789abc',
        'tool': str(tool), 'tool_sha256': runtime.digest(tool),
        'parameters': {'total_mib': 21},
        'context': {'log_pressure': previous} if previous is not None else {},
    }, tmp_path / 'result.json')
    monkeypatch.setattr(instance, 'bind', lambda *_: {})
    monkeypatch.setattr(instance, 'log_snapshot', lambda: {'/userdata/log': {}, '/userdata/log/archived': {}})
    monkeypatch.setattr(instance, 'health', lambda: instance.result['data'].update(health='healthy'))
    return instance


def upload_error(stderr='file-put chunk failed: write failed', exit_code=1):
    return {'name': 'qa-test-0001.log', 'response': {'exit_code': exit_code, 'stdout': '', 'stderr': stderr}}


def test_log_pressure_rejected_write_fails_at_execution_gate(tmp_path, monkeypatch):
    instance = log_runtime(tmp_path, monkeypatch, 'log_pressure')
    calls = []
    def upload(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return {'exit_code': 0, 'stdout': 'wrote 60000 bytes to /userdata/log/test.log', 'stderr': ''}
        return upload_error()['response']
    monkeypatch.setattr(instance, 'call', upload)
    result = instance.run_operation()
    assert result['outcome'] == 'failed'
    assert 'write failed' in result['message']
    assert result['data']['successful_bytes'] == 60000
    assert len(result['data']['files_written']) == 1
    assert len(result['data']['errors']) == 1
    assert result['data']['health'] == 'healthy'
    assert len(result['data']['snapshots']) == 2
    assert result['data']['actual_full_observed'] is False
    assert result['data']['write_failure_observed'] is True
    assert any(not assertion['passed'] for assertion in result['assertions'])


@pytest.mark.parametrize('failure_stage', ['snapshot', 'health', 'both'])
def test_log_pressure_diagnostic_problem_cannot_hide_known_write_failure(tmp_path, monkeypatch, failure_stage):
    instance = log_runtime(tmp_path, monkeypatch, 'log_pressure')
    count = 0
    def snapshot():
        nonlocal count
        count += 1
        if count > 1 and failure_stage in ('snapshot', 'both'):
            raise runtime.Blocked('Device disappeared after write rejection')
        return {'/userdata/log': {}, '/userdata/log/archived': {}}
    def health():
        if failure_stage in ('health', 'both'):
            raise runtime.Blocked('No module is available for health inspection')
        instance.result['data']['health'] = 'healthy'
    monkeypatch.setattr(instance, 'log_snapshot', snapshot)
    monkeypatch.setattr(instance, 'health', health)
    monkeypatch.setattr(instance, 'call', lambda *a, **k: upload_error()['response'])
    result = instance.run_operation()
    assert result['outcome'] == 'failed'
    assert 'write failed' in result['message']
    assert result['data']['diagnostic_errors']
    assert len(result['data']['errors']) == 1


@pytest.mark.parametrize('stderr', ['file-put chunk failed: write failed', 'No space left on device', 'ENOSPC', 'errno=28'])
def test_log_verify_old_context_device_rejection_fails_even_without_current_device(tmp_path, monkeypatch, stderr):
    previous = {'successful_bytes': 130 * 60000, 'errors': [upload_error(stderr)], 'files_written': ['qa-first.log']}
    instance = log_runtime(tmp_path, monkeypatch, 'log_verify', previous)
    def unavailable(*args):
        raise runtime.Blocked('Bound module unavailable')
    monkeypatch.setattr(instance, 'bind', unavailable)
    monkeypatch.setattr(instance, 'health', unavailable)
    result = instance.run_operation()
    assert result['outcome'] == 'failed'
    assert result['data']['successful_bytes'] == 7800000
    assert result['data']['errors'] == previous['errors']
    assert len(result['data']['diagnostic_errors']) == 2


@pytest.mark.parametrize('stderr,exit_code', [('Command timed out', 124), ('USB device disconnected', 1), ('', 0)])
def test_log_pressure_unavailable_acknowledgement_is_blocked(tmp_path, monkeypatch, stderr, exit_code):
    instance = log_runtime(tmp_path, monkeypatch, 'log_pressure')
    monkeypatch.setattr(instance, 'call', lambda *a, **k: upload_error(stderr, exit_code)['response'])
    result = instance.run_operation()
    assert result['outcome'] == 'blocked'
    assert 'acknowledgement unavailable' in result['message']
    assert not result['data'].get('write_failure_observed')


def test_log_verify_old_context_transport_error_remains_blocked(tmp_path, monkeypatch):
    previous = {'successful_bytes': 7800000, 'errors': [upload_error('Command timed out', 124)]}
    instance = log_runtime(tmp_path, monkeypatch, 'log_verify', previous)
    result = instance.run_operation()
    assert result['outcome'] == 'blocked'
    assert 'acknowledgement unavailable' in result['message']


@pytest.mark.parametrize('listing', [
    {'qa-first.log': {'size': 60000, 'directory': False}},
    {'lasted.log': {'size': 5 * 1024 * 1024, 'directory': False}},
])
def test_log_verify_observed_no_reclaim_or_no_convergence_fails(tmp_path, monkeypatch, listing):
    previous = {'successful_bytes': 24 * 1024 * 1024, 'errors': [], 'files_written': ['qa-first.log']}
    instance = log_runtime(tmp_path, monkeypatch, 'log_verify', previous)
    monkeypatch.setattr(instance, 'log_snapshot', lambda: {'/userdata/log': listing, '/userdata/log/archived': {}})
    result = instance.run_operation()
    assert result['outcome'] == 'failed'
    assert 'Rolling reclaimed' in result['message']


def test_log_verify_missing_directory_evidence_is_blocked(tmp_path, monkeypatch):
    previous = {'successful_bytes': 24 * 1024 * 1024, 'errors': [], 'files_written': ['qa-first.log']}
    instance = log_runtime(tmp_path, monkeypatch, 'log_verify', previous)
    monkeypatch.setattr(instance, 'log_snapshot', lambda: {'/userdata/log': {}, '/userdata/log/archived': None})
    result = instance.run_operation()
    assert result['outcome'] == 'blocked'
    assert 'directories' in result['message']


def test_log_verify_completed_pressure_and_observed_reclaim_pass(tmp_path, monkeypatch):
    previous = {'successful_bytes': 24 * 1024 * 1024, 'errors': [], 'files_written': ['qa-first.log']}
    instance = log_runtime(tmp_path, monkeypatch, 'log_verify', previous)
    result = instance.run_operation()
    assert result['outcome'] == 'passed'
    assert result['data']['reclaimed_files'] == ['qa-first.log']
    assert result['data']['managed_log_bytes'] == 0


def test_log_verify_without_pressure_evidence_is_blocked(tmp_path, monkeypatch):
    instance = log_runtime(tmp_path, monkeypatch, 'log_verify')
    result = instance.run_operation()
    assert result['outcome'] == 'blocked'
    assert 'evidence missing' in result['message']


def test_log_pressure_unhealthy_recovery_preserves_primary_write_failure(tmp_path, monkeypatch):
    instance = log_runtime(tmp_path, monkeypatch, 'log_pressure')
    monkeypatch.setattr(instance, 'call', lambda *a, **k: upload_error()['response'])
    def unhealthy():
        instance.check('Module health', 'healthy', 'app not running', False)
    monkeypatch.setattr(instance, 'health', unhealthy)
    result = instance.run_operation()
    assert result['outcome'] == 'failed'
    assert 'write failed' in result['message']
    assert result['data']['failure_reason'] == 'log_write_failed'
    assert any(item['name'] == 'Module health' and not item['passed'] for item in result['assertions'])
    assert any(item['operation'] == 'health' for item in result['data']['diagnostic_errors'])
