from importlib import import_module

from recovery_service.core.docker_oracle import RemoteDockerOracle
from recovery_service.core.domain import RemoteHost
from recovery_service.infrastructure.docker.remote_executor import RemoteDockerImpdpExecutor

ImpdpRunner = import_module("recovery_service.engine.import.impdp_runner").ImpdpRunner


def create_impdp_runner(
    ssh_host: RemoteHost,
    options: dict,
    remote_directory: str,
) -> ImpdpRunner | RemoteDockerImpdpExecutor:
    docker_cfg = RemoteDockerOracle.from_options(options, remote_directory)
    if docker_cfg:
        errs = docker_cfg.validate()
        if errs:
            raise ValueError("; ".join(errs))
        return RemoteDockerImpdpExecutor(ssh_host, docker_cfg)
    return ImpdpRunner()
