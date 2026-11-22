# Changelog

All notable changes to this project are recorded here. Versions follow
[Semantic Versioning](https://semver.org/): the API path carries the major
version (`/v1`), so a breaking API change would be a new `/v2`, never a change
to `/v1`.

## 1.3.0 - 2026-11-23

The API contract version reported by the deployment (`src/version.json`). 1.1 and
1.2 were never published separately; everything since 1.0.0 is listed here.

### New services

- `recon`: reconciles what a test submitted against what the system under test
  processed. `recon/expect` and `recon/confirm` record the two sides, `recon/get`
  gives the counts (matched, outstanding, unexpected, mismatched), and `recon/list`
  and `recon/cyclepop` hand out the records still to check.
- `health`: `health/get` reports the deployed version, build, commit and region;
  `health/check` sends a request with a known answer to every service and reports
  the result.
- `trace`: `trace/get` returns the AWS X-Ray trace of a request, cleaned and limited
  to this deployment, as JSON or an HTML page.

### Added

- `voice/listen`, `voice/find` and `voice/compare`: transcribe audio and check what
  was said, with a daily listening budget (`VoiceDailyListenSeconds`).
- `mail/attachments/make` generates a real PDF of a given page count or exact size,
  with known text on known pages and optional password protection. `mail/send`
  can attach a generated PDF in the same way (`filename=....pdf` with `pages` or
  `attachmentkb`).
- Every response carries an `X-Build` header naming the build that served it.
- X-Ray tracing, on by default (`Tracing` parameter / `tracing` variable).

### Not yet covered

- `scripts/test-deployment.ps1` does not yet test `recon`, `health` or `trace`; run
  `/v1/health/check` after a deploy to exercise them.

## 1.0.0 - 2026-11-19

First public release.

### Services

`lists`, `names`, `lock`, `mail`, `sms`, `webhook`, `totp`, `qr`, `perf` and
`voice`, at `https://api.<your-domain>/v1`, with discovery documents
(`/v1/openapi.json`, `/v1/openapi-3.0.json`, `/v1/llms.txt`, `/.well-known/*`).

### Deployment

- `deploy.ps1` deploys the CloudFormation stack into your own AWS account.
  `-CheckOnly` is a read-only dry run: it checks the tools, credentials, Route 53
  zone, name-server delegation and SES, and reports what a deploy would do.
- `scripts/test-deployment.ps1` checks a deployment end to end (52 tests, about a
  minute, PowerShell only) and prints a pass/fail table.
- `terraform/` builds the same stack with Terraform.
- New, not yet proven against a real account: GitHub Actions deploys with
  short-lived OIDC credentials (`scripts/setup-github-deploy.ps1`,
  `.github/workflows/deploy.yml`).

### Verified

- Deployed from a fresh clone into a new AWS account from Windows Server 2022
  with Windows PowerShell 5.1.
- Offline test suite (pytest + moto, and the Node.js lists suite) on every push.
