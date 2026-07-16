"""Tests for the Drone Setup app's Pi-provisioning logic — the pure,
SSH-free parts. The value here is the non-destructive token update: a token
rotation must change only the token line and leave every other .env value
exactly as the Pi had it."""
import pi_setup


def test_replace_token_line_changes_only_the_token_line():
    env = (
        "# generated\n"
        "CADDIS_API_TOKEN=oldtoken\n"
        "CADDIS_API_URL=https://api.caddistech.com\n"
        "SERIAL_PORT=/dev/ttyACM0\n"
    )
    out = pi_setup._replace_token_line(env, "NEWTOKEN")

    assert "CADDIS_API_TOKEN=NEWTOKEN" in out
    assert "oldtoken" not in out
    # everything else survives, unchanged and in order
    assert out.splitlines()[0] == "# generated"
    assert "CADDIS_API_URL=https://api.caddistech.com" in out
    assert "SERIAL_PORT=/dev/ttyACM0" in out


def test_replace_token_line_preserves_pi_side_customization():
    # A value a tech hand-added on the Pi must survive a token rotation — this
    # is the whole point of editing in place instead of regenerating .env.
    env = "CADDIS_API_TOKEN=old\nQUEUE_FALLBACK_DIR=/mnt/bigdisk/queue\n"
    out = pi_setup._replace_token_line(env, "new")

    assert "CADDIS_API_TOKEN=new" in out
    assert "QUEUE_FALLBACK_DIR=/mnt/bigdisk/queue" in out


def test_replace_token_line_appends_when_absent():
    out = pi_setup._replace_token_line("CADDIS_API_URL=x\n", "TOK")

    assert "CADDIS_API_TOKEN=TOK" in out
    assert "CADDIS_API_URL=x" in out


def test_replace_token_line_normalizes_to_unix_endings():
    # .env.example is read on Windows (CRLF) but must land on the Pi as LF.
    out = pi_setup._replace_token_line("CADDIS_API_TOKEN=old\r\nA=b\r\n", "t")

    assert "\r" not in out
    assert out.endswith("\n")


def test_build_env_file_fills_token_from_template(tmp_path, monkeypatch):
    template = tmp_path / ".env.example"
    template.write_text(
        "CADDIS_API_TOKEN=your_40_char_token_here\n"
        "CADDIS_API_URL=https://api.caddistech.com\n"
    )
    monkeypatch.setattr(pi_setup, "ENV_EXAMPLE", template)

    path = pi_setup._build_env_file("REALTOKEN")
    try:
        text = path.read_text()
    finally:
        path.unlink()

    assert "CADDIS_API_TOKEN=REALTOKEN" in text
    assert "your_40_char_token_here" not in text
    assert "CADDIS_API_URL=https://api.caddistech.com" in text


def test_service_unit_paths_all_point_inside_install_dir():
    # The "make sure the .service runs" guarantee: every path the unit
    # references must live under the one directory we install to, and the
    # service user must be the one we deployed as.
    unit = pi_setup._service_unit("blueos")

    assert f"ExecStart={pi_setup.VENV_DIR}/bin/python3 {pi_setup.BRIDGE_ENTRY}" in unit
    assert f"EnvironmentFile={pi_setup.ENV_PATH}" in unit
    assert f"WorkingDirectory={pi_setup.INSTALL_DIR}" in unit
    assert "User=blueos" in unit
    assert pi_setup.BRIDGE_ENTRY.startswith(pi_setup.INSTALL_DIR + "/")
    assert pi_setup.ENV_PATH.startswith(pi_setup.INSTALL_DIR + "/")
    assert pi_setup.VENV_DIR.startswith(pi_setup.INSTALL_DIR + "/")
