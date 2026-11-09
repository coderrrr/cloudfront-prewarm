"""CloudFormation custom resource: turn on "Send notifications to Amazon EventBridge" for buckets.

Existing notification settings (SNS, SQS, Lambda) are kept. On stack delete the
setting is left on, because other consumers may depend on it.
"""

import json
import logging
import os
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def _respond(event, context, status, reason="", data=None):
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch log stream {context.log_stream_name}",
        "PhysicalResourceId": event.get("PhysicalResourceId") or "s3-eventbridge-notification",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode()
    request = urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                     headers={"Content-Type": "", "Content-Length": str(len(body))})
    with urllib.request.urlopen(request, timeout=30):
        pass


def _enable(bucket, region):
    location = s3.get_bucket_location(Bucket=bucket)["LocationConstraint"] or "us-east-1"
    if location == "EU":
        location = "eu-west-1"
    if location != region:
        raise ValueError(f"bucket {bucket} is in {location}; deploy the forwarder stack in {location}")
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    config.pop("ResponseMetadata", None)
    if "EventBridgeConfiguration" in config:
        return False
    config["EventBridgeConfiguration"] = {}
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration=config,
                                             SkipDestinationValidation=True)
    return True


def handler(event, context):
    logger.info(json.dumps({k: v for k, v in event.items() if k != "ResponseURL"}))
    try:
        data = {}
        if event["RequestType"] in ("Create", "Update"):
            region = os.environ["AWS_REGION"]
            buckets = [b.strip() for b in event["ResourceProperties"]["BucketNames"] if b.strip()]
            changed = [b for b in buckets if _enable(b, region)]
            logger.info("EventBridge notifications turned on for %s; already on for the rest", changed or "none")
            data = {"ChangedBuckets": ",".join(changed)}
        _respond(event, context, "SUCCESS", data=data)
    except Exception as exc:
        logger.exception("custom resource failed")
        _respond(event, context, "FAILED", reason=str(exc)[:900])
