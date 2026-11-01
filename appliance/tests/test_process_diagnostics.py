from reco_field.supervisor import Supervisor


def test_capture_error_survives_buffered_argus_cleanup(tmp_path):
    path = tmp_path / "camera.log"
    fault = 'Error: frame read: right: capture pipeline: INVALID_SETTINGS (Some("Argus Correctable Error Status"))'
    path.write_text("$ reco camera\n\x1b[31m" + fault + "\x1b[0m\n" + "GST_ARGUS: Done Success\n" * 100)
    assert Supervisor._log_tail(path) == fault


def test_tracing_error_is_selected_before_normal_shutdown(tmp_path):
    path = tmp_path / "camera.log"
    fault = "2026-11-01T06:42:16Z ERROR reco_io: [right] capture pipeline failed"
    path.write_text("$ reco camera\n" + fault + "\nGST_ARGUS: Cleaning up\n")
    assert Supervisor._log_tail(path) == fault


def test_previous_capture_error_is_not_reused(tmp_path):
    path = tmp_path / "camera.log"
    path.write_text("$ reco old\nError: stale capture failure\n$ reco new\nGST_ARGUS: Done Success\n")
    assert Supervisor._log_tail(path) == "GST_ARGUS: Done Success"


def test_error_is_found_across_log_rotation_without_using_previous_capture(tmp_path):
    path = tmp_path / "camera.log"
    previous = tmp_path / "camera.log.1"
    previous.write_text("$ reco old\nError: stale capture failure\n$ reco new\nError: current capture failure\n")
    path.write_text("GST_ARGUS: Done Success\n" * 100)
    assert Supervisor._log_tail(path) == "Error: current capture failure"
    previous.write_text("$ reco old\nError: stale capture failure\n$ reco new\n")
    assert "stale" not in Supervisor._log_tail(path)


def test_missing_current_log_does_not_reuse_rotated_capture(tmp_path):
    path = tmp_path / "camera.log"
    (tmp_path / "camera.log.1").write_text("$ reco old\nError: stale capture failure\n")
    assert Supervisor._log_tail(path) == "no process log"


def test_log_scan_and_result_are_bounded_and_tolerate_invalid_utf8(tmp_path):
    path = tmp_path / "camera.log"
    path.write_bytes(b"$ reco camera\nError: old error outside scan\n" + b"progress\n" * 10000 + b"\xff latest output\n")
    result = Supervisor._log_tail(path)
    assert "old error" not in result
    assert len(result) == 400
    assert result.endswith("\ufffd latest output")
    path.write_text("$ reco camera\nError: " + "reason " * 100)
    assert Supervisor._log_tail(path).startswith("Error:")
    assert len(Supervisor._log_tail(path)) == 400
