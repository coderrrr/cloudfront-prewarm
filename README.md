# CloudFront Edge Location Prewarming

When an object is created or overwritten under a configured S3 path, a Lambda function downloads that object once, in full, from every CloudFront edge location in the configured countries or cities, so those locations cache the latest version ahead of user requests.

![Architecture diagram](architecture.png)

The diagram source is `architecture.drawio`, which you can open and edit in draw.io. `architecture.png` also embeds the diagram source, so draw.io can open it directly too.

```
S3 bucket in each region ──(Object Created)──> default event bus in that region
                                      │ forwarding rule (filters by bucket name, optionally by prefix)
                                      ▼
                         cdn-prewarm-bus in central region ──> prewarm Lambda
                                                        ├─ 1. Invalidate the URL in CloudFront and wait for completion
                                                        └─ 2. GET <domain>/<path> from each edge location
```

How a request is pinned to a specific edge location: resolve `<distribution ID prefix>.<location code>.cloudfront.net` (for example `d111111abcdef8.NRT57-P3.cloudfront.net`) to get that location's IP, then connect to that IP using the site domain as Host/SNI. The `x-amz-cf-pop` response header confirms that the target location handled the request.

## Files

| File | Description |
|---|---|
| `dist/cdn-prewarm-central.yaml` | Central stack: event bus, prewarm Lambda, log group. Deploy once. |
| `dist/cdn-prewarm-forwarder.yaml` | Forwarder stack: deploy in every region that has a monitored bucket, including the central region itself. |
| `src/prewarm.py`, `src/enable_s3_eventbridge.py` | Lambda source code, inlined into the templates above. |
| `templates/`, `build.py` | Template sources. After changing the source code, run `python3 build.py` to regenerate `dist/`. |
| `refresh_edge_snapshot.py` | Updates the edge location snapshot built into the Lambda code. See "Edge location list". |
| `architecture.drawio`, `architecture.png` | Architecture diagram. |
| `deployer-policy.json` | IAM permissions needed to deploy, update, and delete both stacks. See "Permissions required for deployment". |

## Deployment

Prerequisite: the buckets, the CloudFront distributions, and the central stack are in the same AWS account. The Lambda function looks up distributions by domain name in this account.

**1. Deploy the central stack** (pick one region as the central region, for example us-east-1)

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

**2. Deploy the forwarder stack in every region that has a bucket**

```bash
aws cloudformation deploy --region us-west-2 --stack-name cdn-prewarm-forwarder \
  --template-file dist/cdn-prewarm-forwarder.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "CentralEventBusArn=arn:aws:events:us-east-1:111122223333:event-bus/cdn-prewarm-bus" \
    "BucketNames=bucket-a" \
    "KeyPrefixes=static/;img/"
```

You can also deploy from the console: CloudFormation → Create stack → Upload a template file, then fill in the parameters on the page.

### Permissions required for deployment

`deployer-policy.json` contains the permissions needed to deploy, update, and delete both stacks. You can attach it directly to the IAM user or role used for deployment. It has been tested by creating, updating, and deleting both stacks with a role that had only this policy attached.

For readability, CloudFormation, Lambda, EventBridge, and CloudWatch Logs actions are merged into `cloudformation:*`, `lambda:*`, `events:*`, and `logs:*`, but resources are still limited to stacks, functions, event buses, rules, and log groups whose names start with `cdn-prewarm`. The few read actions that can only be granted on `*` (such as `cloudformation:ValidateTemplate` and `logs:DescribeLogGroups`) are listed in a separate statement.

IAM actions are listed one by one instead of being merged into `iam:*`. With `iam:*`, `iam:PassRole` would no longer be limited to Lambda and EventBridge, and the policy would allow attaching any managed policy (for example `AdministratorAccess`) to a role. Security reviews usually reject that.

Requirements:

- Stack names must start with `cdn-prewarm` (for example `cdn-prewarm` and `cdn-prewarm-forwarder`), and the central stack's `EventBusName` must also start with `cdn-prewarm` (default `cdn-prewarm-bus`). All resources in the policy are scoped by this prefix. To use a different prefix, replace every `cdn-prewarm` in the policy.
- Pass `--capabilities CAPABILITY_IAM` when deploying. In the console, check "I acknowledge that AWS CloudFormation might create IAM resources".
- The `iam:PassRole` statement in the policy only allows passing these roles to Lambda and EventBridge.

When you upload a template in the console, the console first stores it in a `cf-templates-*` bucket, so you also need the statement below. This statement was not part of the test described above:

```json
{
  "Effect": "Allow",
  "Action": ["s3:CreateBucket", "s3:PutObject", "s3:GetObject"],
  "Resource": ["arn:aws:s3:::cf-templates-*", "arn:aws:s3:::cf-templates-*/*"]
}
```

