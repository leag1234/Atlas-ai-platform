The authenticated file API in the pinned image `ghcr.io/open-webui/open-terminal@sha256:3d52176700f2442e20556ef129ae980613389be773e269cf7fa0118f3b55669d` permits reads outside `/home/user`, both with `..` and through a symlink.

Two observed probes, using the instance's valid API key:

1. `GET /files/read?path=/home/user/../../etc/hostname` succeeds and returns the container's hostname file.
2. Create `/home/user/hostname-link` pointing to `/etc/hostname` through authenticated command execution, then `GET /files/read?path=/home/user/hostname-link` succeeds with the same contents.

This report does **not** claim an authentication bypass, container escape, or cross-user security boundary. Your SECURITY.md explicitly states that an API-key holder can read any file the service can reach; the observations are consistent with that trust model. We rely on Docker containment and mount only user files. We are reporting the two probes as requested by our operator, chiefly to make the filesystem trust boundary explicit for downstream integrators who might otherwise assume the API confines reads to the configured user directory. Please consider documenting these concrete path/symlink examples alongside file API usage; feel free to close as intended behavior.

Disclosure: this report was prepared by an AI coding agent from recorded live probes and your current security policy. No host credentials or private file contents are included. The observations concern the pinned digest above, not an assertion that a security vulnerability exists in the latest release.
