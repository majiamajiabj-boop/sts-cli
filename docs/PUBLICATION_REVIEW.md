# Public publication review

Reviewed on 2026-09-22 before public publication.

## Scope and changes

- Inspected the previously published branch history, release tag, source files, tracked screenshot, and Windows ZIP.
- Removed machine-specific personal username paths from the process guide and experiment build instructions. The experiment build now resolves Python from PATH or its explicit parameter.
- Used the account's GitHub noreply email for public commit metadata.
- Preserved the original repository as a private history backup. The public repository receives clean commits rather than the old commits containing a personal email.
- Retained a sanitized source checkpoint for the existing Windows release and a current source checkpoint with the reorganized layout and English README.
- Checked for credential patterns and compared the actual locally configured secret values against candidate files and the ZIP without printing or publishing those values. No matching credentials were found.
- `.env`, runtime logs, personal saves, local archives, and tool caches are excluded. Regression fixtures and the report-viewer screenshot were reviewed as development material.

Third-party license attribution emails remain intact. Paths and example IP addresses embedded in the bundled Python/Tcl/OpenSSL runtime belong to the upstream runtime or its examples, not the local user's machine identity.

## Validation

The original release ZIP is unchanged and retains SHA-256 `62ff5e12dde765c5b813d986782869b563ceb4fe302d5413cdbdea8485bfa97e`. Its published validation remains 2,947 tests; the reorganized source previously passed 2,948 tests and a full build. This publication adds documentation, removes personal path defaults, and changes repository metadata. Three targeted layout/build tests, PowerShell parsing, relative README links, and whitespace checks passed. No game was launched and no additional live-play acceptance is claimed.

This is a recorded review of the inspected content, not a guarantee about future commits. Share the original ZIP and keep credentials in ignored local configuration.
