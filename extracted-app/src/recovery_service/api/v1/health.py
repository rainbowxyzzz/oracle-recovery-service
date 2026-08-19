from fastapi import APIRouter

from recovery_service.db.session import check_mysql_connection

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    mysql_ok, mysql_msg = await check_mysql_connection()
    return {
        "status": "ok" if mysql_ok else "degraded",
        "mysql": {"ok": mysql_ok, "message": mysql_msg},
    }
