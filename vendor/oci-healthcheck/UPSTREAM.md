# OCI Health Check

Created by Richard Garsthagen (RichardORCL).
Source: https://github.com/RichardORCL/OCI-Healthcheck
Bundled revision: `95589c2c2a8f708389cc6cc8dfd87e928db171bd`.

Integrated with the project owner's stated permission. The original README and
checklist template are retained alongside the original application.

Console adaptations:

- Only the OCI Storage checklist is bundled. OCVS is excluded; existing copies
  on upgraded installations are hidden without deleting customer edits.

- FastAPI serves the application and storage API; no separate server is started.
- `js/console-bridge.js` supplies the existing console session to API requests.
- Editor enablement uses that session instead of a separate editor password.
- `server.py` storage helpers can be imported without a standalone password file.
- Editable definitions and feedback live outside the checkout. Unmodified bundled
  definitions can be updated; customer changes and added definitions are retained.

When updating this snapshot, review upstream changes, reapply these adaptations,
update this revision, and run the integration tests. Do not replace customer data.
