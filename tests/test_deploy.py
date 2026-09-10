import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


spec = importlib.util.spec_from_file_location(
    "deploy", Path(__file__).resolve().parents[1] / "scripts/deploy.py"
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_first_deployment_requires_explicit_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["deploy.py"])
    session = Mock()
    monkeypatch.setattr(deploy.boto3, "Session", session)
    with pytest.raises(SystemExit) as error:
        deploy.main()
    assert error.value.code == 2
    session.assert_not_called()


def test_saved_profile_cannot_deploy_to_different_account(tmp_path, monkeypatch):
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local/deployment.json").write_text(
        json.dumps({"Profile": "test-profile", "Account": "111111111111"})
    )
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["deploy.py"])
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "222222222222"}
    factory = Mock(return_value=session)
    monkeypatch.setattr(deploy.boto3, "Session", factory)
    with pytest.raises(SystemExit) as error:
        deploy.main()
    assert error.value.code == 2
    factory.assert_called_once_with(profile_name="test-profile", region_name="eu-west-2")
    session.client.assert_called_once_with("sts")
