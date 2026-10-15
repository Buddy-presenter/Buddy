# Deployment and release

Buddy's desktop release and backend deployment are separate operations. The desktop is a
native PySide6 application; the backend is a private API/worker service. Neither component
ships privileged credentials. Local installation and run commands are in
[development](development.md).

Terraform roots, infrastructure boundaries, AWS service configuration, and deployment
procedures are documented in [infrastructure](infrastructure.md). Backend release,
migration, promotion, and rollback behavior are described in [releases](releases.md).
Desktop packaging, artifact verification, signing, and update boundaries are covered in
that release guide as well. CI and workflow ownership are documented in [CI](ci.md).

These guides describe the configured implementation and its prerequisites. A successful
local build or infrastructure validation does not itself mean a cloud environment has
been provisioned or a live release has been approved.
