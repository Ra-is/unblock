"""Reproducible dev deployment. Uses only the explicitly selected AWS profile."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]


def deploy_stack(client, name, template, parameters=None):
    args = dict(
        StackName=name,
        TemplateBody=template.read_text(),
        Capabilities=["CAPABILITY_IAM"],
        Parameters=[
            {"ParameterKey": k, "ParameterValue": v} for k, v in (parameters or {}).items()
        ],
        Tags=[{"Key": "Project", "Value": "Unblock"}, {"Key": "Environment", "Value": "dev"}],
    )
    try:
        client.describe_stacks(StackName=name)
    except ClientError as error:
        if "does not exist" not in error.response["Error"]["Message"]:
            raise
        client.create_stack(**args)
    else:
        try:
            client.update_stack(**args)
        except ClientError as error:
            if "No updates" not in str(error):
                raise
            return {
                o["OutputKey"]: o["OutputValue"]
                for o in client.describe_stacks(StackName=name)["Stacks"][0].get("Outputs", [])
            }
    previous = None
    while True:
        stack = client.describe_stacks(StackName=name)["Stacks"][0]
        status = stack["StackStatus"]
        if status != previous:
            print(f"{name}: {status}", flush=True)
            previous = status
        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        if "IN_PROGRESS" not in status:
            events = client.describe_stack_events(StackName=name)["StackEvents"]
            failures = [
                f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}"
                for e in events
                if "FAILED" in e["ResourceStatus"]
            ]
            raise RuntimeError(f"{name}: {status}\n" + "\n".join(failures[:10]))
        time.sleep(8)


def package():
    build = ROOT / ".build"
    build.mkdir(exist_ok=True)
    target = build / "package"
    # Only remove this script's exact generated dependency directory.
    if target.exists():
        shutil.rmtree(target)
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(build / "requirements.txt"),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python-version",
            "3.12",
            "--python-platform",
            "x86_64-manylinux2014",
            "--only-binary",
            ":all:",
            "--target",
            str(target),
            "-r",
            str(build / "requirements.txt"),
        ],
        cwd=ROOT,
        check=True,
    )
    shutil.copytree(
        ROOT / "unblock", target / "unblock", ignore=shutil.ignore_patterns("__pycache__")
    )
    archive = build / "application.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(target.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zipped.write(path, path.relative_to(target))
    return archive


def activate_rule_set(session, name):
    """CloudFormation can create a receipt rule set but cannot make it the active one."""
    ses = session.client("ses")
    active = (ses.describe_active_receipt_rule_set().get("Metadata") or {}).get("Name")
    if active == name:
        return
    if active:
        print(f"Replacing active SES receipt rule set {active} with {name}", flush=True)
    ses.set_active_receipt_rule_set(RuleSetName=name)
    print(f"Activated SES receipt rule set {name}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", help="Local AWS profile; defaults to saved deployment profile")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--stack", default="unblock-dev")
    parser.add_argument("--inbound-domain", default=None)
    parser.add_argument("--mail-from", default=None)
    parser.add_argument("--allowed-recipients", default=None)
    parser.add_argument("--hosted-zone-id", default=None)
    parser.add_argument("--enable-sending", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    config_path = ROOT / ".local/deployment.json"
    saved = json.loads(config_path.read_text()) if config_path.exists() else {}
    args.profile = args.profile or saved.get("Profile")
    if not args.profile:
        parser.error("--profile is required for the first deployment")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if saved.get("Account") and saved["Account"] != account:
        parser.error("Selected AWS account differs from the saved deployment account")
    cf = session.client("cloudformation")
    try:
        prior = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in cf.describe_stacks(StackName=args.stack)["Stacks"][0].get("Parameters", [])
        }
    except ClientError as error:
        if "does not exist" not in str(error):
            raise
        prior = {}
    for attribute, parameter in (
        ("inbound_domain", "InboundDomain"),
        ("mail_from", "MailFrom"),
        ("allowed_recipients", "AllowedRecipients"),
        ("hosted_zone_id", "HostedZoneId"),
    ):
        if getattr(args, attribute) is None:
            setattr(args, attribute, prior.get(parameter, ""))
    if args.enable_sending is None:
        args.enable_sending = prior.get("SendEnabled", "false") == "true"
    if args.enable_sending and not (args.mail_from and args.allowed_recipients):
        parser.error("--enable-sending requires --mail-from and --allowed-recipients")
    # Created out of band by scripts/create_demo.py; absent on deployments without a demo.
    demo_path = ROOT / ".local/demo-access.json"
    demo = json.loads(demo_path.read_text()) if demo_path.exists() else {}
    print(f"Deploying {args.stack} in {args.region}, account {account}", flush=True)
    bootstrap = deploy_stack(cf, args.stack + "-artifacts", ROOT / "infra/bootstrap.yaml")
    archive = package()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    key = f"releases/{digest}.zip"
    session.client("s3").upload_file(str(archive), bootstrap["ArtifactBucket"], key)
    outputs = deploy_stack(
        cf,
        args.stack,
        ROOT / "infra/application.yaml",
        {
            "ArtifactBucket": bootstrap["ArtifactBucket"],
            "ArtifactKey": key,
            "InboundDomain": args.inbound_domain,
            "MailFrom": args.mail_from,
            "AllowedRecipients": args.allowed_recipients,
            "HostedZoneId": args.hosted_zone_id,
            "SendEnabled": "true" if args.enable_sending else "false",
            "DemoUsername": demo.get("username", ""),
            "DemoPassword": demo.get("password", ""),
        },
    )
    if outputs.get("ReceiptRuleSet"):
        activate_rule_set(session, outputs["ReceiptRuleSet"])
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    (local / "deployment.json").write_text(
        json.dumps(
            {**outputs, "Region": args.region, "Profile": args.profile,
             "Stack": args.stack, "Account": account},
            indent=2,
        )
    )
    os.chmod(local / "deployment.json", 0o600)
    print("Deployment complete: " + outputs["AppUrl"], flush=True)


if __name__ == "__main__":
    main()
