# CloudFront Edge Location Prewarming

When an object is created or overwritten under a configured S3 path, a Lambda function downloads it once from every CloudFront edge location in the configured countries or cities, so those locations cache the new version before users ask for it.

![Architecture diagram](architecture.png)

```
S3 bucket in each region ──(Object Created)──> default event bus in that region
                                      │ forwarding rule (filters by bucket name, optionally by prefix)
                                      ▼
                         cdn-prewarm-bus in central region ──> prewarm Lambda
                                                        ├─ 1. Invalidate the URL in CloudFront and wait for completion
                                                        └─ 2. GET <domain>/<path> from each edge location
```

To pin a request to one edge location, the Lambda resolves `<distribution ID prefix>.<location code>.cloudfront.net` (for example `d111111abcdef8.NRT57-P3.cloudfront.net`) to get that location's IP, then connects to it with the site domain as Host/SNI. The `x-amz-cf-pop` response header confirms which location answered.

## Files

| File | Description |
|---|---|
| `dist/cdn-prewarm-central.yaml` | Central stack: event bus, prewarm Lambda, log group. Deploy once. |
| `dist/cdn-prewarm-forwarder.yaml` | Forwarder stack. Deploy in every region that has a monitored bucket, including the central region. |
| `src/`, `templates/`, `build.py` | Lambda source and template sources. After changing them, run `python3 build.py` to regenerate `dist/`. |
| `refresh_edge_snapshot.py` | Updates the edge location snapshot built into the Lambda code. |
| `deployer-policy.json` | IAM policy for deploying, updating, and deleting both stacks. |
| `architecture.drawio`, `architecture.png` | Architecture diagram (both open in draw.io). |

## Deployment

The buckets, the CloudFront distributions, and the central stack must be in the same AWS account.

**1. Deploy the central stack** in one region of your choice:

```bash
aws cloudformation deploy --region us-east-1 --stack-name cdn-prewarm \
  --template-file dist/cdn-prewarm-central.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "S3Paths=bucket-a/static/;bucket-b/img/" \
    "CdnUrlPrefixes=https://www.example.com/static/;https://img.example.com/" \
    "EdgeCountriesOrCities=Tokyo;Germany;United States" \
    "FilePatterns=*"
```

Note the `EventBusArn` output.

**2. Deploy the forwarder stack in every region that has a bucket:**

```bash
aws cloudformation deploy --region us-west-2 --stack-name cdn-prewarm-forwarder \
  --template-file dist/cdn-prewarm-forwarder.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "CentralEventBusArn=arn:aws:events:us-east-1:111122223333:event-bus/cdn-prewarm-bus" \
    "BucketNames=bucket-a" \
    "KeyPrefixes=static/;img/"
```

You can also deploy from the CloudFormation console by uploading the template.

**Deployment permissions.** Attach `deployer-policy.json` to the deploying user or role. Its resources are scoped to names starting with `cdn-prewarm`, so stack names and the central stack's `EventBusName` must start with `cdn-prewarm` (or replace the prefix throughout the policy). Uploading a template in the console also needs `s3:CreateBucket`, `s3:PutObject`, and `s3:GetObject` on `arn:aws:s3:::cf-templates-*` and `arn:aws:s3:::cf-templates-*/*`. If an SCP requires a permissions boundary, add `PermissionsBoundary` to the three `AWS::IAM::Role` resources in the templates.

## Parameters

All list parameters are separated by `;`.

### Central stack

| Parameter | Description |
|---|---|
| S3Paths | S3 paths to monitor, as `bucket/prefix/`. `bucket` alone means the whole bucket. |
| CdnUrlPrefixes | One URL prefix per S3 path, matched by position. The domain is a distribution alternate domain name or `dxxxx.cloudfront.net`. |
| EdgeCountriesOrCities | Countries and/or cities to prewarm, for example `Tokyo;Germany;United States` (the default). |
| FilePatterns | Only prewarm file names matching these patterns, for example `*.mp4;*.m3u8`. Case-insensitive, supports `*` and `?`. Default `*`. |
| InvalidateBeforePrewarm | Default `true`: invalidate the URL before prewarming. Keep it on if files get overwritten. |
| EdgeLocationsUrl | Edge location list JSON. Default `https://static.cloudping.cloud/cdn/cloudfront-edge-locations.json`. |

Any file created or overwritten under a path, including subdirectories, is prewarmed. Files that existed before deployment are not. The prewarm URL is the URL prefix plus the rest of the key after the S3 path, URL-encoded:

| S3 path | URL prefix | Uploaded file | Prewarm URL |
|---|---|---|---|
| `bucket-a/static/` | `https://www.example.com/static/` | `s3://bucket-a/static/js/app.js` | `https://www.example.com/static/js/app.js` |
| `bucket-b/img/` | `https://img.example.com/` | `s3://bucket-b/img/logo.png` | `https://img.example.com/logo.png` |

The Lambda finds the distribution from the URL prefix's domain, exact alternate domain name first, then wildcard (`*.example.com` matches `cdn.example.com`).