If your organization has an SCP that requires a permissions boundary when creating roles, add a `PermissionsBoundary` property to the three `AWS::IAM::Role` resources in the templates.

Permissions of the three roles created by the stacks:

| Stack | Role | Used by | Permissions |
|---|---|---|---|
| Central stack | `PrewarmRole` | Prewarm Lambda | Write to its own log group; `cloudfront:ListDistributions` (only grantable on `*`); `cloudfront:CreateInvalidation` and `cloudfront:GetInvalidation` on all distributions in this account |
| Forwarder stack | `ForwardRole` | Forwarding rule | `events:PutEvents`, limited to the central event bus |
| Forwarder stack | `EnableEventBridgeRole` | Lambda that turns on notifications | Write to its own log group; `s3:GetBucketLocation`, `s3:GetBucketNotification`, and `s3:PutBucketNotification` on each bucket in `BucketNames` |

## Parameters

### Central stack (mapped to Lambda environment variables)

| Parameter | Environment variable | Description |
|---|---|---|
| S3Paths | `S3_PATHS` | S3 paths to monitor, separated by `;`. Format `bucket/prefix/`; `bucket` alone means the whole bucket. Prefixes are treated as directories, so `static` is the same as `static/`. |
| CdnUrlPrefixes | `CDN_URL_PREFIXES` | One URL prefix per S3 path, matched by position. Use the distribution's alternate domain name (such as `www.example.com`) or `dxxxx.cloudfront.net` as the domain. |
| EdgeCountriesOrCities | `EDGE_COUNTRIES_OR_CITIES` | Countries and/or cities to prewarm, separated by `;`. You can mix them, for example `Tokyo;Germany;United States`, which is also the default. See "Writing country and city names" below. |
| FilePatterns | `FILE_PATTERNS` | Only prewarm files whose names match these patterns, separated by `;`, for example `*.mp4;*.m3u8;*.ts`. Default `*`, meaning all files. Matching uses the file name (the part of the key after the last `/`), is case-insensitive, `*` matches any characters, and `?` matches one character. Applies to all S3 paths. Files that don't match are skipped, with a `file name does not match FILE_PATTERNS` log entry. |
| InvalidateBeforePrewarm | `INVALIDATE_BEFORE_PREWARM` | Default `true`: invalidate the URL and wait for completion before prewarming. Keep it on if files are overwritten, otherwise edge locations keep serving the old cached copy. |
| EdgeLocationsUrl | `EDGE_LOCATIONS_URL` | Edge location list JSON, fetched by the Lambda at runtime and cached for 1 hour. Default `https://static.cloudping.cloud/cdn/cloudfront-edge-locations.json`. If fetching fails, the snapshot built into the code is used. See "Edge location list". |

You only configure paths, not individual files. Any file created or overwritten under a path (including subdirectories) is prewarmed automatically. Files that existed before deployment are not prewarmed.

Prewarm URL = URL prefix + the part of the key after the S3 path, URL-encoded. For example:

| S3 path | URL prefix | Uploaded file | Prewarm URL |
|---|---|---|---|
| `bucket-a/static/` | `https://www.example.com/static/` | `s3://bucket-a/static/js/app.js` | `https://www.example.com/static/js/app.js` |
| `bucket-b/img/` | `https://img.example.com/` | `s3://bucket-b/img/logo.png` | `https://img.example.com/logo.png` |

If a file matches several paths, each of them is prewarmed.

Distributions don't need separate configuration. The Lambda uses the domain in the URL prefix to find the matching distribution through the CloudFront API: first an exact alternate domain name match, then a wildcard match (`*.example.com` matches `cdn.example.com` but not `a.b.example.com`). You can also use `dxxxx.cloudfront.net` directly. Lookup results are cached for 1 hour, and a miss triggers one immediate re-lookup. If the distribution still isn't found, the invocation fails and is retried, and the log names the domain.

#### Writing country and city names

Names come from the `country` and `city` fields of the edge location list. You can also find them in `EDGE_SNAPSHOT` in `src/prewarm.py`.

- A country prewarms all edge locations in that country, for example `Japan` or `United States`. Hong Kong and Taipei locations are listed under `China`.
- A city prewarms only the locations in that city, for example `Tokyo`, `Hong Kong`, or `Frankfurt`.
- Countries and cities can be mixed. Overlapping locations are prewarmed once, so `Japan;Tokyo` is the same as `Japan`.
- Matching is case-insensitive and ignores accents, spaces, and punctuation: `montreal` matches `Montréal, QC`, and `Washington DC` matches `Washington D. C.`.
- The state or region code after a city name can be omitted: `Los Angeles` matches `Los Angeles, CA`, and `Taipei` matches `Taipei, TW`. For `Dallas/Fort Worth, TX`, either `Dallas` or `Fort Worth` works.
- A name that matches no edge location logs a warning, and the other names are prewarmed as usual.

