# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button
(Security → Advisories) on this repository. Do not open a public issue.

Include the affected package and version, reproduction steps, and impact. We aim to acknowledge
reports within 3 business days and to ship a fix or mitigation within 30 days for high-severity
issues. We credit reporters in the advisory unless you prefer otherwise.

## Scope

In scope: all packages in this repository, the GitHub Action, the admin UI, and generated
project templates. Out of scope: vulnerabilities in sites built with RedBlue that stem from
code the site owner wrote, and issues requiring a compromised host.

## Design commitments

- The security gate's Red agent only scans previews it launched itself. It has no target-URL
  input and refuses non-loopback hosts. Reports that it can be pointed at third-party hosts
  are treated as high severity.
- The GitHub Action never uses `pull_request_target` with a checkout of PR code; fork PRs run
  without secrets and skip AI steps.
- Blue agent fixes are proposed as pull requests and never merged automatically.
- Agents cannot publish, deploy, send email, or post; those actions require a human with the
  right role.
- Secrets are read from the environment only.

The project runs its own gate on every pull request (`.github/workflows/redblue.yml`).
