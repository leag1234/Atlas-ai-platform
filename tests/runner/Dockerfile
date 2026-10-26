FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server git util-linux && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1001 runner && passwd -d runner \
    && mkdir -p /run/sshd /home/runner/.ssh /home/runner/work \
    && chown -R runner:runner /home/runner
RUN printf '%s\n' 'Port 2222' 'HostKey /etc/ssh/ssh_host_ed25519_key' 'PasswordAuthentication no' 'PermitEmptyPasswords no' 'PermitRootLogin no' 'AllowUsers runner' 'AllowTcpForwarding no' 'X11Forwarding no' 'PermitTunnel no' 'Subsystem sftp internal-sftp' 'PidFile /run/sshd/sshd.pid' > /etc/ssh/sshd_config
EXPOSE 2222
CMD ["/usr/sbin/sshd", "-D", "-e"]