The `prewarm_start` log entry of each prewarm has a `nodes_per_name` field showing how many locations each name matched, for example `{"Japan": 7, "Hong Kong": 5}`. Use it to check that names are written correctly.

### Forwarder stack

| Parameter | Description |
|---|---|
| CentralEventBusArn | The `EventBusArn` output of the central stack. |
| BucketNames | Monitored buckets in this region, separated by `;` with no spaces, for example `bucket-a;bucket-b`. |
| KeyPrefixes | Optional. Only forward objects under these prefixes, separated by `;`, for example `static/;img/`. Recommended, because it filters out unrelated writes such as access logs. |
| EnableBucketEventBridge | Default `true`: turns on "Send notifications to Amazon EventBridge" for the buckets, keeping any other notification configuration already on the buckets. Deleting the stack does not turn this setting off. |

All list parameters (in both the central and forwarder stacks) are separated by `;`.

## Edge location list

The Lambda gets the edge location list in this order:

1. The list fetched from `EDGE_LOCATIONS_URL` within the last hour.
2. A fresh fetch from `EDGE_LOCATIONS_URL`.
3. If the fetch fails (network error, non-200 status, invalid JSON, or no locations in the list), the last list successfully fetched in the current Lambda execution environment.
4. If the execution environment has never fetched the list successfully, the snapshot built into the code. The snapshot keeps only "country → city → location code", and its generation date is in `EDGE_SNAPSHOT_DATE`.

The `prewarm_start` log entry of each prewarm has an `edge_list` field recording which list was used: `url`, `url, fetched <time>`, or `snapshot <date>`.

To update the snapshot:

```bash
python3 refresh_edge_snapshot.py   # rewrites the EDGE SNAPSHOT section in src/prewarm.py
python3 build.py                   # regenerates the dist/ templates
```

Then update the central stack with the new template.

## Viewing results

Log group: `/aws/lambda/<central stack name>-prewarm`. A `prewarm_result` entry is written as soon as each edge location finishes, and a `prewarm_summary` entry is written per file after all locations finish. CloudWatch Logs Insights query for locations that did not succeed:

```
fields @timestamp, url, node, outcome, error
| filter type = "prewarm_result" and ok = 0
| sort @timestamp desc
```

The `outcome` field of `prewarm_result`:

| outcome | Meaning |
|---|---|
| `ok` | Returned 2xx, and `x-amz-cf-pop` equals the target location |
| `failed` | This location had an error: no IP resolved, none of the IPs reachable, timeout, non-2xx response, response from a different location, or any other exception |
| `not_started` | Less than 60 seconds of Lambda time remained, so no request was sent |
| `aborted` | The download was stopped midway because less than 15 seconds of Lambda time remained |

`etag_match` indicates whether the ETag returned by the location matches the one in the S3 event. `prewarm_summary` contains counts for each outcome, plus `failed_nodes`, `unfinished_nodes` (locations that were `not_started` or `aborted`), and `skipped_urls`.

## Verifying with curl

After uploading a file, request it from a specific edge location with curl to check whether that location has cached the new file:

```bash
DOMAIN=www.example.com              # the domain in CdnUrlPrefixes
FILE_PATH='/static/js/my_app.js'    # path of the file to check; may contain spaces or non-ASCII characters, it is URL-encoded below
NODE=NRT12-P5                       # location code, taken from this prewarm's prewarm_result logs; see notes below

# Prefix of the distribution domain dxxxx.cloudfront.net. If the domain uses a wildcard alternate domain name (*.example.com), this lookup finds nothing; copy it from the CloudFront console instead
DIST=$(aws cloudfront list-distributions --output text \
  --query "DistributionList.Items[?Aliases.Items && contains(Aliases.Items, '$DOMAIN')].DomainName | [0]" | cut -d. -f1)

URL_PATH=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$FILE_PATH")

curl -sI -m 15 --retry 3 --retry-all-errors -H 'Accept-Encoding: gzip, deflate, br' \
  --connect-to "$DOMAIN:443:$DIST.$NODE.cloudfront.net:443" \
  "https://$DOMAIN$URL_PATH" | grep -iE '^(HTTP/|x-cache:|x-amz-cf-pop:|etag:|age:)'
```

`--connect-to` makes curl connect to the location domain `dxxxx.<location code>.cloudfront.net` while the URL, Host, and SNI stay the site domain, the same as a real user request. curl resolves the location domain itself and moves on to the next IP if one is unreachable. If the connection is reset midway, `--retry 3 --retry-all-errors` tries up to 3 more times (requires curl 7.71 or later; the version shipped with macOS qualifies).

