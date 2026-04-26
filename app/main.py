from fastapi import FastAPI
from app.routers import zoning

app = FastAPI(
    title="建築確認API",
    description="緯度経度から建築規制情報・防災情報・マーケット情報を一括取得するAPI",
    version="0.2.0",
)

app.include_router(zoning.router)
