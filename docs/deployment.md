# Deploying into your own AWS account

This guide takes you from an empty AWS account to a working copy of the API at
`https://api.<your-domain>/v1`. Everything runs in **your** account; nothing
talks back to the original project.

There are two ways to deploy. Both use the same `deploy.ps1`, so you can start
with one and switch later:

| | From your machine | From GitHub Actions |
|---|---|---|
| Good for | trying it out, one-off deploys | repeatable deploys, teams |
| Credentials | your own AWS CLI login | short-lived OIDC role, no keys stored in GitHub |
| Setup | AWS CLI, PowerShell, Python | one script run once, then a button |

(There is also a Terraform version in [`terraform/`](../terraform/README.md).
Use either CloudFormation or Terraform in an account, not both.)

## What it costs

Almost everything is pay-per-request (Lambda, API Gateway, DynamoDB on-demand,
S3, SES), so an idle deployment costs very little. The fixed costs are the
Route 53 hosted zone (USD 0.50/month) and your domain registration. Text-to-speech
(`voice/say`) is capped by `-VoiceDailyCharacters` (default 25,000 characters a
day, at most about USD 0.40–0.75/day). Real SMS sends cost whatever SNS charges
and are off until you allow-list numbers.

## 1. Before you start

You need:

- **An AWS account** where you can create IAM roles, Lambda functions,
  API Gateway, DynamoDB, S3, SES, ACM and Route 53 records.
- **A domain** whose DNS is a **public Route 53 hosted zone in that same account**.
  The API lives at `api.<domain>` and the stack receives email for
  `anything@<domain>`, so use a domain (or subdomain) dedicated to testing.
