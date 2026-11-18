# Changelog

All notable changes to this project are recorded here. Versions follow
[Semantic Versioning](https://semver.org/): the API path carries the major
version (`/v1`), so a breaking API change would be a new `/v2`, never a change
to `/v1`.

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
