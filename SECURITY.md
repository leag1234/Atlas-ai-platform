# Security policy

Atlas is in beta. Only the latest stable release, deployed at atlas.eu.com, receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately, never in a public issue: use "Report a vulnerability" in the Security tab of this repository (a private advisory). Include what you found, how to reproduce it, and its impact.

We acknowledge reports within 3 working days and aim to fix critical issues within 7 days. We credit reporters who wish it once the fix is released.

## Scope

In scope: the Atlas services in this repository and the hosted instance.

Out of scope: third-party providers and models, denial of service by volume, findings that need an already compromised account or device.

## What we do

- Dependencies are audited for known vulnerabilities and updated; critical issues are fixed first.
- Every change is reviewed for security before release. Sensitive areas (authentication, the front door, the terminal sandbox, file reading, outbound fetch) are reviewed again each month.
- Nothing users send through the API is stored on the server.