Country and city names come from the edge location list (also in `EDGE_SNAPSHOT` in `src/prewarm.py`). A country covers all its locations; Hong Kong and Taipei are listed under `China`. Matching ignores case, accents, spaces, and punctuation, and the state code can be omitted (`Los Angeles` matches `Los Angeles, CA`). The `nodes_per_name` field in the `prewarm_start` log shows how many locations each name matched.

### Forwarder stack

| Parameter | Description |
|---|---|
| CentralEventBusArn | The `EventBusArn` output of the central stack. |
| BucketNames | Monitored buckets in this region, for example `bucket-a;bucket-b`, no spaces. |
| KeyPrefixes | Optional. Only forward objects under these prefixes. Recommended, to filter out unrelated writes such as access logs. |
| EnableBucketEventBridge | Default `true`: turns on EventBridge notifications for the buckets, keeping existing notification settings. Deleting the stack doesn't turn it off. |

## Edge location list

The Lambda fetches the list from `EdgeLocationsUrl` and caches it for 1 hour. If a fetch fails, it uses the last list fetched successfully, or else the snapshot built into the code. The `edge_list` field in the `prewarm_start` log shows which one was used. To update the snapshot, run the following, then update the central stack:

```bash
python3 refresh_edge_snapshot.py
python3 build.py
```

## Viewing results

Logs are in `/aws/lambda/<central stack name>-prewarm`: one `prewarm_result` entry per edge location and one `prewarm_summary` entry per file. Logs Insights query for locations that did not succeed:

```
fields @timestamp, url, node, outcome, error
| filter type = "prewarm_result" and ok = 0
| sort @timestamp desc
```

| outcome | Meaning |
|---|---|
| `ok` | 2xx response, and `x-amz-cf-pop` matches the target location |
| `failed` | Any error at this location: DNS, connection, timeout, non-2xx, or a response from a different location |
| `not_started` | Skipped because less than 60 seconds of Lambda time remained |
| `aborted` | Stopped midway because less than 15 seconds of Lambda time remained |

`etag_match` shows whether the location's ETag matches the one in the S3 event.

## Verifying with curl

Request the file from one edge location to check that it has the new version:

```bash
DOMAIN=www.example.com              # domain in CdnUrlPrefixes
FILE_PATH='/static/js/my_app.js'    # file path, URL-encoded below
NODE=NRT12-P5                       # location code from this file's prewarm_result logs

# Distribution prefix dxxxx. For wildcard alternate domain names, copy it from the console instead
DIST=$(aws cloudfront list-distributions --output text \
  --query "DistributionList.Items[?Aliases.Items && contains(Aliases.Items, '$DOMAIN')].DomainName | [0]" | cut -d. -f1)

# URL-encode each path segment, keeping the slashes (jq ships with macOS 15 and later)
URL_PATH=$(jq -rn --arg p "$FILE_PATH" '$p | split("/") | map(@uri) | join("/")')

curl -sI -m 15 --retry 3 --retry-all-errors -H 'Accept-Encoding: gzip, deflate, br' \
  --connect-to "$DOMAIN:443:$DIST.$NODE.cloudfront.net:443" \
  "https://$DOMAIN$URL_PATH" | grep -iE '^(HTTP/|x-cache:|x-amz-cf-pop:|etag:|age:)'
```

A successful prewarm looks like this:

```
HTTP/2 200
etag: "b1bf01b86ec379951e7dedcd78e199c9"
x-cache: Hit from cloudfront
x-amz-cf-pop: NRT12-P5
age: 55
```

`x-amz-cf-pop` should equal `NODE`, `x-cache` should be `Hit`, and `etag` should equal the S3 ETag (`aws s3api head-object --bucket <bucket> --key <key> --query ETag`).

- Use a location code from this prewarm's logs. Locations from other sources may be gone or unreachable.
- No output means your network can't reach that location. Remove `-s` to see the error.
- Keep the `Accept-Encoding` header. Without it you hit a different cached copy and see `Miss`.

## Fault tolerance

- An error at one location marks only that location `failed`. Failed locations are not retried.
- The invocation fails, and Lambda retries it up to 2 more times, when: no request succeeded, no distribution matched, no edge location matched, the configuration is invalid, or the invalidation fails or takes over 5 minutes.
- Events that fail all attempts are dropped. A missed prewarm only makes the first user request slower; upload the file again to retry. Query errors with `filter level = "ERROR"`.

## Caveats

- **Caching must be enabled** on the distribution. With `Managed-CachingDisabled` or TTL 0, prewarming does nothing.
- **Cost.** Each upload downloads about `file size × number of locations` from CloudFront (100 MB × 50 locations = 5 GB). Invalidations beyond 1,000 paths per month cost $0.005 each.
- **Don't monitor paths that logs are written to**, or each prewarm's logs trigger another prewarm.
- **Change configuration through the stack.** Lambda console edits are overwritten on the next stack update.
- Delete events don't trigger anything.
