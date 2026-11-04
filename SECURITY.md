# Security and privacy

This is a research prototype with **mocked robot capabilities**, not a safety-rated
controller. Models, transcripts, summaries, historical messages and tool strings
are untrusted data. A model response is not hardware authorization. A future robot
adapter must enforce local policy, resource arbitration, watchdogs and emergency
stop independently of the cloud.

Use a restricted OpenAI project/key and provider-side spending alerts. Local cost
estimates are not a billing cap. Never expose this CLI, its database or exports as
a public service. Anyone or any recording near an enabled microphone may speak
the wake phrase; no speaker authentication is implemented.

Keep `.env` and runtime directories private. Key redaction is best-effort defense
in depth, not an absolute credential-removal guarantee or a personal-data scrubber.
It checks dictionary keys and complete string values, but does not stream across
transcript fragments: split credentials can survive and later be reconstructed.
Never speak credentials or treat redacted exports as safe to publish.
Deleting local records does not erase provider
retention, backups or separately saved fixtures. Rotation/revocation is necessary
if a credential is exposed, even if a Git commit is later removed.

Report suspected vulnerabilities through the repository's private security advisory
channel if available, or contact the maintainer privately. Do not post credentials,
private transcripts or weaponized physical-robot instructions in public issues.
Until a coordinated disclosure channel is configured, share only a minimal,
non-sensitive description publicly and request a private contact.
