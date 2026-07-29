"""Security contract for the local-only Streamlit server."""

from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_streamlit_is_bound_to_loopback_with_web_protections() -> None:
    config_path = PROJECT_ROOT / ".streamlit" / "config.toml"

    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)

    server = config["server"]
    assert server["address"] == "127.0.0.1"
    assert server["enableCORS"] is True
    assert server["enableXsrfProtection"] is True
