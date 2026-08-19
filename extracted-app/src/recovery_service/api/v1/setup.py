from fastapi import APIRouter, Depends

from recovery_service.api.deps import verify_api_key
from recovery_service.api.schemas.setup import (
    ConfigTemplateResponse,
    SetupCheckAllResponse,
    SetupCheckRequest,
    SetupStepResponse,
)
from recovery_service.services import setup_validator
from recovery_service.settings import get_settings

router = APIRouter(prefix="/setup", tags=["setup"])


def _body_to_dict(body: SetupCheckRequest) -> dict:
    data = body.model_dump()
    if body.ssh_password:
        data["ssh_password"] = body.ssh_password.get_secret_value()
    if body.target_admin_password:
        data["target_admin_password"] = body.target_admin_password.get_secret_value()
    if body.execution:
        data["execution"] = body.execution.model_dump()
    return data


@router.get("/template", response_model=ConfigTemplateResponse)
async def get_config_template(_: None = Depends(verify_api_key)):
    return ConfigTemplateResponse(
        description="恢复服务部署在 A 机；DMP + Oracle Docker 在 B 机。通过 SSH + docker exec 执行 impdp。",
        env_variables={
            "说明": "生产环境可将下列值写入 task.options.execution，或使用分步检测 API 验证",
        },
        task_example={
            "remote_host": "<DMP服务器IP>",
            "remote_port": 22,
            "remote_user": "root",
            "remote_password": "***",
            "remote_directory": "/data/oracle/dump",
            "target_connection": "<目标19c IP>:1521/ORCLPDB1",
            "target_admin_user": "system",
            "target_admin_password": "***",
            "options": {
                "auto_confirm": False,
                "execution": {
                    "mode": "remote_docker",
                    "docker_container": "oracle19c",
                    "dmp_host_path": "/data/oracle/dump",
                    "dmp_container_path": "/opt/oracle/admin/ORCL/dpdump",
                    "oracle_directory": "DATA_PUMP_DIR",
                    "oracle_home_in_container": "/opt/oracle/product/19c/dbhome_1",
                },
            },
        },
    )


@router.get("/embedded-oracle-defaults")
async def get_embedded_oracle_defaults(_: None = Depends(verify_api_key)):
    settings = get_settings()
    oracle_target_host = settings.oracle_target_host or settings.oracle_container_name
    oracle_target_port = settings.oracle_host_port if settings.oracle_target_host else 1521
    return {
        "oracle_container_name": settings.oracle_container_name,
        "oracle_pdb": settings.oracle_pdb,
        "oracle_connection": f"{oracle_target_host}:{oracle_target_port}/{settings.oracle_pdb}",
        "oracle_docker_host": settings.oracle_docker_host,
        "oracle_docker_ssh_port": settings.oracle_docker_ssh_port,
        "oracle_docker_ssh_user": settings.oracle_docker_ssh_user,
        "oracle_dmp_host_path": settings.oracle_dmp_host_path,
        "oracle_dmp_container_path": settings.oracle_dmp_container_path,
        "oracle_tablespace_host_path": settings.oracle_tablespace_host_path,
        "oracle_tablespace_container_path": settings.oracle_tablespace_container_path,
        "oracle_directory": settings.oracle_directory,
        "oracle_home_in_container": settings.oracle_home_in_container,
        "has_oracle_host_password": bool(settings.oracle_docker_ssh_password),
    }


@router.post("/check/mysql", response_model=SetupStepResponse)
async def check_mysql(_: None = Depends(verify_api_key)):
    from recovery_service.db.session import check_mysql_connection

    ok, msg = await check_mysql_connection()
    return SetupStepResponse(step="mysql", ok=ok, message=msg)


@router.post("/check/redis", response_model=SetupStepResponse)
async def check_redis(_: None = Depends(verify_api_key)):
    r = setup_validator.validate_redis({})
    return SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)


@router.post("/check/ssh", response_model=SetupStepResponse)
async def check_ssh(body: SetupCheckRequest, _: None = Depends(verify_api_key)):
    r = setup_validator.validate_ssh(_body_to_dict(body))
    return SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)


@router.post("/check/dmp-files", response_model=SetupStepResponse)
async def check_dmp_files(body: SetupCheckRequest, _: None = Depends(verify_api_key)):
    r = setup_validator.validate_dmp_files(_body_to_dict(body))
    return SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)


@router.post("/check/docker", response_model=SetupStepResponse)
async def check_docker(body: SetupCheckRequest, _: None = Depends(verify_api_key)):
    r = setup_validator.validate_docker(_body_to_dict(body))
    return SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)


@router.post("/check/container-path", response_model=SetupStepResponse)
async def check_container_path(body: SetupCheckRequest, _: None = Depends(verify_api_key)):
    r = setup_validator.validate_container_path(_body_to_dict(body))
    return SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)


@router.post("/check/impdp", response_model=SetupStepResponse)
async def check_impdp(body: SetupCheckRequest, _: None = Depends(verify_api_key)):
    r = setup_validator.validate_impdp(_body_to_dict(body))
    return SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)


@router.post("/check/target-db", response_model=SetupStepResponse)
async def check_target_db(body: SetupCheckRequest, _: None = Depends(verify_api_key)):
    d = _body_to_dict(body)
    if not d.get("target_connection"):
        return SetupStepResponse(step="target_db", ok=False, message="缺少 target_connection")
    r = setup_validator.validate_target_db(d)
    return SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)


@router.post("/check/all", response_model=SetupCheckAllResponse)
async def check_all(body: SetupCheckRequest, _: None = Depends(verify_api_key)):
    results = setup_validator.validate_all(_body_to_dict(body))
    return SetupCheckAllResponse(
        results=[
            SetupStepResponse(step=r.step, ok=r.ok, message=r.message, detail=r.detail)
            for r in results
        ],
        all_passed=all(r.ok for r in results),
    )
