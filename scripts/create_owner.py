"""Create the initial private reviewer account without sending an email."""

import json
import os
from pathlib import Path
import secrets

import boto3

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = json.loads((ROOT / ".local/deployment.json").read_text())
    credentials_path = ROOT / ".local/owner-access.json"
    if credentials_path.exists():
        print("Owner credentials already exist in .local/owner-access.json; no changes made.")
        return
    client = boto3.Session(profile_name=config["Profile"], region_name=config["Region"]).client(
        "cognito-idp"
    )
    password = secrets.token_urlsafe(24) + "Aa1!"
    # Save privately before creation so an interrupted run does not lose the credential.
    payload = {
        "url": config["AppUrl"],
        "username": "owner",
        "password": password,
        "tenant": "renobytes",
        "status": "creating",
    }
    fd = os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(payload, file, indent=2)
    client.admin_create_user(
        UserPoolId=config["UserPoolId"],
        Username="owner",
        TemporaryPassword=password,
        MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "custom:tenant", "Value": "renobytes"}],
    )
    client.admin_set_user_password(
        UserPoolId=config["UserPoolId"], Username="owner", Password=password, Permanent=True
    )
    client.admin_add_user_to_group(
        UserPoolId=config["UserPoolId"], Username="owner", GroupName="reviewer"
    )
    payload["status"] = "ready"
    credentials_path.write_text(json.dumps(payload, indent=2))
    print("Private owner account created. Credentials saved to .local/owner-access.json.")


if __name__ == "__main__":
    main()
