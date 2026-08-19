from recovery_service.core.domain import DumpVolumeGroup, RemoteHost
from recovery_service.core.exceptions import DiscoveryError
from recovery_service.engine.discovery.volume_grouper import classify_artifacts
from recovery_service.infrastructure.ssh.sync_client import list_remote_artifacts


class RemoteScanner:
    def scan(self, host: RemoteHost, remote_directory: str) -> list[DumpVolumeGroup]:
        artifacts = list_remote_artifacts(host, remote_directory)
        if not artifacts:
            raise DiscoveryError(f"No dmp/log/par files in {remote_directory}")
        groups = classify_artifacts(artifacts)
        if not groups:
            raise DiscoveryError("No dump volume groups identified")
        return groups