Don't use `dxxxx.<location code>.cloudfront.net` as the URL directly. CloudFront only accepts the distribution's alternate domain names and `dxxxx.cloudfront.net`, so HTTPS fails the handshake and HTTP returns 403. That domain is only used to find the location's IP.

Output for a successful prewarm looks like this:

```
HTTP/2 200
etag: "b1bf01b86ec379951e7dedcd78e199c9"
x-cache: Hit from cloudfront
x-amz-cf-pop: NRT12-P5
age: 55
```

How to read it:

- `x-amz-cf-pop` equals `NODE`: the request really reached this location.
- `x-cache` is `Hit from cloudfront`: this location already has the file cached.
- `etag` equals the file's ETag in S3: the cached copy is the new version. Check the S3 ETag with `aws s3api head-object --bucket <bucket> --key <key> --query ETag`.

Notes:

- **Use a location code from this prewarm.** Edge locations come and go. A location from a web page or an old list may no longer be in the current list, so the Lambda won't prewarm it, and some can't be reached at all. Query the central stack's log group with Logs Insights for the locations where this file was prewarmed successfully:

  ```
  fields node, x_cache, etag_match
  | filter type = "prewarm_result" and outcome = "ok" and url like "app.js"
  | sort node
  ```

- **No output at all**: your network can't reach this location, which has nothing to do with prewarming. Remove `-s` to see curl's error, or try another location or another network.
- `-I` fetches only the response headers and doesn't download the whole file.
- Always send `Accept-Encoding: gzip, deflate, br`. Without this header you hit a different cached copy and see `Miss`.

## Fault tolerance

- Any error at a single edge location only marks that location as `failed`; the other locations continue. Failed locations are not retried.
- If a file matches several paths and the domain of one path has no matching distribution, only that path is skipped: an ERROR log entry is written, the URL is added to `skipped_urls`, and the other paths continue.
- The Lambda runs for at most 15 minutes. With less than 60 seconds left it sends no new requests, and in-flight downloads are stopped at 15 seconds left, so that every location's result and the summary make it into the logs. Locations left unfinished for lack of time don't trigger a retry, because a retry would hit the same time limit.
- If the edge location list can't be fetched, the last successfully fetched list and then the built-in snapshot are used. See "Edge location list".

In the following cases the invocation fails, and Lambda retries it 2 more times (3 attempts in total):

- None of the requests sent succeeded, and at least one was `failed`.
- None of the matching paths has a matching distribution.
- None of the configured countries and cities matches any edge location.
- Configuration error, for example `S3Paths` and `CdnUrlPrefixes` have different numbers of entries.
- The invalidation request returns an error or doesn't complete within 5 minutes.

Events that fail all 3 attempts are dropped without being stored, and the reason for each failure is in the log group. The solution doesn't use a dead-letter queue because missing a prewarm has little impact: on the first user request the edge location fetches from the origin itself, and only that one request is slower. To prewarm again, upload the file again. Log query for Lambda errors:

```
fields @timestamp, errorType, errorMessage, message
| filter level = "ERROR"
| sort @timestamp desc
```

When an invocation fails, the reason is in the `errorMessage` field. When a path is skipped, the reason is in the `message` field.

If cross-region forwarding in EventBridge fails, EventBridge retries automatically for up to 24 hours and then drops the event as well.

## Caveats

- **The distribution must allow caching.** If the cache policy is `Managed-CachingDisabled` or the TTL is 0, prewarming has no effect.
- **Traffic and cost.** For every uploaded file, the Lambda downloads about `file size × number of locations` from CloudFront. For example, prewarming a 100 MB file at 50 locations is 5 GB, billed as CloudFront data transfer. Origin traffic from S3 is lower, because several locations share a regional edge cache. The first 1,000 CloudFront invalidation paths each month are free, and each additional path costs $0.005.
- **Compressed versions.** Prewarm requests send `Accept-Encoding: gzip, deflate, br`, like a browser. Clients that don't send this header (for example curl by default) hit a different cached copy.
- **Edge location list.** The list comes from a third-party site, static.cloudping.cloud, and some locations in it may be offline. Offline locations are recorded as failed and don't affect the others. A location usually has several IPs, and the next IP is tried when one can't be reached.
- **Don't monitor paths that logs are written to.** If the bucket's access logs or CloudFront logs are written under a monitored path, the logs produced by each prewarm trigger another prewarm, creating a loop.
- **Changing configuration.** Editing environment variables in the Lambda console takes effect immediately, but the next stack update overwrites them with the template parameters. For lasting changes, update the stack parameters.
- **Delete events.** Delete events don't trigger anything.