- **A region that supports SES email receiving.** The default is
  `ap-southeast-2` (Sydney); `us-east-1`, `us-west-2` and `eu-west-1` also
  work. Check the current list in the
  [SES email receiving documentation](https://docs.aws.amazon.com/ses/latest/dg/regions.html#region-receive-email).

### Getting the domain into Route 53

Pick whichever applies:

- **Buy a new domain in Route 53** (console → Route 53 → Registered domains).
  The hosted zone is created for you and already delegated. Nothing more to do.
- **A domain you already own elsewhere:** create the zone, then copy its four
  name servers into your registrar's settings:

  ```powershell
  aws route53 create-hosted-zone --name example.com --caller-reference (Get-Date -Format o)
  aws route53 get-hosted-zone --id <ZoneId> --query DelegationSet.NameServers
  ```

- **A subdomain of a domain you already run** (e.g. `testdata.example.com`):
  create a zone for the subdomain as above, then add an `NS` record for
  `testdata.example.com` in the **parent** zone listing the four name servers.

Name-server changes can take from minutes to a couple of days to take effect.
`deploy.ps1 -CheckOnly` (below) tells you when they have. Don't skip this:
the certificate can't be validated until the delegation works, and a first
deploy started too early sits waiting for hours before it fails.

## 2. Get the code

```powershell
git clone https://github.com/OpenTDS/opentds-helper-apis.git
cd opentds-helper-apis
```

To deploy from GitHub Actions you need the repository under **your own**
GitHub account or organisation: **Fork** it on GitHub (or push the clone to a
new repository of your own) and clone that instead.

## 3a. Deploy from your machine

Install the tools (Windows shown; on macOS/Linux install the same three with
your package manager — `deploy.ps1` runs under PowerShell 7 on any OS):

```powershell
winget install Amazon.AWSCLI
winget install Microsoft.PowerShell
winget install Python.Python.3.12
```

**No `winget`?** Windows Server 2019 and 2022 usually don't have it. Use the
installers instead, from PowerShell run as Administrator:

```powershell
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; $ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest https://awscli.amazonaws.com/AWSCLIV2.msi -OutFile "$env:TEMP\AWSCLIV2.msi"
Start-Process msiexec.exe -ArgumentList "/i `"$env:TEMP\AWSCLIV2.msi`" /qn" -Wait
```

Git: https://git-scm.com/download/win. Python 3 (tick *Add python.exe to PATH*):
https://www.python.org/downloads/windows/. Windows PowerShell 5.1, already on
every Windows Server, is enough; PowerShell 7 is optional. Python is optional
too: without it `deploy.ps1` only skips a documentation freshness check.

Open a **new** terminal so the tools are on PATH, then sign in to the target
account. On a server, `aws configure sso --use-device-code` lets you approve the
sign-in in your own PC's browser. If the server has an IAM role attached, always
set `AWS_PROFILE`: without it the CLI silently uses the instance's own account. With IAM Identity Center (SSO):

```powershell
aws configure sso          # once: creates a named profile
aws sso login --profile my-test-account
$env:AWS_PROFILE = 'my-test-account'
```

(or `aws configure` with access keys, if that's how your account works).

**Dry run first.** This checks the tools, your credentials, the hosted zone,
the name-server delegation and SES, then reports what a deploy would do. It
creates and changes nothing:

```powershell
.\deploy.ps1 -Domain example.com -CheckOnly
```

When that passes, deploy:

```powershell
.\deploy.ps1 -Domain example.com
```

It shows every setting and asks before going ahead. The first deploy takes
about 5–10 minutes, mostly certificate validation. At the end it prints the
stack outputs and a URL to try.

Common options: `-Region`, `-StackName`, `-ApiHostLabel` (default `api`),
`-EmailSendAllowedDomains`, `-EmailSendAllowedTo`, `-Yes` (no prompts). For a
second copy in the same account, change `-StackName` **and** `-BucketPrefix`.
`Get-Help .\deploy.ps1 -Full` lists everything.

## 3b. Deploy from GitHub Actions

GitHub Actions signs in to AWS with **OpenID Connect**: AWS trusts tokens that
GitHub issues to your repository's workflows, so no AWS access keys are ever
stored in GitHub.

**One-off setup**, from your machine, signed in to the target account as in 3a
(the GitHub CLI is optional but saves the manual step; `winget install GitHub.cli`,
then `gh auth login`):

```powershell
.\scripts\setup-github-deploy.ps1 -Domain example.com
```

The script:

1. reads your repository name from `git remote get-url origin`;
2. deploys [`infra/github-deploy-role.yaml`](../infra/github-deploy-role.yaml),
   which creates GitHub's identity provider in the account (or reuses the one
   already there) and a role that **only** workflow jobs from your repository,
   running in the `aws-deploy` environment, can assume;
3. with `gh`, creates the `aws-deploy` environment and sets the repository
   variables `AWS_ROLE_ARN`, `AWS_REGION`, `DOMAIN` and `STACK_NAME`. Without
   `gh` it prints them for you to add under **Settings → Secrets and variables
   → Actions → Variables**.

**Strongly recommended:** in **Settings → Environments → aws-deploy**, add
yourself as a **required reviewer**. Every deploy then waits for your approval.

**Deploy:** **Actions → Deploy to AWS → Run workflow**. "Check only" is ticked
by default, which gives the same dry run as `-CheckOnly`. Read the log, then
run it again with the box unticked to deploy. To deploy on every push to
`main` as well, uncomment the `push:` trigger in
[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml).

Optional repository variables, passed to `deploy.ps1` only when set:
`EMAIL_SEND_ALLOWED_DOMAINS`, `EMAIL_SEND_ALLOWED_TO`,
`SMS_SEND_ALLOWED_NUMBERS`. Optional **environment secret** `SMS_DEVICE_KEYS`:
the capture-handset keys (leave it unset if you don't run handsets; that turns
`sms/receive` off).

**What the role can do:** it has AWS's `PowerUserAccess` policy (every service
except IAM), plus IAM rights limited to roles whose names start with the stack
name. That is enough to build and update this stack. It can't create users,
access keys, or roles for anything else. You can tighten it further for your
account by editing `infra/github-deploy-role.yaml` and re-running the setup
script.

## 4. After the first deploy

**Check it.** `scripts/test-deployment.ps1` exercises every service through its public URLs
and prints a pass/fail table. It needs nothing but PowerShell (no AWS credentials, Postman or
Python) and takes about a minute:

```powershell
.\scripts\test-deployment.ps1 -Domain example.com
```

It covers discovery, lists (queues, variables, counters, cleanup register, import), names,
lock, TOTP codes (checked against a local RFC 6238 calculation), QR, webhooks, perf, SMS
(simulated texts only), mail (a real email to your own domain and back, attachments) and
voice. Test data goes under a fresh namespace each run and is cleared afterwards. The exit
code is 0 when everything passes, so it also works in a pipeline. Useful options:
`-Only lists,mail`, `-Skip voice` (avoids the one short Polly synthesis), `-ResultFile
results.json` (or `.csv`). Straight after a first deploy the new hostname can take a minute
or two to resolve; the script waits for it.

Then open `https://api.example.com/v1/llms.txt` for the full guide.

**SES production access (one manual step).** A new account's SES is in the
*sandbox*: `mail/send` can only reach verified addresses (200 a day), and
sends elsewhere return 502. Receiving mail at your domain works straight away.
To send to your allow-listed test domains, request production access in the
deploy region:

```powershell
aws sesv2 put-account-details --region ap-southeast-2 `
  --production-access-enabled --mail-type TRANSACTIONAL `
  --website-url "https://api.example.com/v1" `
  --use-case-description "Test-automation API. Sends synthetic test emails to its own catch-all domain and a short allow-list of test domains."
```

AWS usually replies within a day.

**SMS (optional).** Real texts need SNS SMS production access and a spend
limit in `-SmsRegion`, plus the handset numbers in `-SmsSendAllowedNumbers`.
`sms/mocksend` works without any of that. See `docs/guide/sms.md`.

## Updating

Pull the latest code (or merge it into your fork) and run the same deploy
again, from your machine or from Actions. Settings you don't pass keep their
deployed values. Pass a setting explicitly to change it, or pass `''` to clear
a list.

## Removing it

```powershell
aws cloudformation delete-stack --stack-name testhelper-v1 --region ap-southeast-2
```

The DynamoDB tables and S3 buckets are **retained** on purpose, so test data
survives an accidental delete. To remove them as well (this is permanent):

```powershell
$acct = aws sts get-caller-identity --query Account --output text
$r = 'ap-southeast-2'
foreach ($b in 'imports','mail-attachments','mail-inbound','voice') {
    aws s3 rb "s3://testhelper-v1-$b-$acct-$r" --force
}
aws dynamodb list-tables --region $r --query "TableNames[?starts_with(@, 'testhelper-v1-')]" --output text
#   then: aws dynamodb delete-table --table-name <name> --region $r   for each one
```

Until those buckets are removed, deploying again with the same `-BucketPrefix`
fails, because S3 bucket names must be unique. Either remove them or use a new
prefix. If you used GitHub Actions, also delete the `testhelper-v1-github-deploy`
stack.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No public Route 53 hosted zone named …` | The zone is missing or in another account. See step 1. |
| `… is delegated to …, but hosted zone … uses …` | The registrar's name servers don't point at the zone. Fix them at the registrar, wait, and run `-CheckOnly` again. |
| First deploy stuck on `ApiCertificate` | DNS delegation isn't working yet, so ACM can't validate. Delete the stack (a first create can't be cancelled), remove any buckets it left behind (see *Removing it*), fix the delegation, and redeploy. |
| `api.<domain> is already an API Gateway custom domain owned by something else` | Another stack uses that hostname. Use `-ApiHostLabel` or a different domain. |
| A receipt rule set is already active | Only one can be active per region. Choose **A** to add this stack's rule to it; `-Yes` and Actions do that automatically. |
| `mail/send` returns 502 | SES is still in the sandbox. See step 4. |
| Actions: `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The role trusts an exact `owner/repo` and environment, and case matters. Re-run the setup script from your own clone, and check the job uses the `aws-deploy` environment. |
| Actions: `Set repository variable(s): …` | Run the setup script, or add the variables by hand. |
| `Python was not found; run without arguments to install from the Microsoft Store` | Windows' placeholder `python.exe`, not a real Python. Harmless: `deploy.ps1` notices and skips the docs check with a warning. Install Python 3 to enable it. |
| `Discovery documents are stale` | You edited `docs/`; run `python tools/build_docs.py` and commit the result. |
