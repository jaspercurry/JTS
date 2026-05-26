from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "switch-voice-provider.sh"


def test_switch_voice_provider_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_switch_voice_provider_writes_wizard_owned_env():
    text = SCRIPT.read_text()

    assert 'PROVIDER_ENV="/var/lib/jasper/voice_provider.env"' in text
    assert 'env="/var/lib/jasper/voice_provider.env"' in text
    assert "sed -i 's|^JASPER_VOICE_PROVIDER=" not in text
    assert "JASPER_VOICE_PROVIDER=${PROVIDER}' /etc/jasper/jasper.env" not in text
