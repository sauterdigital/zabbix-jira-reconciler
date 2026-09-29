# Zabbix ↔ Jira Reconciler

Automated reconciliation between Zabbix alerts and Jira Service Management issues.

## Why this exists

Dynamic infrastructure such as Kubernetes and GKE can remove or recreate Pods and Nodes before a normal recovery event reaches Jira.

This can leave Jira alert tickets open even though the corresponding Zabbix problem is no longer active.

This project periodically reconciles both systems and safely resolves stale Jira alerts.

## Architecture

~~~text
Cloud Scheduler
      |
      | every 10 minutes
      v
Cloud Run Job
      |
      +---- Zabbix API
      |
      +---- Jira API
      |
      +---- Firestore
             |
             +-- missing_since / grace-period state
~~~

## How it works

1. Retrieves open alert issues from Jira.
2. Retrieves current problems from Zabbix.
3. Verifies which Zabbix triggers are still monitored.
4. Classifies Jira issues as:
   - `ACTIVE`
   - `CANDIDATE`
   - `SKIPPED`
5. Candidates enter a configurable grace period.
6. Only issues that remain absent after the grace period are eligible for resolution.
7. Before resolution, the automation:
   - assigns the issue;
   - adds an audit comment;
   - transitions it to the resolved status.
8. Firestore state is removed after successful resolution or when the alert becomes active again.

## Safety

The project follows a fail-safe approach:

- no credentials are stored in source code;
- API tokens are supplied through environment variables or secret management;
- Firestore persists the grace-period state;
- an API failure prevents unintended bulk resolution;
- audit comments are not duplicated;
- active alerts are preserved;
- a configurable maximum number of resolutions can be enforced per run;
- `DRY_RUN` mode allows validation without modifying Jira.

## Environment variables

### Jira

~~~text
JIRA_URL
JIRA_EMAIL
JIRA_API_TOKEN
JIRA_JQL
JIRA_ASSIGNEE_ACCOUNT_ID
~~~

### Zabbix

~~~text
ZABBIX_API_URL
ZABBIX_TOKEN
~~~

### Runtime

~~~text
DRY_RUN
MAX_RESOLVE_PER_RUN
GRACE_PERIOD_MINUTES
FIRESTORE_COLLECTION
~~~

## Example environment

See `.env.example`.

Example:

~~~env
# Jira
JIRA_URL=https://your-domain.atlassian.net
JIRA_EMAIL=automation@example.com
JIRA_API_TOKEN=replace-with-secret
JIRA_ASSIGNEE_ACCOUNT_ID=replace-with-jira-account-id
JIRA_JQL=project = ABC AND issuetype = Alert AND status NOT IN (Resolved, Closed)

# Zabbix
ZABBIX_API_URL=https://zabbix.example.com/api_jsonrpc.php
ZABBIX_TOKEN=replace-with-secret

# Runtime
DRY_RUN=true
MAX_RESOLVE_PER_RUN=5
GRACE_PERIOD_MINUTES=10
FIRESTORE_COLLECTION=zabbix_jira_reconciler
~~~

## Components

- Python 3.13
- Google Cloud Run Jobs
- Google Cloud Scheduler
- Google Firestore
- Google Secret Manager
- Google Artifact Registry
- Jira REST API
- Zabbix API

## Files

~~~text
reconciler.py      Zabbix/Jira correlation logic
runner.py          Execution, grace period and Jira mutations
Dockerfile         Container image definition
requirements.txt   Python dependencies
.env.example       Example configuration
.gitignore         Local and sensitive files excluded from Git
.gcloudignore      Files excluded from Google Cloud builds
~~~

## Grace period

The reconciler does not immediately resolve a Jira issue when the corresponding Zabbix problem disappears.

Instead, the first missing detection is persisted in Firestore:

~~~text
Alert disappears
      |
      v
FIRST MISSING
      |
      | save missing_since
      v
WAITING
      |
      | grace period elapsed
      v
READY
      |
      v
Resolve Jira issue
~~~

If the alert becomes active again before the grace period expires, the stored `missing_since` state is removed and the Jira issue is preserved.

## Jira resolution flow

For each eligible issue, the runner performs:

~~~text
Assign issue
    |
    v
Check audit comment
    |
    +-- already exists --> skip duplicate
    |
    v
Add audit comment
    |
    v
Find transition to resolved status
    |
    v
Resolve issue
~~~

The workflow also supports fallback transitions when the current Jira status cannot transition directly to the resolved state.

## Deployment

The application is designed to run as a containerized Google Cloud Run Job.

A typical production architecture is:

~~~text
Cloud Scheduler
      |
      | cron
      v
Cloud Run Job
      |
      +---- Secret Manager
      |
      +---- Firestore
      |
      +---- Jira API
      |
      +---- Zabbix API
~~~

Before deploying, configure all required environment variables and secrets in your cloud environment.

## Scheduling

A typical schedule is every 10 minutes:

~~~cron
*/10 * * * *
~~~

The grace period can also be configured independently using:

~~~text
GRACE_PERIOD_MINUTES
~~~

## Recommended production settings

Example:

~~~text
DRY_RUN=false
MAX_RESOLVE_PER_RUN=10
GRACE_PERIOD_MINUTES=10
FIRESTORE_COLLECTION=zabbix_jira_reconciler
~~~

Always test with:

~~~text
DRY_RUN=true
~~~

before enabling automatic Jira modifications.

## Security

Never commit:

- Jira API tokens
- Zabbix API tokens
- service-account keys
- `.env` files containing real credentials
- private certificates or keys
- cloud credentials

Use Secret Manager or another secure secret-management solution in production.

## Disclaimer

Review and test the reconciliation rules against your own Jira workflow and Zabbix configuration before enabling automated issue resolution.

Different Jira workflows may expose different transition IDs, required fields, statuses and validation rules.
